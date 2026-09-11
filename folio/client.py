"""Minimal MCP client for Wealthfolio's Streamable HTTP endpoint.

The desktop app mounts rmcp's StreamableHttpService with its default, stateful
config (apps/tauri/src/mcp/server.rs), and that fixes the protocol details
below - checked against rmcp 1.8's streamable_http_server/tower.rs:

- A POST must Accept both application/json and text/event-stream (406
  otherwise) and send Content-Type: application/json (415 otherwise).
- `initialize` opens a session. Its id comes back in the Mcp-Session-Id header
  and has to ride on every later request; an unknown id is a 404.
- A request's reply is an SSE stream: first a priming event (an id, a retry
  hint and an empty data field), then the JSON-RPC message. Idle streams get
  keep-alive comments every 15s and nothing promises the stream closes after
  the reply, so read until the matching id arrives and hang up.
- Notifications are answered 202 with no body.
- The app rejects any Origin header other than `null`, and rmcp rejects a Host
  other than localhost/127.0.0.1/::1. urllib sends no Origin and the right
  Host - as long as no HTTP proxy sits in between, which is why loopback URLs
  bypass proxies here.

stdlib only, so it runs wherever the import scripts run.
"""
import http.client
import ipaddress
import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from folio import __version__
from folio.errors import ProtocolError, RpcError, TimedOut, Unauthorized, Unreachable

# A revision rmcp 1.8 accepts (ProtocolVersion::KNOWN_VERSIONS). The server
# answers with the version it settles on, and later requests declare that one.
PROTOCOL_VERSION = '2025-06-18'


def is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ''
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(url):
    # A proxy would rewrite Host and trip rmcp's DNS-rebinding check - and has
    # no business seeing loopback traffic anyway. Remote servers keep the
    # environment's proxy settings.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({} if is_loopback(url) else None))


def _reason(err):
    return getattr(err, 'reason', None) or err


def iter_sse_data(lines):
    """Yield the data payload of each server-sent event, as text.

    `lines` is an iterable of raw byte lines, such as an HTTP response. Events
    with no data - rmcp's priming event, keep-alive comments - yield nothing.
    """
    data = []
    for raw in lines:
        line = raw.decode('utf-8').rstrip('\r\n')
        if not line:
            payload = '\n'.join(data)
            data = []
            if payload.strip():
                yield payload
            continue
        if line.startswith(':'):
            continue
        field, _, value = line.partition(':')
        if field == 'data':
            data.append(value[1:] if value.startswith(' ') else value)
    payload = '\n'.join(data)
    if payload.strip():
        yield payload


def _until(lines, deadline):
    """Pass lines through until `deadline`. The socket timeout alone cannot
    bound a call: the keep-alive pings reset it every 15 seconds."""
    for line in lines:
        yield line
        if time.monotonic() > deadline:
            raise TimeoutError


class McpClient:
    """One MCP session. Entering initializes it; leaving ends it, so the app
    is not left holding a session per invocation."""

    def __init__(self, url, token, timeout=300):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = None
        self.server_info = {}
        self._opener = _opener(url)
        self._ids = itertools.count(1)

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        self.close()

    def initialize(self):
        result = self.request('initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 'folio', 'version': __version__},
        })
        self.protocol_version = result.get('protocolVersion') or PROTOCOL_VERSION
        self.server_info = result.get('serverInfo') or {}
        self.notify('notifications/initialized')
        return result

    def list_tools(self):
        """Every tool this token's scopes reach - Wealthfolio hides the rest."""
        tools, cursor = [], None
        while True:
            result = self.request('tools/list', {'cursor': cursor} if cursor else None)
            tools.extend(result.get('tools') or [])
            cursor = result.get('nextCursor')
            if not cursor:
                return tools

    def call_tool(self, name, arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})

    def request(self, method, params=None):
        rid = next(self._ids)
        message = {'jsonrpc': '2.0', 'id': rid, 'method': method}
        if params is not None:
            message['params'] = params
        deadline = time.monotonic() + self.timeout
        with self._send('POST', message) as resp:
            if method == 'initialize':
                self.session_id = resp.headers.get('Mcp-Session-Id')
            reply = self._read_reply(resp, rid, deadline, method)
        if 'error' in reply:
            raise RpcError(reply['error'])
        return reply.get('result') or {}

    def notify(self, method, params=None):
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        with self._send('POST', message):
            pass

    def close(self):
        """End the session. Best-effort: whatever it was for already happened."""
        if not self.session_id:
            return
        try:
            with self._send('DELETE', timeout=3):
                pass
        except (ProtocolError, Unreachable, Unauthorized):
            pass
        self.session_id = None

    def _send(self, method, message=None, timeout=None):
        headers = {
            'Accept': 'application/json, text/event-stream',
            'Authorization': f'Bearer {self.token}',
            'User-Agent': f'folio/{__version__}',
        }
        data = None
        if message is not None:
            data = json.dumps(message).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        if self.protocol_version:
            headers['MCP-Protocol-Version'] = self.protocol_version
        req = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        try:
            return self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (urllib.error.URLError, OSError) as e:
            if isinstance(_reason(e), TimeoutError):
                raise self._timed_out(message) from None
            raise Unreachable(f'cannot reach {self.url}: {_reason(e)}') from None

    def _http_error(self, e):
        with e:
            body = e.read().decode('utf-8', 'replace').strip()
        if e.code == 401:
            return Unauthorized('Wealthfolio rejected the token (401) - it was removed, '
                                'has expired, or was mistyped')
        if e.code == 404 and self.session_id:
            return ProtocolError('the MCP session is gone (404) - Wealthfolio was restarted '
                                 'or its agent server stopped mid-call')
        if e.code == 404:
            return ProtocolError(f'HTTP 404 from {self.url} - is that the /mcp endpoint?')
        return ProtocolError(f'HTTP {e.code} from {self.url}' + (f': {body}' if body else ''))

    def _timed_out(self, message):
        method = (message or {}).get('method', 'request')
        note = ''
        if method == 'tools/call':
            # The app keeps running a call the client stopped waiting for.
            note = ' The call may still complete - check before retrying a write.'
        return TimedOut(f'no reply to {method} within {self.timeout:g}s.{note}')

    def _read_reply(self, resp, rid, deadline, method):
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
        try:
            if ctype == 'application/json':
                body = json.loads(resp.read())
                messages = body if isinstance(body, list) else [body]
            elif ctype == 'text/event-stream':
                messages = (json.loads(d) for d in iter_sse_data(_until(resp, deadline)))
            else:
                raise ProtocolError(f'{method}: unexpected Content-Type {ctype!r} from {self.url}')
            for m in messages:
                # Anything else on the stream (progress, logging) isn't the reply.
                if isinstance(m, dict) and m.get('id') == rid and ('result' in m or 'error' in m):
                    return m
        except TimeoutError:
            raise self._timed_out({'method': method}) from None
        except ValueError as e:
            raise ProtocolError(f'{method}: malformed JSON from the server: {e}') from None
        except (http.client.HTTPException, OSError) as e:
            raise ProtocolError(f'{method}: connection dropped mid-reply: {e}') from None
        raise ProtocolError(f'{method}: the stream ended without a reply')

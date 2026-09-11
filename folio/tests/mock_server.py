"""A stand-in for Wealthfolio's MCP endpoint, faithful where the client cares.

Mirrors rmcp 1.8's StreamableHttpService in its default stateful mode plus the
desktop app's own middleware (apps/tauri/src/mcp/middleware.rs): Origin other
than none/null is 403, a wrong bearer token 401, a Host outside loopback 403,
a missing Accept type 406, an unknown session 404. Replies stream as chunked
SSE with rmcp's priming event and a keep-alive comment first - and, like rmcp's
streams, may stay open after the reply.

The tool schemas copy the shapes of the real catalog (crates/agent-tools).
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = 'wfp_' + 'k' * 43
KNOWN_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25', '2026-07-28'}

# activity_row_schema() in crates/agent-tools/src/tools/activity_import.rs
ROW_SCHEMA = {
    'type': 'object',
    'properties': {
        'date': {'type': 'string'}, 'activityType': {'type': 'string'},
        'currency': {'type': 'string'}, 'symbol': {'type': 'string'},
        'symbolName': {'type': 'string'}, 'quantity': {'type': 'number'},
        'unitPrice': {'type': 'number'}, 'amount': {'type': 'number'},
        'fee': {'type': 'number'}, 'accountId': {'type': 'string'},
        'comment': {'type': 'string'}, 'lineNumber': {'type': 'integer'},
        'forceImport': {'type': 'boolean'},
    },
    'required': ['date', 'activityType', 'currency'],
}

TOOLS = [
    {'name': 'get_accounts', 'description': 'Get all investment accounts.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'get_holdings', 'description': 'Get current holdings. Optionally by account.',
     'inputSchema': {'type': 'object', 'properties': {
         'accountId': {'type': 'string', 'description': 'Account ID'}}}},
    {'name': 'search_activities', 'description': 'Search activities with filters.',
     'inputSchema': {'type': 'object', 'properties': {
         'accountId': {'type': 'string'},
         'activityType': {'type': 'string', 'description': 'Filter by activity type',
                          'enum': ['BUY', 'SELL', 'DIVIDEND', 'DEPOSIT']},
         'symbol': {'type': 'string', 'description': 'Filter by symbol'},
         'dateFrom': {'type': 'string', 'description': 'Start date in YYYY-MM-DD'},
         'page': {'type': 'integer', 'description': 'Page number, 1-based', 'default': 1}}}},
    {'name': 'prepare_activity_import', 'description': 'Validate rows and detect duplicates.',
     'inputSchema': {'type': 'object', 'properties': {
         'activities': {'type': 'array', 'items': ROW_SCHEMA}}, 'required': ['activities']}},
    {'name': 'record_activities', 'description': 'Prepare a batch of activity drafts.',
     'inputSchema': {'type': 'object', 'properties': {
         'activities': {'type': 'array', 'items': {'type': 'object', 'properties': {
             'activityType': {'type': 'string'}, 'activityDate': {'type': 'string'},
             'quantity': {'type': 'number'}, 'subtype': {'type': 'string'}},
             'required': ['activityType', 'activityDate']}}},
         'required': ['activities']}},
    {'name': 'propose_transaction_categories', 'description': 'Propose categories.',
     'inputSchema': {'type': 'object', 'properties': {
         'activityIds': {'type': 'array', 'items': {'type': 'string'}},
         'includeTransfers': {'type': 'boolean'}}}},
    {'name': 'get_health_status', 'description': 'Run the health checks.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'slow', 'description': 'Never answers.',
     'inputSchema': {'type': 'object', 'properties': {}}},
]

# In the catalog but outside the mock token's scopes: hidden from tools/list,
# and a call is a tool error rather than a JSON-RPC one (handler.rs).
HIDDEN = {'commit_activity_import'}


def _text_result(value, is_error=False):
    result = {'content': [{'type': 'text', 'text': value if is_error else json.dumps(value)}]}
    if is_error:
        result['isError'] = True
    else:
        result['structuredContent'] = value
    return result


class MockServer:
    def __init__(self, token=TOKEN, json_response=False, hold_open=0.0,
                 notify_first=False, page_size=None):
        self.token = token
        self.json_response = json_response      # rmcp's stateless json_response mode
        self.hold_open = hold_open              # seconds a stream stays open after the reply
        self.notify_first = notify_first        # send a progress notification before replies
        self.page_size = page_size              # paginate tools/list
        self.requests = []
        self.sessions = set()
        self.release = threading.Event()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.mock = self
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.httpd.server_address[1]}/mcp'

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def calls(self, method):
        return [r['body'] for r in self.requests if (r['body'] or {}).get('method') == method]

    def reply(self, body):
        method, rid, params = body['method'], body['id'], body.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': params['protocolVersion'],
                      'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'wealthfolio', 'version': '3.8.0'}}
        elif method == 'tools/list':
            start = int(params.get('cursor') or 0)
            end = start + self.page_size if self.page_size else len(TOOLS)
            result = {'tools': TOOLS[start:end]}
            if end < len(TOOLS):
                result['nextCursor'] = str(end)
        elif method == 'tools/call':
            name, args = params['name'], params.get('arguments') or {}
            if name == 'get_health_status':
                result = _text_result('health check exploded', is_error=True)
            elif name in {t['name'] for t in TOOLS}:
                result = _text_result({'tool': name, 'arguments': args})
            elif name in HIDDEN:
                result = _text_result(f'scope denied: {name} requires activities:write', True)
            else:
                return {'jsonrpc': '2.0', 'id': rid,
                        'error': {'code': -32602, 'message': f'unknown tool: {name}'}}
        else:
            return {'jsonrpc': '2.0', 'id': rid,
                    'error': {'code': -32601, 'message': f'method not found: {method}'}}
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    @property
    def mock(self):
        return self.server.mock

    def _record(self, body):
        self.mock.requests.append({
            'method': self.command,
            'headers': {k.lower(): v for k, v in self.headers.items()},
            'body': body,
        })

    def _guarded(self):
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'null':
            return self._plain(403, 'Forbidden')
        if self.headers.get('Authorization') != f'Bearer {self.mock.token}':
            return self._plain(401, '')
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        if host not in ('localhost', '127.0.0.1', '::1'):
            return self._plain(403, 'Forbidden: Host header is not allowed')
        return None

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        body = json.loads(raw) if raw else None
        self._record(body)
        if self._guarded() is not None:
            return
        accept = self.headers.get('Accept') or ''
        if 'application/json' not in accept or 'text/event-stream' not in accept:
            return self._plain(406, 'Not Acceptable: Client must accept both')
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self._plain(415, 'Unsupported Media Type')
        version = self.headers.get('MCP-Protocol-Version')
        is_request = 'id' in body and 'method' in body

        if self.mock.json_response:
            if not is_request:
                return self._plain(202, '')
            return self._json(self.mock.reply(body))

        session = self.headers.get('Mcp-Session-Id')
        if session is None:
            if body.get('method') != 'initialize':
                return self._plain(422, 'Unexpected message, expect initialize request')
            if version and version != body['params']['protocolVersion']:
                return self._plain(400, 'MCP-Protocol-Version does not match')
            session = uuid.uuid4().hex
            self.mock.sessions.add(session)
            return self._sse([self.mock.reply(body)], session)
        if session not in self.mock.sessions:
            return self._plain(404, 'Not Found: Session not found')
        if version and version not in KNOWN_VERSIONS:
            return self._plain(400, f'Bad Request: Unsupported MCP-Protocol-Version: {version}')
        if not is_request:
            return self._plain(202, '')
        if body.get('method') == 'tools/call' and body['params']['name'] == 'slow':
            return self._stall()
        messages = []
        if self.mock.notify_first:
            messages.append({'jsonrpc': '2.0', 'method': 'notifications/progress',
                             'params': {'progressToken': 1, 'progress': 1}})
            messages.append({'jsonrpc': '2.0', 'id': 999, 'result': {}})  # someone else's reply
        messages.append(self.mock.reply(body))
        return self._sse(messages)

    def do_DELETE(self):
        self._record(None)
        if self._guarded() is not None:
            return
        session = self.headers.get('Mcp-Session-Id')
        if not session:
            return self._plain(400, 'Bad Request: Session ID is required')
        self.mock.sessions.discard(session)
        return self._plain(202, '')

    def _plain(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return code

    def _json(self, message):
        data = json.dumps(message).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _chunk(self, data):
        self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
        self.wfile.flush()

    def _open_stream(self, session=None):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        if session:
            self.send_header('Mcp-Session-Id', session)
        self.end_headers()
        self._chunk(b'id: 0\nretry: 3000\ndata: \n\n')   # rmcp's priming event
        self._chunk(b':\n\n')                             # a keep-alive comment

    def _sse(self, messages, session=None):
        self.close_connection = True
        try:
            self._open_stream(session)
            for i, message in enumerate(messages, 1):
                self._chunk(b'id: 0/%d\ndata: %s\n\n' % (i, json.dumps(message).encode()))
            self.mock.release.wait(self.mock.hold_open)
            self._chunk(b'')
        except OSError:
            pass  # the client hung up once it had its reply - expected

    def _stall(self):
        self.close_connection = True
        try:
            self._open_stream()
            while not self.mock.release.wait(0.1):
                self._chunk(b':\n\n')
        except OSError:
            pass

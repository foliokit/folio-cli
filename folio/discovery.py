"""Where Wealthfolio's agent server is, and which token to present.

Endpoint, first match wins:
    --url, $FOLIO_URL, `url` in the config file, the desktop app's mcp.lock,
    then http://127.0.0.1:8639/mcp.
Token, first match wins:
    --token-file, $FOLIO_TOKEN, `token` in the config file.

The desktop app writes mcp.lock (port, pid, startedAt - never a token) into its
app data directory when the agent server starts. It falls back to a random port
when 8639 is taken, so the lock file outranks the default.
"""
import hashlib
import json
import os
import sys
import urllib.parse

from folio.errors import FolioError, UsageError

APP_IDENTIFIER = 'com.teymz.wealthfolio'
DEFAULT_PORT = 8639


def app_data_dir():
    """Tauri's app_data_dir() for the desktop app's identifier."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser(r'~\AppData\Roaming')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, APP_IDENTIFIER)


def config_dir():
    if os.environ.get('FOLIO_CONFIG_DIR'):
        return os.environ['FOLIO_CONFIG_DIR']
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser(r'~\AppData\Roaming')
    elif sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'folio')


def config_path():
    return os.path.join(config_dir(), 'config.json')


def load_config():
    try:
        with open(config_path(), encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise FolioError(f'{config_path()} is not valid JSON: {e}') from None
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    # 0600 on POSIX, since the file holds a bearer token. On Windows the
    # per-user AppData ACL already keeps other accounts out.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def read_lock():
    """The desktop app's discovery file, or None when its server isn't up."""
    path = os.path.join(app_data_dir(), 'mcp.lock')
    try:
        with open(path, encoding='utf-8') as f:
            lock = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(lock, dict) or not isinstance(lock.get('port'), int):
        return None
    lock['path'] = path
    return lock


def normalize_url(url):
    """Accept a bare origin for a self-hosted server: https://wf.example.com
    means https://wf.example.com/mcp."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise UsageError(f'{url!r} is not an http(s) URL - expected e.g. http://127.0.0.1:8639/mcp')
    if parts.path in ('', '/'):
        parts = parts._replace(path='/mcp')
    return urllib.parse.urlunsplit(parts)


def resolve_endpoint(url_flag, cfg):
    """Returns (url, where it came from)."""
    if url_flag:
        return normalize_url(url_flag), '--url'
    if os.environ.get('FOLIO_URL'):
        return normalize_url(os.environ['FOLIO_URL']), '$FOLIO_URL'
    if cfg.get('url'):
        return normalize_url(cfg['url']), f'config {config_path()}'
    lock = read_lock()
    if lock:
        return (f'http://127.0.0.1:{lock["port"]}/mcp',
                f'mcp.lock, pid {lock.get("pid")}, started {lock.get("startedAt")}')
    return f'http://127.0.0.1:{DEFAULT_PORT}/mcp', 'default port - no mcp.lock found'


def resolve_token(token_file, cfg):
    """Returns (token, where it came from), or (None, None)."""
    if token_file:
        try:
            with open(token_file, encoding='utf-8-sig') as f:
                return f.read().strip(), f'--token-file {token_file}'
        except OSError as e:
            raise UsageError(f'cannot read --token-file: {e}') from None
    if os.environ.get('FOLIO_TOKEN'):
        return os.environ['FOLIO_TOKEN'].strip(), '$FOLIO_TOKEN'
    if cfg.get('token'):
        return cfg['token'], f'config {config_path()}'
    return None, None


def fingerprint(token):
    """How Wealthfolio's audit log names a token (wealthfolio_mcp::pat) - safe
    to print, unlike the token."""
    return 'sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]


def token_from_mcp_json(path):
    """The bearer token from a Claude Code .mcp.json entry for Wealthfolio.

    Returns (token, entry name) or (None, None). An entry named `wealthfolio`
    wins; otherwise the first one presenting a wfp_ token.
    """
    try:
        with open(path, encoding='utf-8-sig') as f:
            servers = (json.load(f) or {}).get('mcpServers') or {}
    except (OSError, ValueError) as e:
        raise UsageError(f'cannot read {path}: {e}') from None
    for name in sorted(servers, key=lambda n: n != 'wealthfolio'):
        headers = (servers[name] or {}).get('headers') or {}
        auth = next((v for k, v in headers.items() if k.lower() == 'authorization'), '')
        if auth.startswith('Bearer wfp_'):
            return auth[len('Bearer '):].strip(), name
    return None, None

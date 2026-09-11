"""python -m folio <command | tool> [flags] - see --help."""
import argparse
import contextlib
import difflib
import getpass
import json
import os
import re
import sys

from folio import __version__, discovery, schema_args
from folio.client import McpClient, is_loopback
from folio.errors import FolioError, TimedOut, Unauthorized, Unreachable, UsageError


def _prog():
    """The command as it was typed, so help and hints show one that works."""
    arg0 = sys.argv[0] if sys.argv else ''
    if os.path.basename(arg0) == '__main__.py':
        return 'python -m folio'
    if os.path.isdir(arg0):
        return f'python {arg0}'
    return 'folio'


PROG = _prog()

DESCRIPTION = """\
Command-line client for Wealthfolio's AI Agent Access server - the MCP endpoint
the desktop app runs on 127.0.0.1:8639. Calls go through Wealthfolio's own
services, scope checks and audit log, exactly as an MCP client's would.

commands:
  status              check the endpoint, the token, and what the token reaches
  tools               list the tools this token can use (--schemas: full JSON)
  <tool> --help       a tool's flags, generated from its schema
  <tool> [flags]      call a tool; get_holdings, get-holdings and holdings all work
  login               save a token (prompts, reads stdin, or --from-mcp-json PATH)
  logout              forget the saved token
"""

EPILOG = f"""\
examples:
  {PROG} holdings
  {PROG} search_activities --symbol MU --date-from 2026-01-01
  {PROG} prepare_activity_import --activities @rows.csv --fill accountId=<id>
  {PROG} commit_activity_import --args @payload.json

Results print as JSON: indented on a terminal, compact when piped. Tool flags
go after the tool name; the options above can go anywhere.
exit codes: 0 ok, 1 tool error, 2 usage, 3 unreachable or timed out,
            4 token rejected, 5 protocol error
"""

NO_TOKEN = ('no token. Create one in Wealthfolio > Settings > AI Agent Access, then run '
            f'`{PROG} login` (or set $FOLIO_TOKEN).')


def top_parser():
    p = argparse.ArgumentParser(
        prog=PROG, usage=f'{PROG} [options] <command | tool> [tool flags]',
        description=DESCRIPTION, epilog=EPILOG, add_help=False, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', nargs='?', help=argparse.SUPPRESS)
    g = p.add_argument_group('options')
    g.add_argument('-h', '--help', action='store_true',
                   help="show this help; after a tool or command, show that one's")
    g.add_argument('--version', action='version', version=f'folio {__version__}')
    g.add_argument('--url', help='MCP endpoint (default: from the app\'s mcp.lock, else '
                                 'http://127.0.0.1:8639/mcp)')
    g.add_argument('--token-file', metavar='PATH', help='read the token from this file')
    g.add_argument('--timeout', type=float, default=300, metavar='SECONDS',
                   help='stop waiting for a reply after this long (default 300)')
    g.add_argument('--args', metavar='JSON|@FILE',
                   help='tool arguments as one JSON object; flags override it')
    g.add_argument('--fill', action='append', default=[], metavar='KEY=VALUE',
                   help='set KEY on every row that lacks it (repeatable)')
    g.add_argument('--raw', action='store_true', help='print the whole MCP result')
    fmt = g.add_mutually_exclusive_group()
    fmt.add_argument('--pretty', action='store_true', help='indent JSON even when piped')
    fmt.add_argument('--compact', action='store_true', help='one-line JSON even on a terminal')
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    _utf8_when_piped()
    parser = top_parser()
    try:
        ns, rest = parser.parse_known_args(argv)
        if not ns.command:
            parser.print_help()
            return 0 if ns.help else 2
        commands = {'status': cmd_status, 'tools': cmd_tools,
                    'login': cmd_login, 'logout': cmd_logout}
        return commands.get(ns.command, cmd_call)(ns, rest, parser)
    except FolioError as e:
        sys.stdout.flush()  # keep the error after whatever status already printed
        print(f'folio: {e}', file=sys.stderr)
        return e.exit_code
    except SystemExit as e:
        # argparse's way out: usage errors (2), --help and --version (0).
        return e.code if isinstance(e.code, int) else 0
    except KeyboardInterrupt:
        return 130


def _utf8_when_piped():
    # Piped output would otherwise go out in the Windows ANSI code page and
    # mangle anything outside it. A console is written as Unicode either way.
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def _hint(err, url):
    if isinstance(err, Unauthorized):
        return ('\n  Create a new token in Wealthfolio > Settings > AI Agent Access and run '
                f'`{PROG} login`.')
    if isinstance(err, Unreachable) and not isinstance(err, TimedOut) and is_loopback(url):
        return ('\n  Is Wealthfolio open with its agent server started? Settings > AI Agent '
                'Access > Start ("Start automatically" keeps it on).')
    return ''


@contextlib.contextmanager
def session(ns):
    """An initialized MCP session for the resolved endpoint and token."""
    cfg = discovery.load_config()
    url, _ = discovery.resolve_endpoint(ns.url, cfg)
    token, _ = discovery.resolve_token(ns.token_file, cfg)
    if not token:
        raise UsageError(NO_TOKEN)
    try:
        with McpClient(url, token, ns.timeout) as client:
            yield client
    except (Unauthorized, Unreachable) as e:
        e.args = (str(e) + _hint(e, url),)
        raise


def emit(value, ns):
    pretty = ns.pretty or (not ns.compact and sys.stdout.isatty())
    print(json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                     separators=None if pretty else (',', ':')))


def _sub_parser(prog, description):
    return argparse.ArgumentParser(prog=f'{PROG} {prog}', description=description,
                                   allow_abbrev=False)


def _parse(p, rest, ns):
    # The top-level parser consumed -h, wherever it appeared.
    return p.parse_args(rest + (['--help'] if ns.help else []))


def resolve_tool(name, tools):
    by_name = {t['name']: t for t in tools}
    snake = name.replace('-', '_')
    for candidate in (name, snake, 'get_' + snake):
        if candidate in by_name:
            return by_name[candidate]
    close = difflib.get_close_matches(snake, list(by_name), n=3)
    guess = f' Did you mean {", ".join(close)}?' if close else ''
    raise UsageError(f'no tool {name!r} among the {len(tools)} this token can use.{guess} '
                     f"Tools outside the token's scopes are hidden; `{PROG} tools` lists the rest.")


def cmd_call(ns, rest, parser):
    reserved = {s for action in parser._actions for s in action.option_strings}
    with session(ns) as client:
        tool = resolve_tool(ns.command, client.list_tools())
        tool_parser = schema_args.build_parser(tool, f'{PROG} {tool["name"]}', reserved)
        if ns.help:
            tool_parser.print_help()
            return 0
        parsed = tool_parser.parse_args(rest)
        arguments = schema_args.collect_arguments(
            tool.get('inputSchema') or {}, ns.args, parsed, ns.fill)
        result = client.call_tool(tool['name'], arguments)
    return render(tool['name'], result, ns)


def render(name, result, ns):
    if ns.raw:
        emit(result, ns)
        return 1 if result.get('isError') else 0
    texts = [c.get('text', '') for c in result.get('content') or [] if c.get('type') == 'text']
    if result.get('isError'):
        message = '\n'.join(texts).strip() or '(no message)'
        print(f'folio: {name} failed: {message}', file=sys.stderr)
        return 1
    if 'structuredContent' in result:
        emit(result['structuredContent'], ns)
        return 0
    for text in texts:
        try:
            emit(json.loads(text), ns)
        except ValueError:
            print(text)
    return 0


def summary(description, width=100):
    text = ' '.join((description or '').split())
    # First sentence - but "limits (e.g. RRSP)" is not a sentence end.
    first = re.split(r'(?<=[.!?])(?<!\be\.g\.)(?<!\bi\.e\.)\s', text, maxsplit=1)[0]
    return first if len(first) <= width else first[:width - 3].rstrip() + '...'


def cmd_tools(ns, rest, parser):
    p = _sub_parser('tools', 'List the tools this token can use.')
    p.add_argument('--schemas', action='store_true',
                   help="print each tool's name, description and input schema as JSON")
    a = _parse(p, rest, ns)
    with session(ns) as client:
        tools = sorted(client.list_tools(), key=lambda t: t['name'])
    if a.schemas:
        emit(tools, ns)
        return 0
    width = max((len(t['name']) for t in tools), default=0)
    for t in tools:
        print(f'{t["name"]:<{width}}  {summary(t.get("description"))}')
    sys.stdout.flush()
    print(f'\n{len(tools)} tools. `{PROG} <tool> --help` shows its flags.', file=sys.stderr)
    return 0


def cmd_status(ns, rest, parser):
    _parse(_sub_parser('status', 'Check the endpoint, the token, and what it reaches.'), rest, ns)
    cfg = discovery.load_config()
    url, url_source = discovery.resolve_endpoint(ns.url, cfg)
    print(f'endpoint  {url}  ({url_source})')
    token, token_source = discovery.resolve_token(ns.token_file, cfg)
    if not token:
        raise UsageError(NO_TOKEN)
    print(f'token     {discovery.fingerprint(token)}  ({token_source})')
    with session(ns) as client:
        count = len(client.list_tools())
        info = client.server_info
        print(f'server    {info.get("name")} {info.get("version")}, MCP {client.protocol_version}')
    print(f'access    {count} tools')
    return 0


def cmd_login(ns, rest, parser):
    p = _sub_parser('login', (
        f'Save a Wealthfolio access token to {discovery.config_path()}. Create one in '
        'Wealthfolio > Settings > AI Agent Access. With no option the token is prompted for '
        '(hidden), or read from stdin when piped. --url, if given, is saved too - for a '
        'self-hosted server.'))
    p.add_argument('--from-mcp-json', metavar='PATH',
                   help="take the token from a Claude Code .mcp.json entry for Wealthfolio")
    p.add_argument('--no-verify', action='store_true',
                   help='save without checking the token against the server')
    a = _parse(p, rest, ns)
    if a.from_mcp_json:
        token, _ = discovery.token_from_mcp_json(a.from_mcp_json)
        if not token:
            raise UsageError(f'no entry in {a.from_mcp_json} presents a wfp_ bearer token')
    elif sys.stdin.isatty():
        token = getpass.getpass('Wealthfolio token (input hidden): ').strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise UsageError('no token given')
    if not token.startswith('wfp_'):
        print('folio: warning: Wealthfolio tokens start with wfp_ - saving anyway', file=sys.stderr)

    cfg = discovery.load_config()
    if ns.url:
        cfg['url'] = discovery.normalize_url(ns.url)
    note = ''
    if not a.no_verify:
        url, _ = discovery.resolve_endpoint(ns.url, cfg)
        try:
            with McpClient(url, token, ns.timeout) as client:
                note = f', {len(client.list_tools())} tools visible'
        except Unreachable:
            note = ' - not verified, Wealthfolio is not reachable right now'
    cfg['token'] = token
    discovery.save_config(cfg)
    print(f'saved {discovery.fingerprint(token)} to {discovery.config_path()}{note}')
    return 0


def cmd_logout(ns, rest, parser):
    _parse(_sub_parser('logout', f'Remove the saved token from {discovery.config_path()}.'), rest, ns)
    cfg = discovery.load_config()
    if 'token' not in cfg:
        print('no saved token')
        return 0
    removed = discovery.fingerprint(cfg.pop('token'))
    discovery.save_config(cfg)
    print(f'removed {removed} from {discovery.config_path()}. The token itself stays valid '
          'until you remove it in Wealthfolio > Settings > AI Agent Access.')
    return 0

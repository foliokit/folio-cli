import contextlib
import io
import json
import os
import socket
import unittest
from unittest import mock

from folio import discovery
from folio.cli import main
from folio.tests.mock_server import TOKEN, TOOLS, MockServer
from folio.tests.test_discovery import Isolated


class Cli(Isolated):
    def setUp(self):
        super().setUp()
        self.server = MockServer().__enter__()
        self.addCleanup(self.server.__exit__)
        os.environ['FOLIO_URL'] = self.server.url
        os.environ['FOLIO_TOKEN'] = TOKEN

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch('sys.stdin', io.StringIO(stdin)))
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def sent_arguments(self):
        return self.server.calls('tools/call')[-1]['params']['arguments']

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        return path


class Calls(Cli):
    def test_short_name_and_compact_json_when_piped(self):
        code, out, _ = self.run_cli('holdings', '--account-id', 'a1')
        self.assertEqual(code, 0)
        self.assertEqual(out, '{"tool":"get_holdings","arguments":{"accountId":"a1"}}\n')

    def test_flags_become_typed_arguments(self):
        code, _, _ = self.run_cli('search-activities', '--symbol', 'MU',
                                  '--activity-type', 'buy', '--page', '2')
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_arguments(), {'symbol': 'MU', 'activityType': 'BUY', 'page': 2})

    def test_global_options_can_follow_tool_flags(self):
        code, out, _ = self.run_cli('get_accounts', '--pretty')
        self.assertEqual(code, 0)
        self.assertIn('\n  "tool": "get_accounts"', out)

    def test_args_from_file(self):
        path = self.write('payload.json', '{"symbol": "SNDQ", "page": 4}')
        code, _, _ = self.run_cli('search_activities', '--args', '@' + path, '--page', '1')
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_arguments(), {'symbol': 'SNDQ', 'page': 1})

    def test_converter_csv_with_fill(self):
        path = self.write('rows.csv', (
            'date,activityType,subtype,symbol,quantity,unitPrice,amount,currency,fee,comment\n'
            '2026-04-28,BUY,,SNDQ,500,21.4,10700,USD,,\n'))
        code, _, err = self.run_cli('prepare_activity_import', '--activities', '@' + path,
                                    '--fill', 'accountId=acc-1')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent_arguments()['activities'], [{
            'date': '2026-04-28', 'activityType': 'BUY', 'symbol': 'SNDQ', 'quantity': 500,
            'unitPrice': 21.4, 'amount': 10700, 'currency': 'USD', 'lineNumber': 2,
            'accountId': 'acc-1'}])

    def test_subtype_rows_are_refused_before_anything_is_sent(self):
        path = self.write('rows.json', json.dumps([{
            'date': '2026-03-02', 'activityType': 'SELL', 'currency': 'USD', 'subtype': 'STO'}]))
        code, _, err = self.run_cli('prepare_activity_import', '--activities', '@' + path)
        self.assertEqual(code, 2)
        self.assertIn('subtype', err)
        self.assertEqual(self.server.calls('tools/call'), [])

    def test_missing_required(self):
        code, _, err = self.run_cli('prepare_activity_import')
        self.assertEqual(code, 2)
        self.assertIn('missing required --activities', err)
        self.assertEqual(self.server.calls('tools/call'), [])

    def test_tool_error_exits_1(self):
        code, out, err = self.run_cli('get_health_status')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('get_health_status failed: health check exploded', err)

    def test_raw(self):
        code, out, _ = self.run_cli('--raw', 'get_accounts')
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)), {'content', 'structuredContent'})

    def test_unknown_tool_suggests_the_near_miss(self):
        code, _, err = self.run_cli('search_activity')
        self.assertEqual(code, 2)
        self.assertIn('Did you mean search_activities', err)

    def test_tool_help_calls_nothing(self):
        code, out, _ = self.run_cli('search_activities', '--help')
        self.assertEqual(code, 0)
        self.assertIn('--activity-type', out)
        self.assertIn('one of BUY, SELL, DIVIDEND, DEPOSIT', ' '.join(out.split()))
        self.assertEqual(self.server.calls('tools/call'), [])

    def test_row_list_help_names_the_csv_columns(self):
        code, out, _ = self.run_cli('prepare_activity_import', '-h')
        self.assertEqual(code, 0)
        self.assertIn('date*, activityType*, currency*', ' '.join(out.split()))


class Commands(Cli):
    def test_tools(self):
        code, out, _ = self.run_cli('tools')
        self.assertEqual(code, 0)
        listed = [line.split()[0] for line in out.splitlines()]
        self.assertEqual(listed, sorted(t['name'] for t in TOOLS))

    def test_summary_is_the_first_sentence(self):
        from folio.cli import summary
        self.assertEqual(summary('Get contribution limits (e.g. RRSP, TFSA). More text.'),
                         'Get contribution limits (e.g. RRSP, TFSA).')
        self.assertEqual(summary('x' * 150, width=20), 'x' * 17 + '...')

    def test_tools_schemas(self):
        code, out, _ = self.run_cli('tools', '--schemas')
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), len(TOOLS))

    def test_status(self):
        code, out, _ = self.run_cli('status')
        self.assertEqual(code, 0)
        self.assertIn(f'endpoint  {self.server.url}  ($FOLIO_URL)', out)
        self.assertIn(discovery.fingerprint(TOKEN), out)
        self.assertIn('server    wealthfolio 3.8.0, MCP 2025-06-18', out)
        self.assertIn(f'access    {len(TOOLS)} tools', out)
        self.assertNotIn(TOKEN, out)

    def test_wrong_token(self):
        os.environ['FOLIO_TOKEN'] = 'wfp_wrong'
        code, _, err = self.run_cli('tools')
        self.assertEqual(code, 4)
        self.assertIn('Settings > AI Agent Access', err)

    def test_no_token(self):
        del os.environ['FOLIO_TOKEN']
        code, _, err = self.run_cli('tools')
        self.assertEqual(code, 2)
        self.assertIn('no token', err)

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        code, _, err = self.run_cli('--url', f'http://127.0.0.1:{port}/mcp', 'tools')
        self.assertEqual(code, 3)
        self.assertIn('Is Wealthfolio open with its agent server started?', err)


class Login(Cli):
    def setUp(self):
        super().setUp()
        del os.environ['FOLIO_TOKEN']

    def mcp_json(self, token):
        return self.write('.mcp.json', json.dumps({'mcpServers': {'wealthfolio': {
            'type': 'http', 'url': 'http://127.0.0.1:8639/mcp',
            'headers': {'Authorization': f'Bearer {token}'}}}}))

    def test_from_mcp_json_verifies_then_saves(self):
        code, out, err = self.run_cli('login', '--from-mcp-json', self.mcp_json(TOKEN))
        self.assertEqual(code, 0, err)
        self.assertEqual(discovery.load_config()['token'], TOKEN)
        self.assertIn(f'{len(TOOLS)} tools visible', out)
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(self.run_cli('get_accounts')[0], 0)   # now works with no env token

    def test_rejected_token_is_not_saved(self):
        code, _, _ = self.run_cli('login', '--from-mcp-json', self.mcp_json('wfp_wrong'))
        self.assertEqual(code, 4)
        self.assertNotIn('token', discovery.load_config())

    def test_from_stdin(self):
        code, _, _ = self.run_cli('login', stdin=TOKEN + '\n')
        self.assertEqual(code, 0)
        self.assertEqual(discovery.load_config()['token'], TOKEN)

    def test_unreachable_server_still_saves(self):
        del os.environ['FOLIO_URL']   # default port; nothing listens there in the test
        with mock.patch.object(discovery, 'DEFAULT_PORT', 9):
            code, out, _ = self.run_cli('login', stdin=TOKEN + '\n')
        self.assertEqual(code, 0)
        self.assertIn('not verified', out)

    def test_logout(self):
        self.run_cli('login', stdin=TOKEN + '\n')
        code, out, _ = self.run_cli('logout')
        self.assertEqual(code, 0)
        self.assertNotIn('token', discovery.load_config())
        self.assertIn('stays valid', out)


if __name__ == '__main__':
    unittest.main()

import socket
import time
import unittest

from folio.client import McpClient, iter_sse_data, is_loopback
from folio.errors import ProtocolError, RpcError, TimedOut, Unauthorized, Unreachable
from folio.tests.mock_server import TOKEN, TOOLS, MockServer


def lines(text):
    return [line.encode() for line in text.splitlines(keepends=True)]


class SseParsing(unittest.TestCase):
    def test_skips_priming_and_comments(self):
        stream = 'id: 0\nretry: 3000\ndata: \n\n:\n\nid: 0/1\ndata: {"a":1}\n\n'
        self.assertEqual(list(iter_sse_data(lines(stream))), ['{"a":1}'])

    def test_joins_multiline_data_and_flushes_last_event(self):
        stream = 'data: {"a":\r\ndata: 1}\r\n\r\ndata:{"b":2}'
        self.assertEqual(list(iter_sse_data(lines(stream))), ['{"a":\n1}', '{"b":2}'])


class Loopback(unittest.TestCase):
    def test_hosts(self):
        self.assertTrue(is_loopback('http://127.0.0.1:8639/mcp'))
        self.assertTrue(is_loopback('http://localhost/mcp'))
        self.assertTrue(is_loopback('http://[::1]:8639/mcp'))
        self.assertFalse(is_loopback('https://wf.example.com/mcp'))


class Session(unittest.TestCase):
    def test_full_session_follows_the_stateful_protocol(self):
        with MockServer() as server:
            with McpClient(server.url, TOKEN) as client:
                self.assertEqual(client.server_info['name'], 'wealthfolio')
                self.assertEqual(len(client.list_tools()), len(TOOLS))
                result = client.call_tool('get_holdings', {'accountId': 'a1'})
            self.assertEqual(result['structuredContent']['arguments'], {'accountId': 'a1'})

            methods = [(r['method'], (r['body'] or {}).get('method')) for r in server.requests]
            self.assertEqual(methods, [('POST', 'initialize'),
                                       ('POST', 'notifications/initialized'),
                                       ('POST', 'tools/list'),
                                       ('POST', 'tools/call'),
                                       ('DELETE', None)])
            self.assertEqual(server.sessions, set(), 'session was not ended')

            init, *later = [r['headers'] for r in server.requests]
            for headers in [init] + later:
                self.assertEqual(headers['authorization'], f'Bearer {TOKEN}')
                self.assertNotIn('origin', headers)
            self.assertIn('text/event-stream', init['accept'])
            self.assertIn('application/json', init['accept'])
            # initialize must not declare a version it hasn't negotiated yet
            self.assertNotIn('mcp-protocol-version', init)
            self.assertNotIn('mcp-session-id', init)
            for headers in later:
                self.assertEqual(headers['mcp-protocol-version'], '2025-06-18')
                self.assertTrue(headers['mcp-session-id'])

    def test_reply_does_not_wait_for_the_stream_to_close(self):
        with MockServer(hold_open=5) as server:
            started = time.monotonic()
            with McpClient(server.url, TOKEN) as client:
                client.call_tool('get_accounts', {})
            self.assertLess(time.monotonic() - started, 2.5)

    def test_ignores_other_messages_on_the_stream(self):
        with MockServer(notify_first=True) as server:
            with McpClient(server.url, TOKEN) as client:
                result = client.call_tool('get_accounts', {})
        self.assertEqual(result['structuredContent']['tool'], 'get_accounts')

    def test_json_response_mode(self):
        with MockServer(json_response=True) as server:
            with McpClient(server.url, TOKEN) as client:
                self.assertIsNone(client.session_id)
                self.assertEqual(len(client.list_tools()), len(TOOLS))
        self.assertNotIn('DELETE', [r['method'] for r in server.requests])

    def test_follows_tools_list_pagination(self):
        with MockServer(page_size=3) as server:
            with McpClient(server.url, TOKEN) as client:
                names = [t['name'] for t in client.list_tools()]
        self.assertEqual(names, [t['name'] for t in TOOLS])
        self.assertEqual(len(server.calls('tools/list')), 3)


class Failures(unittest.TestCase):
    def test_wrong_token(self):
        with MockServer() as server:
            with self.assertRaises(Unauthorized):
                McpClient(server.url, 'wfp_wrong').initialize()

    def test_nothing_listening(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        with self.assertRaises(Unreachable):
            McpClient(f'http://127.0.0.1:{port}/mcp', TOKEN, timeout=2).initialize()

    def test_timeout_bounds_a_call_despite_keepalives(self):
        with MockServer() as server:
            with McpClient(server.url, TOKEN, timeout=0.5) as client:
                started = time.monotonic()
                with self.assertRaises(TimedOut) as caught:
                    client.call_tool('slow', {})
                self.assertLess(time.monotonic() - started, 3)
        self.assertIn('may still complete', str(caught.exception))

    def test_session_gone(self):
        with MockServer() as server:
            client = McpClient(server.url, TOKEN)
            client.initialize()
            server.sessions.clear()  # as after an app restart
            with self.assertRaises(ProtocolError) as caught:
                client.list_tools()
        self.assertIn('session is gone', str(caught.exception))

    def test_unknown_tool_is_an_rpc_error(self):
        with MockServer() as server:
            with McpClient(server.url, TOKEN) as client:
                with self.assertRaises(RpcError) as caught:
                    client.call_tool('no_such_tool', {})
        self.assertIn('unknown tool', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

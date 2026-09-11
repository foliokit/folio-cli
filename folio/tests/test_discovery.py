import json
import os
import tempfile
import unittest
from unittest import mock

from folio import discovery
from folio.errors import UsageError


class Isolated(unittest.TestCase):
    """Runs with a temp config dir, a temp app data dir and no FOLIO_* variables."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.app_dir = os.path.join(self.tmp, 'app')
        os.makedirs(self.app_dir)
        env = {k: v for k, v in os.environ.items() if not k.startswith('FOLIO_')}
        env['FOLIO_CONFIG_DIR'] = os.path.join(self.tmp, 'config')
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(discovery, 'app_data_dir', return_value=self.app_dir)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_lock(self, port):
        with open(os.path.join(self.app_dir, 'mcp.lock'), 'w', encoding='utf-8') as f:
            json.dump({'lockFileVersion': 1, 'port': port, 'pid': 42,
                       'startedAt': '2026-09-10T00:00:00Z'}, f)


class Endpoint(Isolated):
    def test_default_port_without_a_lock_file(self):
        url, source = discovery.resolve_endpoint(None, {})
        self.assertEqual(url, 'http://127.0.0.1:8639/mcp')
        self.assertIn('no mcp.lock', source)

    def test_lock_file_wins_over_default(self):
        self.write_lock(51234)   # the app's fallback when 8639 is taken
        url, source = discovery.resolve_endpoint(None, {})
        self.assertEqual(url, 'http://127.0.0.1:51234/mcp')
        self.assertIn('pid 42', source)

    def test_precedence(self):
        self.write_lock(51234)
        cfg = {'url': 'https://cfg.example.com'}
        self.assertEqual(discovery.resolve_endpoint(None, cfg)[0], 'https://cfg.example.com/mcp')
        os.environ['FOLIO_URL'] = 'https://env.example.com/mcp'
        self.assertEqual(discovery.resolve_endpoint(None, cfg)[0], 'https://env.example.com/mcp')
        self.assertEqual(discovery.resolve_endpoint('http://127.0.0.1:9/mcp', cfg)[0],
                         'http://127.0.0.1:9/mcp')

    def test_corrupt_lock_file_is_ignored(self):
        with open(os.path.join(self.app_dir, 'mcp.lock'), 'w') as f:
            f.write('{"port": ')   # caught mid-write
        self.assertEqual(discovery.resolve_endpoint(None, {})[0], 'http://127.0.0.1:8639/mcp')

    def test_normalize_url(self):
        self.assertEqual(discovery.normalize_url('https://wf.example.com'), 'https://wf.example.com/mcp')
        self.assertEqual(discovery.normalize_url('https://wf.example.com/'), 'https://wf.example.com/mcp')
        self.assertEqual(discovery.normalize_url('http://h:1/custom/mcp'), 'http://h:1/custom/mcp')
        with self.assertRaises(UsageError):
            discovery.normalize_url('127.0.0.1:8639')


class Token(Isolated):
    def test_precedence(self):
        path = os.path.join(self.tmp, 'token.txt')
        with open(path, 'w') as f:
            f.write('wfp_file\n')
        cfg = {'token': 'wfp_cfg'}
        self.assertEqual(discovery.resolve_token(None, {}), (None, None))
        self.assertEqual(discovery.resolve_token(None, cfg)[0], 'wfp_cfg')
        os.environ['FOLIO_TOKEN'] = 'wfp_env'
        self.assertEqual(discovery.resolve_token(None, cfg)[0], 'wfp_env')
        self.assertEqual(discovery.resolve_token(path, cfg)[0], 'wfp_file')

    def test_fingerprint_matches_wealthfolio_audit_format(self):
        # sha256("wfp_test"), first 16 hex chars - computed with sha256sum.
        self.assertEqual(discovery.fingerprint('wfp_test'), 'sha256:ad9b45ead8967010')

    def test_config_round_trip(self):
        self.assertEqual(discovery.load_config(), {})
        discovery.save_config({'token': 'wfp_x'})
        self.assertEqual(discovery.load_config(), {'token': 'wfp_x'})
        self.assertFalse(os.path.exists(discovery.config_path() + '.tmp'))

    def test_token_from_mcp_json(self):
        path = os.path.join(self.tmp, '.mcp.json')
        with open(path, 'w') as f:
            json.dump({'mcpServers': {
                'other': {'type': 'http', 'headers': {'Authorization': 'Bearer wfp_other'}},
                'wealthfolio': {'type': 'http', 'url': 'http://127.0.0.1:8639/mcp',
                                'headers': {'authorization': 'Bearer wfp_mine'}},
            }}, f)
        self.assertEqual(discovery.token_from_mcp_json(path), ('wfp_mine', 'wealthfolio'))
        with open(path, 'w') as f:
            json.dump({'mcpServers': {'x': {'headers': {'Authorization': 'Bearer sk-nope'}}}}, f)
        self.assertEqual(discovery.token_from_mcp_json(path), (None, None))


if __name__ == '__main__':
    unittest.main()

import contextlib
import io
import json
import os
import tempfile
import unittest

from folio import schema_args
from folio.errors import UsageError
from folio.tests.mock_server import TOOLS

TOOL = {t['name']: t for t in TOOLS}


def parse(tool_name, argv, reserved=()):
    tool = TOOL[tool_name]
    parser = schema_args.build_parser(tool, 'folio ' + tool_name, set(reserved))
    with contextlib.redirect_stderr(io.StringIO()):   # argparse's usage errors
        return parser.parse_args(argv)


class Flags(unittest.TestCase):
    def test_kebab(self):
        self.assertEqual(schema_args.kebab('activityType'), 'activity-type')
        self.assertEqual(schema_args.kebab('accountId'), 'account-id')
        self.assertEqual(schema_args.kebab('page'), 'page')

    def test_kebab_and_schema_names_both_work(self):
        self.assertEqual(parse('search_activities', ['--date-from', '2026-01-01']).dateFrom,
                         '2026-01-01')
        self.assertEqual(parse('search_activities', ['--dateFrom', '2026-01-01']).dateFrom,
                         '2026-01-01')

    def test_scalars_are_typed(self):
        ns = parse('search_activities', ['--activity-type', 'buy', '--page', '2'])
        self.assertEqual(ns.activityType, 'BUY')   # enum matched case-insensitively
        self.assertEqual(ns.page, 2)

    def test_bad_values_are_usage_errors(self):
        with self.assertRaises(SystemExit):
            parse('search_activities', ['--activity-type', 'bought'])
        with self.assertRaises(SystemExit):
            parse('search_activities', ['--page', 'two'])

    def test_unset_flags_stay_unset(self):
        ns = parse('search_activities', [])
        self.assertIsNone(ns.page)   # the schema default is the server's to apply

    def test_booleans_and_scalar_lists(self):
        ns = parse('propose_transaction_categories',
                   ['--activity-ids', 'a', 'b', '--activity-ids', 'c', '--no-include-transfers'])
        self.assertEqual(ns.activityIds, ['a', 'b', 'c'])
        self.assertIs(ns.includeTransfers, False)

    def test_global_flags_are_not_shadowed(self):
        self.assertEqual(parse('get_holdings', ['--accountId', 'a1'], {'--account-id'}).accountId,
                         'a1')
        with self.assertRaises(SystemExit):
            parse('get_holdings', ['--account-id', 'a1'], {'--account-id'})

    def test_default_noted_once(self):
        self.assertEqual(schema_args._help({'description': 'Page size', 'default': 50}, False),
                         'Page size [default 50]')
        self.assertEqual(schema_args._help({'description': 'Page size (default: 50)',
                                            'default': 50}, False), 'Page size (default: 50)')

    def test_numbers(self):
        self.assertEqual(schema_args.parse_number('100'), 100)
        self.assertEqual(schema_args.parse_number('0.1'), 0.1)
        self.assertEqual(schema_args.parse_number('-5'), -5)
        for bad in ('nan', 'inf', '1,000'):
            with self.assertRaises(ValueError):
                schema_args.parse_number(bad)


class Rows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, text):
        path = os.path.join(self.dir.name, name)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        return path

    def test_csv_rows_typed_blank_cells_dropped_line_numbers_filled(self):
        # The column set the import converters write (SKILL.md "Target format").
        path = self.write('rows.csv', (
            'date,activityType,subtype,symbol,quantity,unitPrice,amount,currency,fee,comment\n'
            '2026-03-02,BUY,,MU,100,95.5,9550,USD,1,first\n'
            '2026-03-03,DIVIDEND,,MU,,,12.5,USD,,\n'))
        rows = parse('prepare_activity_import', ['--activities', '@' + path]).activities
        self.assertEqual(rows[0], {'date': '2026-03-02', 'activityType': 'BUY', 'symbol': 'MU',
                                   'quantity': 100, 'unitPrice': 95.5, 'amount': 9550,
                                   'currency': 'USD', 'fee': 1, 'comment': 'first',
                                   'lineNumber': 2})
        self.assertEqual(rows[1], {'date': '2026-03-03', 'activityType': 'DIVIDEND',
                                   'symbol': 'MU', 'amount': 12.5, 'currency': 'USD',
                                   'lineNumber': 3})

    def test_csv_with_a_subtype_value_is_refused(self):
        path = self.write('options.csv', (
            'date,activityType,subtype,symbol,quantity,unitPrice,amount,currency\n'
            '2026-03-02,SELL,STO,MU260417P00095000,1,4.2,420,USD\n'))
        with self.assertRaises(ValueError) as caught:
            schema_args.read_csv_rows(path, TOOL['prepare_activity_import']
                                      ['inputSchema']['properties']['activities']['items'])
        self.assertIn('subtype', str(caught.exception))
        self.assertIn('line 2', str(caught.exception))
        self.assertIn('sell-to-open would book as a plain sale', str(caught.exception))

    def test_csv_unknown_column_is_refused(self):
        path = self.write('extra.csv', 'date,activityType,currency,notes\n2026-01-01,FEE,CAD,x\n')
        with self.assertRaises(SystemExit):
            parse('prepare_activity_import', ['--activities', '@' + path])

    def test_csv_bad_number_names_line_and_column(self):
        path = self.write('bad.csv', 'date,activityType,currency,amount\n2026-01-01,FEE,CAD,"1,000"\n')
        with self.assertRaises(ValueError) as caught:
            schema_args.read_csv_rows(path, TOOL['prepare_activity_import']
                                      ['inputSchema']['properties']['activities']['items'])
        self.assertIn('line 2, amount: expected a number', str(caught.exception))

    def test_csv_row_longer_than_header(self):
        path = self.write('long.csv', 'date,activityType,currency\n2026-01-01,FEE,CAD,oops\n')
        with self.assertRaises(ValueError) as caught:
            schema_args.read_csv_rows(path, TOOL['prepare_activity_import']
                                      ['inputSchema']['properties']['activities']['items'])
        self.assertIn('more cells than header columns', str(caught.exception))

    def test_json_rows_from_file(self):
        path = self.write('rows.json', json.dumps([{'date': '2026-01-01'}]))
        rows = parse('prepare_activity_import', ['--activities', '@' + path]).activities
        self.assertEqual(rows, [{'date': '2026-01-01'}])


class Collect(unittest.TestCase):
    schema = TOOL['prepare_activity_import']['inputSchema']

    def collect(self, tool_name, args_text=None, argv=(), fills=()):
        tool = TOOL[tool_name]
        parsed = schema_args.build_parser(tool, 'x', set()).parse_args(list(argv))
        return schema_args.collect_arguments(tool['inputSchema'], args_text, parsed, list(fills))

    def test_flags_override_args(self):
        got = self.collect('search_activities', '{"symbol": "MU", "page": 3}', ['--page', '1'])
        self.assertEqual(got, {'symbol': 'MU', 'page': 1})

    def test_args_must_be_an_object(self):
        with self.assertRaises(UsageError):
            self.collect('search_activities', '[1]')

    def test_missing_required(self):
        with self.assertRaises(UsageError) as caught:
            self.collect('prepare_activity_import')
        self.assertIn('--activities', str(caught.exception))

    def test_fill_sets_only_missing_fields_and_types_them(self):
        rows = [{'date': 'd', 'accountId': 'keep'}, {'date': 'd'}, {'date': 'd', 'accountId': ''}]
        got = self.collect('prepare_activity_import', json.dumps({'activities': rows}),
                           fills=['accountId=acc-1', 'forceImport=true'])
        self.assertEqual([r['accountId'] for r in got['activities']], ['keep', 'acc-1', 'acc-1'])
        self.assertTrue(all(r['forceImport'] is True for r in got['activities']))

    def test_fill_needs_a_row_field(self):
        with self.assertRaises(UsageError):
            self.collect('prepare_activity_import', '{"activities": []}', fills=['nope=1'])
        with self.assertRaises(UsageError):
            self.collect('prepare_activity_import', '{"activities": []}', fills=['accountId'])

    def test_json_rows_with_subtype_refused_where_the_tool_would_drop_it(self):
        rows = [{'date': 'd', 'activityType': 'SELL', 'currency': 'USD', 'subtype': 'STO'}]
        with self.assertRaises(UsageError) as caught:
            self.collect('prepare_activity_import', json.dumps({'activities': rows}))
        self.assertIn('1 of 1 activities rows have a subtype', str(caught.exception))

    def test_subtype_passes_where_the_tool_keeps_it(self):
        rows = [{'activityType': 'SELL', 'activityDate': 'd', 'subtype': 'STO'}]
        got = self.collect('record_activities', json.dumps({'activities': rows}))
        self.assertEqual(got['activities'][0]['subtype'], 'STO')


if __name__ == '__main__':
    unittest.main()

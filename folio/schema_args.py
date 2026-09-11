"""Turn a tool's JSON input schema into command-line flags.

Each property gets --kebab-case and --camelCase flags; the second is the name
the schema and Wealthfolio's docs use. Scalars parse from the flag's text, with
enum values matched case-insensitively. An array of scalars takes several
values. Objects and arrays of objects take inline JSON or @file.json, and a
list of rows also takes @file.csv - the format the import converters write.

The whole argument object can also come in through --args; flags override it.
"""
import argparse
import csv
import json
import math
import re
import sys
import textwrap

from folio.errors import UsageError

SCALARS = ('string', 'integer', 'number', 'boolean')

# Why a row carrying `subtype` is refused rather than sent: nothing on the
# server rejects unknown fields, so the value would vanish without an error.
DROPPED_SUBTYPE = (
    "`subtype` carries option open/close intent (and DRIP, BONUS, ...), and this tool's "
    'rows have no such field: Wealthfolio would drop it without an error, and a '
    'sell-to-open would book as a plain sale. Import those rows through the app\'s CSV '
    'import, or with record_activities + commit_activity_drafts, which keep subtype.'
)


def kebab(name):
    return re.sub(r'(?<=[a-z0-9])([A-Z])', r'-\1', name).replace('_', '-').lower()


def prop_type(ps):
    t = (ps or {}).get('type')
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), None)
    return t


def is_row_list(ps):
    return prop_type(ps) == 'array' and prop_type(ps.get('items')) == 'object'


def item_props(ps):
    return (ps.get('items') or {}).get('properties') or {}


def parse_number(text):
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f'expected a number, got {text!r}') from None
    if not math.isfinite(value):
        raise ValueError(f'expected a finite number, got {text!r}')
    return value


def parse_integer(text):
    try:
        return int(text)
    except ValueError:
        raise ValueError(f'expected an integer, got {text!r}') from None


def parse_bool(text):
    lowered = text.strip().lower()
    if lowered in ('true', 'yes', 'y', '1'):
        return True
    if lowered in ('false', 'no', 'n', '0'):
        return False
    raise ValueError(f'expected true or false, got {text!r}')


def enum_parser(values):
    lookup = {str(v).lower(): v for v in values}

    def parse(text):
        try:
            return lookup[text.strip().lower()]
        except KeyError:
            raise ValueError(f'{text!r} is not one of {", ".join(map(str, values))}') from None
    return parse


def scalar_parser(ps):
    if ps.get('enum'):
        return enum_parser(ps['enum'])
    return {'integer': parse_integer, 'number': parse_number,
            'boolean': parse_bool}.get(prop_type(ps), str)


def load_json(text, what='value'):
    """Inline JSON, @path to a JSON file, or @- for stdin."""
    try:
        if text == '@-':
            return json.load(sys.stdin)
        if text.startswith('@'):
            with open(text[1:], encoding='utf-8-sig') as f:
                return json.load(f)
        return json.loads(text)
    except OSError as e:
        raise ValueError(f'cannot read {text[1:]}: {e.strerror}') from None
    except ValueError as e:
        raise ValueError(f'{what} is not valid JSON ({e}) - give inline JSON, @file.json or @-') from None


def load_rows(text, ps):
    if text.startswith('@') and text.lower().endswith('.csv'):
        return read_csv_rows(text[1:], ps.get('items') or {})
    rows = load_json(text)
    if not isinstance(rows, list):
        raise ValueError('expected a JSON array of objects')
    return rows


def read_csv_rows(path, item_schema):
    """Rows for an array-of-objects property, from a CSV with a header row.

    Blank cells are left out rather than sent as empty strings. A non-blank
    column the row schema doesn't declare is an error rather than skipped,
    because Wealthfolio would skip it silently. lineNumber, when the schema has
    it, defaults to the row's line in the file so duplicate reports point there.
    """
    props = item_schema.get('properties') or {}
    parsers = {name: scalar_parser(ps) if prop_type(ps) in SCALARS else json.loads
               for name, ps in props.items()}
    rows, unknown = [], {}
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for record in reader:
                row = {}
                for column, cell in record.items():
                    if column is None:
                        raise ValueError(f'{path} line {reader.line_num}: more cells than header columns')
                    cell = (cell or '').strip()
                    if not cell:
                        continue
                    name = column.strip()
                    if name not in props:
                        unknown.setdefault(name, reader.line_num)
                        continue
                    try:
                        row[name] = parsers[name](cell)
                    except ValueError as e:
                        raise ValueError(f'{path} line {reader.line_num}, {name}: {e}') from None
                if 'lineNumber' in props and 'lineNumber' not in row:
                    row['lineNumber'] = reader.line_num
                rows.append(row)
    except OSError as e:
        raise ValueError(f'cannot read {path}: {e.strerror}') from None
    if unknown:
        columns = ', '.join(f'{c} (first at line {n})' for c, n in unknown.items())
        message = f"{path}: column not in this tool's row schema: {columns}."
        if 'subtype' in unknown:
            message += ' ' + DROPPED_SUBTYPE
        raise ValueError(message + ' Otherwise remove or rename the column.')
    return rows


def _argparse_type(parse):
    # argparse replaces a plain ValueError's message with a generic one.
    def convert(text):
        try:
            return parse(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    return convert


def _loose(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _flag_kwargs(name, ps):
    t = prop_type(ps)
    metavar = kebab(name).upper().replace('-', '_')
    if t == 'boolean':
        return {'action': argparse.BooleanOptionalAction}
    if t in SCALARS:
        return {'type': _argparse_type(scalar_parser(ps)), 'metavar': metavar}
    if t == 'array' and prop_type(ps.get('items')) in SCALARS:
        return {'type': _argparse_type(scalar_parser(ps['items'])), 'nargs': '+',
                'action': 'extend', 'metavar': metavar}
    if is_row_list(ps):
        return {'type': _argparse_type(lambda text: load_rows(text, ps)),
                'metavar': 'JSON|@FILE.json|@FILE.csv'}
    if t in ('object', 'array'):
        return {'type': _argparse_type(load_json), 'metavar': 'JSON|@FILE'}
    return {'type': _loose, 'metavar': 'VALUE'}


def _field_list(props, required):
    return ', '.join(n + ('*' if n in required else '') for n in props)


def _help(ps, required):
    notes = []
    if required:
        notes.append('required')
    if ps.get('enum'):
        notes.append('one of ' + ', '.join(map(str, ps['enum'])))
    text = ' '.join((ps.get('description') or '').split())
    if 'default' in ps and 'default' not in text.lower():
        notes.append('default ' + json.dumps(ps['default']))
    if is_row_list(ps):
        items = ps.get('items') or {}
        notes.append('row fields: ' + _field_list(item_props(ps), items.get('required') or []))
    elif prop_type(ps) == 'object' and ps.get('properties'):
        notes.append('fields: ' + _field_list(ps['properties'], ps.get('required') or []))
    if notes:
        text = f'{text} [{"; ".join(notes)}]'.strip()
    return text.replace('%', '%%')


def _wrap(text, width=79):
    """Wrap long lines, keeping the line breaks (and list items) a description has."""
    lines = []
    for line in (text or '').splitlines():
        indent = re.match(r'\s*(?:[-*]\s+|\d+\.\s+)?', line).end()
        lines.append(textwrap.fill(line, width, subsequent_indent=' ' * indent)
                     if len(line) > width else line)
    return '\n'.join(lines)


def build_parser(tool, prog, reserved):
    """An argparse parser for one tool. Flags in `reserved` (the global ones)
    are skipped; such a property is still reachable through --args."""
    schema = tool.get('inputSchema') or {}
    props = schema.get('properties') or {}
    required = set(schema.get('required') or [])
    epilog = ("Any argument can also come in through --args '{...}', --args @payload.json or "
              '--args @- (stdin); flags override --args.')
    if any(is_row_list(ps) for ps in props.values()):
        epilog += (' --fill KEY=VALUE sets a field on every row that lacks it, e.g. '
                   '--fill accountId=<id> for a converter CSV. Rows are refused if they carry a '
                   'subtype the tool would drop.')
    parser = argparse.ArgumentParser(
        prog=prog, description=_wrap(tool.get('description')), epilog=_wrap(epilog),
        add_help=False,
        allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, ps in props.items():
        flags = [f for f in dict.fromkeys(('--' + kebab(name), '--' + name)) if f not in reserved]
        if flags:
            parser.add_argument(*flags, dest=name, default=None, help=_help(ps, name in required),
                                **_flag_kwargs(name, ps))
    return parser


def apply_fills(arguments, schema, fills):
    """--fill KEY=VALUE: set KEY on every row, in every row list, that lacks it."""
    props = schema.get('properties') or {}
    for fill in fills:
        key, sep, text = fill.partition('=')
        key = key.strip()
        if not sep or not key:
            raise UsageError(f'--fill expects KEY=VALUE, got {fill!r}')
        targets = [(name, item_props(ps)[key]) for name, ps in props.items()
                   if is_row_list(ps) and key in item_props(ps)]
        if not targets:
            raise UsageError(f'--fill {key}: no row list in this tool has a {key} field')
        for name, key_ps in targets:
            try:
                value = scalar_parser(key_ps)(text) if prop_type(key_ps) in SCALARS else load_json(text)
            except ValueError as e:
                raise UsageError(f'--fill {key}: {e}') from None
            for row in arguments.get(name) or []:
                if isinstance(row, dict) and row.get(key) in (None, ''):
                    row[key] = value


def check_subtypes(arguments, schema):
    """Refuse rows whose subtype the tool would silently drop - see DROPPED_SUBTYPE."""
    for name, ps in (schema.get('properties') or {}).items():
        if not is_row_list(ps) or 'subtype' in item_props(ps):
            continue
        rows = arguments.get(name)
        if not isinstance(rows, list):
            continue
        hits = [i for i, row in enumerate(rows, 1) if isinstance(row, dict) and row.get('subtype')]
        if hits:
            shown = ', '.join(map(str, hits[:10])) + (', ...' if len(hits) > 10 else '')
            raise UsageError(f'{len(hits)} of {len(rows)} {name} rows have a subtype '
                             f'(rows {shown}). {DROPPED_SUBTYPE}')


def collect_arguments(schema, args_text, parsed, fills):
    """Merge --args, the tool's flags and --fill into the tools/call arguments."""
    arguments = {}
    if args_text is not None:
        try:
            base = load_json(args_text, '--args')
        except ValueError as e:
            raise UsageError(str(e)) from None
        if not isinstance(base, dict):
            raise UsageError('--args must be a JSON object')
        arguments.update(base)
    for name in schema.get('properties') or {}:
        value = getattr(parsed, name, None)
        if value is not None:
            arguments[name] = value
    apply_fills(arguments, schema, fills)
    check_subtypes(arguments, schema)
    missing = [n for n in schema.get('required') or [] if arguments.get(n) is None]
    if missing:
        raise UsageError('missing required ' + ', '.join('--' + kebab(n) for n in missing))
    return arguments

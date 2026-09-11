# folio

A command-line client for [Wealthfolio](https://wealthfolio.app/)'s **AI Agent
Access** server — the MCP endpoint the desktop app runs on `127.0.0.1:8639`
(and a self-hosted server on `/mcp`).

Use it from a terminal, scripts or scheduled jobs, or hand it to an AI coding
agent instead of wiring the endpoint in as an MCP server:

- **Nothing to connect up front.** An MCP server that is down when an agent
  session starts tends to stay unavailable for the whole session. folio
  connects per call, so opening Wealthfolio later just works.
- **Bulk work stays out of the conversation.** A script can make dozens of
  calls — every transfer, a search per date window — and hand the agent only
  the result. Through MCP, each tool result lands in the agent's context.
- **Payloads come from files.** A few hundred import rows are
  `--activities @rows.json` or `@rows.csv`, not JSON written inline into a
  tool call.
- **Guards a silent import trap.** Rows carrying a `subtype` are refused before
  they reach a tool that would drop it without an error (see [Limits](#limits)).

For a quick one-off question it behaves the same as the MCP server; the
difference shows in bulk work and imports.

It is a client, not a second backend: every call goes through Wealthfolio's
own services, token scopes and audit log. It never opens the database file.

## Install

Python 3.10+, standard library only.

```bash
pipx install git+https://github.com/foliokit/folio-cli      # or: pip install git+...
```

Or run it from a checkout without installing:

```bash
git clone https://github.com/foliokit/folio-cli
python folio-cli/folio status          # same as `folio status`
```

## Setup

1. In Wealthfolio: **Settings → AI Agent Access** — enable it, **Start** the
   server (*Start automatically* keeps it on), and create a token. Pick the
   scopes you want folio to have; tools outside them are hidden.
2. Save the token and check the connection:

   ```bash
   folio login          # prompts, input hidden; or pipe it on stdin
   folio status         # endpoint, token fingerprint, how many tools it reaches
   ```

   The token is stored in `%APPDATA%\folio\config.json` (`~/Library/Application
   Support/folio` on macOS, `~/.config/folio` elsewhere) and never printed.
   `status` shows its `sha256:` fingerprint — the same id Wealthfolio's audit
   log records for it. `login --from-mcp-json .mcp.json` imports the token from
   an existing MCP client config.

Precedence, first match wins. Endpoint: `--url`, `$FOLIO_URL`, the config's
`url`, the desktop app's `mcp.lock` (it moves off 8639 when that port is
taken), then `http://127.0.0.1:8639/mcp`. Token: `--token-file`,
`$FOLIO_TOKEN`, the config's `token`. For a self-hosted server,
`folio login --url https://your-host` saves the URL too.

## Use

```bash
folio tools                               # what this token can call
folio search_activities --help            # a tool's flags, from its schema
folio holdings                            # get_holdings; the get_ is optional
folio search-activities --symbol MU --activity-type buy --date-from 2026-01-01
folio prepare_activity_import --activities @rows.csv --fill accountId=<id>
folio commit_activity_import --activities @rows.csv --fill accountId=<id>
folio record_activities --args @drafts.json
```

- Every schema property is a flag, as `--kebab-case` or `--camelCase`. Enum
  values match case-insensitively; lists of IDs take several values.
- Objects and row lists take inline JSON, `@file.json`, or `@-` for stdin. Row
  lists also take `@file.csv` with a header row whose columns are the row's
  fields (`folio <tool> --help` lists them). Blank cells are omitted, and
  `lineNumber` defaults to the CSV line so duplicate reports point at the file.
- `--fill KEY=VALUE` sets a field on every row that lacks it.
- `--args` supplies the whole argument object; flags override it.
- Output is the tool's JSON: compact when piped, indented on a terminal
  (`--pretty` / `--compact` to force). `--raw` prints the full MCP result.

Exit codes: `0` ok, `1` the tool reported an error, `2` usage, `3` unreachable
or timed out, `4` token rejected, `5` protocol error. A write that timed out
may still complete in the app — check before retrying it.

### With an AI agent

Tell the agent the tool exists and let it discover the rest — for example in a
`CLAUDE.md` or `AGENTS.md`:

```markdown
Wealthfolio data: run `folio tools`, then `folio <tool> --help`. Pass large
payloads as files (`@rows.json`). Ask before running any `commit_*` tool.
```

## Limits

folio can do exactly what Wealthfolio's agent tools can, nothing more:

- **The import tools can't carry `subtype`** — which holds option open/close
  intent, DRIP and BONUS. The server ignores unknown fields, so the value would
  vanish and a sell-to-open would book as a plain sale; folio refuses such rows
  instead. Import them through the app, or with `record_activities` followed
  by `commit_activity_drafts`, which do keep it.
- No tool links or unlinks transfers, updates or deletes activities, edits
  quotes, or makes backups. Those stay in the app.
- **It sees only what the tools return.** `search_activities` rows carry no
  transfer-link state, for example, so folio can't tell which transfers are
  already linked. `get_health_status` lists the unmatched ones, as text.

## Develop

```bash
python -m unittest discover -s folio/tests -t .
```

`folio/tests/mock_server.py` imitates the real endpoint: rmcp 1.8's stateful
Streamable HTTP (chunked SSE, a priming event, streams held open after the
reply, 404 for a lost session) behind the app's Origin and bearer-token checks.
`folio/client.py` documents which of those details the client depends on.

---

MIT licensed. Not affiliated with or endorsed by the Wealthfolio project.
Wealthfolio is a trademark of Teymz Inc.

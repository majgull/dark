# Report: 1051-a, the `ledger-tail` command and the spec tool table

## Terms, once

- **ledger**: dark's append-only file of one JSON row per event, `state_dir/ledger.jsonl`, written by `runner/dark/ledger.py`; every row carries a `kind` field naming the event.
- **`run.end` row**: the one ledger row per finished run, the closing record of a run.
- **MCP server**: `python3 -m dark mcp`, the program that lists six tools over stdio and answers `tools/list` and `tools/call`; five tools run a `dark` command and `dark_ledger_tail` reads the ledger itself.

## Item 1, the command

- Added the `ledger-tail` subparser (`--lines`, integer, default 20; `--kind`, string) and `cmd_ledger_tail` to `runner/dark/__main__.py`, and registered it in the dispatch table.
- The command resolves the ledger as `host.ledger_path` through `config.load_host(os.path.join(args.conf, "host.toml"))`: no token files, no Gitea, no sandbox.
- It prints the last `--lines` rows that match `--kind`, oldest first, one JSON object per line. A missing or empty ledger prints nothing and exits 0. A line that is not a JSON object is skipped, with one note on stderr naming the path and line number.
- The kind field name is `kind`: `ledger.py` writes `{"v": ..., "ts": ..., "iso": ..., "kind": kind, ...}`. Rows carry a kind, so NOT FOUND does not apply.
- MCP reader reuse: `runner/dark/mcp.py` is importable without side effects, but I did not import it. Its `tail_ledger(arguments)` returns an MCP result dict, streams raw lines without a kind filter, and does not note malformed lines, so it cannot satisfy `--kind` or the skip-with-a-note requirement. I read `host.ledger_path` the same way `tail_ledger` resolves it.
- Test: `runner/tests/test_cli_ledger_tail.py` writes a fixture under `/tmp/fx/` with fifteen rows of `run.start` and `run.end` plus one malformed line, and asserts the default prints all fifteen oldest first as parseable JSON, `--lines 3` prints three, `--kind` filters, the malformed line is skipped with one note on stderr, and an absent or empty ledger exits 0 with no output.
- Scope run before the commit: `cd runner && python3 -m unittest tests.test_cli_ledger_tail -q` prints OK (7 tests).
- Ticked tasks item 1. Commit `feat(cli): add the ledger-tail command`.

## Item 2, the spec in line and the two missing lines

- Rewrote the tool table and the argument paragraphs in `specs/mcp-server/spec.md` to what `docs/mcp.md` and `runner/dark/mcp.py` implement: six tools; **`task`** and **`tier`** required for `dark_run`, `dark_user`, `dark_long` and `dark_review`; `arm` and `slot` for `dark_run`; `judge_tier` for `dark_long`; `review_branches` (JSON text) for `dark_review`; `n` (1 to 200, default 10) for `dark_ledger_tail`, which the server answers itself; `timeout_seconds` (1 to 86400, default 1800) on the five command tools; one text item joining stdout, stderr and `exit code N`; SIGTERM then SIGKILL on a timeout. Removed the stale out-of-scope line about timeouts and fixed the Purpose sentence so the ledger tool is described as answered by the server.
- Updated the matching parts of `specs/mcp-server/plan.md`: the `TOOLS`, `validate`, `build_argv` and `handle`/`serve` bullets, the tool-call paragraph, and the "Where the CLI has no matching command" section.
- Added the one line to the README Documentation list: "`docs/mcp.md` says how to start and register the MCP server."
- Added the one term to `docs/concepts.md`: "MCP server".
- Ticked tasks item 10. The only ticks changed are items 1 and 10; item 6 remains open.
- Six tool names in `specs/mcp-server/spec.md`, in table order: dark_preflight, dark_run, dark_user, dark_long, dark_review, dark_ledger_tail.
- Six tool names in `docs/mcp.md`, in table order: dark_preflight, dark_run, dark_user, dark_long, dark_review, dark_ledger_tail.
- `grep -c timeout_seconds specs/mcp-server/spec.md` prints 7.
- Added the `dark ledger-tail` line under `## [Unreleased]` in `CHANGELOG.md`.
- Scope run before the commit: `cd runner && bash verify.sh` prints `verify OK` (610 tests OK, config OK, no-binaries OK).
- Commit `docs(mcp): align the tool table with the server`, carrying this report.

## Out of scope, reported not fixed

- `runner/dark/mcp.py` module docstring still says the ledger tool exists "because the CLI has no command that prints ledger rows"; that clause is stale now that `dark ledger-tail` exists. `mcp.py` is not in the file list.
- `specs/mcp-server/plan.md` "The test file" section says `test_mcp.py` "has seven test classes" and lists seven; the file has eight (`Ledger` is unlisted). Left as is, outside the tool table.
- `docs/mcp.md` shows `serverInfo.version` `0.4.1` in its example while `pyproject.toml` is `0.5.0`.
- Tasks item 6 stays open: the server answers `dark_ledger_tail` itself, so its `ledger-tail --lines=20` argv assertions do not apply.

## Not done / not verified / blocked

NONE

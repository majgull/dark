# Report: 1051-c, tests for `dark_run` and `dark_ledger_tail`, and the docs version

## Terms, once

- **MCP server**: `python3 -m dark mcp`, the program in `runner/dark/mcp.py` that offers tools over standard input and output.
- **tool**: one named operation the server offers, with a JSON Schema naming the arguments it takes; the server offers six.
- **argv**: the list of arguments the server hands to the process it starts for a tool call.
- **ledger**: dark's append-only file of one JSON row per event, `state_dir/ledger.jsonl`; a row's `kind` field names the event.
- **fixture**: a file a test writes in its own directory under `/tmp/fx/`, so the test never touches real state.

## Item 1, tasks item 6

Which of the two it is: the server answers `dark_ledger_tail` itself, reading the ledger. It does not run `dark ledger-tail`. The source shows it three ways: `TOOLS` gives that tool `verb=None`, `_tools_call` routes `verb is None` to `tail_ledger`, and `tail_ledger` opens `host.ledger_path` itself after `config.load_host`. So the `ledger-tail --lines=20` argv assertions in the item text do not apply, and the test asserts the rows the server returns instead.

What I added in `ToolCalls` in `runner/tests/test_mcp.py`:

- `dark_run`: the existing exact-argv assertions stay, `shift --no-push --task-dir=/w/t1 --tier=mid` without optional arguments and the same plus `--arm=a1 --slot=2` with them; added that a `think` argument is refused with `-32602` and `unknown argument: think`, because the tool table has no such argument.
- `dark_ledger_tail`: a fixture ledger under `/tmp/fx/` of five rows of two kinds (`run.start`, `run.end`) and a `DARK_CONF` pointing at it; then two calls. Without arguments it returns all five rows oldest first, each row's `kind` and `n` intact, `isError` false. With `{"kind": "run.end"}` it is `-32602` and `unknown argument: kind`, because the tool's only argument is `n`. Both calls leave the fake dark's call log empty, so neither started a command.

The item text says `dark_run` is covered "with and without `think`" and the ledger tool as `ledger-tail --lines=20` with `--kind=`. The tasks file's own status note says the tool table has no `think`, and the source agrees: `dark_run` takes `task`, `tier`, `arm`, `slot` and `timeout_seconds`, and `dark_ledger_tail` takes only `n` (server default 10, not the CLI's 20). No test can assert those argv lists, so I asserted the server's actual behaviour and the refusal where the named argument does not exist. The CLI `dark ledger-tail` does take `--lines` (default 20) and `--kind`, and tasks item 1 covers it in `runner/tests/test_cli_ledger_tail.py`; the MCP tool does not use that command.

Ticked item 6 in `specs/mcp-server/tasks.md`; no other box changed. I also rewrote the one status-note sentence that said item 6 was open, because it would otherwise contradict the tick.

Acceptance, run before the commit:

    $ cd runner && python3 -m unittest tests.test_mcp.ToolCalls -q
    ----------------------------------------------------------------------
    Ran 8 tests in 5.057s

    OK

Commit `test(mcp): cover dark_run and dark_ledger_tail in ToolCalls`.

## Item 2, the version in `docs/mcp.md`

The example's `serverInfo.version` now reads `0.5.0`, the version in `pyproject.toml`. It was `0.4.1`.

Acceptance:

    $ grep -n 0.4.1 docs/mcp.md
    (no output, exit 1)

    $ cd runner && bash verify.sh
    Ran 611 tests in 179.681s

    OK
    == config
    config OK: 2 models across 1 providers, 7 classes, windows []; host org dark, state /home/blt/.dark
    == no binaries
    no-binaries OK
    verify OK

## Out of scope, reported not fixed

- `runner/dark/mcp.py`'s module docstring says the ledger tool exists "because the CLI has no command that prints ledger rows"; that clause is stale now `dark ledger-tail` exists. `mcp.py` is not in the file list. Already reported by 1051-a.
- `specs/mcp-server/plan.md` says `test_mcp.py` "has seven test classes" and lists seven; the file has eight (`Ledger` is unlisted). Outside the named files.
- `specs/mcp-server/tasks.md` item 6's own text still names `think` and `ledger-tail --lines=20`, and item 7 still names `files`; both are stale against the tool table the status note describes. I changed the tick and the status-note sentence only, as the file was named for one tick.
- No `CHANGELOG.md` line was added: the request forbids editing it, and the existing Unreleased entry already names `docs/mcp.md` and `dark mcp`.

## Not done / not verified / blocked

NONE

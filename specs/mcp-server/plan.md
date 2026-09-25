# Plan: dark as an MCP server

This is the plan for the feature in `spec.md`. A plan says how to build what the spec says; the ordered checklist that carries it out is `tasks.md`. The terms MCP, tool, JSON-RPC 2.0, stdio, dark CLI and ledger are defined in `spec.md` and used here as defined there. Three more: **argv** is the list of arguments a process is started with, **`PATH`** is the list of directories searched for a command, and the **fake dark** is a stand-in executable named `dark`, placed first on `PATH` by a test, that records how it was called and prints what the test tells it to.

## The module

`runner/dark/mcp.py` holds the whole server and imports only the standard library (`json`, `subprocess`, `sys`, `importlib.metadata`), so `dependencies = []` in `pyproject.toml` stays empty. It has five parts.

- `PROTOCOL_VERSION = "2025-06-18"` and the JSON-RPC error codes `-32700`, `-32600`, `-32601` and `-32602` as named constants.
- `TOOLS`, a tuple of six records, one per tool: its name, the dark subcommand it runs or `None` for `dark_ledger_tail` (the server answers that one itself), any fixed options (`--no-push` for `dark_run`, `--arm=review` for `dark_review`), its description, and its arguments, each with a JSON Schema type, a range or a default when it has one, and the CLI option it becomes (`task` becomes `--task-dir` for `dark_run` and `--brief` for `dark_review`, `judge_tier` becomes `--judge-tier`; `timeout_seconds` and `dark_ledger_tail`'s `n` stay with the server). `task` and `tier` are required for `dark_run`, `dark_user`, `dark_long` and `dark_review`. `tools/list` is built from this table alone, so the table is the one place a tool is defined.
- `validate(tool, arguments)` returns `None` or one message: a required argument missing, an argument not in the table, a value of the wrong type (a boolean is not an integer), out of its range, or an empty string where a non-empty one is required, and, for `review_branches`, JSON text that does not hold a list. The caller turns a message into the error `-32602`.
- `build_argv(tool, arguments)` returns the list `[sys.executable, "-m", "dark", subcommand, *fixed, *options]`. A string or integer becomes one item `--option=value`, so a value that starts with a dash cannot be read by the CLI as another option. `timeout_seconds` is the server's own and never becomes an item. `review_branches` is written to a temporary file whose path becomes the `--review-branches` value, and the file is removed after the call.
- `handle(message)` takes one parsed request and returns the response to write, or `None` for a notification. It answers `initialize`, `ping`, `tools/list` and `tools/call`, and any other method with `-32601`. `serve(stdin, stdout)` reads lines, skips blank ones, parses each, calls `handle`, writes one line per response and flushes, and returns 0 at end of input, including when the client closes the pipe.

A tool call starts `build_argv(...)` with `stdin=subprocess.DEVNULL`, standard output and standard error captured and decoded as UTF-8 with replacement, and in its own process group, so a timeout can kill the whole group. Exit 0 gives `isError` false and, when the command wrote to standard error, logs that to the server's standard error. Any other exit gives `isError` true and one text item joining the command's standard output, its standard error and the last line `exit code N`. A call that runs past `timeout_seconds` (default 1800) is sent SIGTERM, then SIGKILL ten seconds later, and its text ends `timed out after N seconds; the command was killed`. An `OSError` while starting gives `isError` true and the text that says the command could not be started. `serverInfo.version` is the installed package version from `importlib.metadata`, or `unknown` when dark runs from a checkout that was not installed.

## The subparser

`runner/dark/__main__.py` gains a subparser `mcp` with no arguments and a function `cmd_mcp(args)` that imports `.mcp` inside the function, so the other commands never load it, and returns `mcp.serve(sys.stdin, sys.stdout)`. It is added to the dispatch table in `main` beside the others. It does not call `_ctx` or `_load`, because the server needs no configuration of its own; the commands it starts read `DARK_CONF` from the environment they inherit.

## Where the CLI has no matching command

Five tools wrap a command that exists: `preflight`, `run` (the `dark shift --task-dir <dir> --tier <id> --no-push` that runs one task through preflight, execution and staging and prints the digest), `user`, `long` and `review`. One does not, and this plan settles it without adding a command the server does not use.

- `dark_run` always passes `--no-push`, so a remote client cannot cause a digest to be published.
- `dark_ledger_tail` is answered by the server itself: it reads `host.ledger_path` through `config.load_host` (no token files, no Gitea, no sandbox) and returns the last `n` rows, 10 by default, oldest first, one JSON object per line, and succeeds even when nothing matched. The CLI command `dark ledger-tail [--lines N] [--kind KIND]` is still built, because `tasks.md` item 1 promised it and an operator can then read the ledger without an MCP client.

## The test file

`runner/tests/test_mcp.py` is the one new test module, run by `verify.sh` through `unittest discover`. It has seven test classes, named as `tasks.md` names them.

- `Framing`, by calling `handle` and `serve` with `io.StringIO`: the `initialize` result and its version string, no response to `notifications/initialized`, `-32601` for an unknown method, `-32700` for a line that is not JSON, `-32600` for an array, blank lines skipped, exit 0 at end of input.
- `ToolList`: six names in the order of the spec, each with an `inputSchema` whose `required` list matches the table in `spec.md`.
- `Validation`: `-32602` for a missing required argument, an argument not in the table, a value of the wrong type or range, and an unknown tool.
- `ToolCalls`, against the fake dark: for every tool, the exact argv the fake recorded and the text returned, including the empty standard input in the child.
- `Failures`: exit code 3, no `dark` on `PATH`, and a value that starts with a dash.
- `Pipes`: the test starts `python3 -m dark mcp` as a real subprocess with `PYTHONPATH` set to `runner` and the fake dark first on `PATH`, writes the four messages of a session to its standard input, reads its standard output line by line, and asserts that every line is JSON, that the ids match, and that closing standard input ends the process with exit code 0.
- `Imports`: parses `runner/dark/mcp.py` and fails on any import that is not the standard library or the `dark` package.

A separate `runner/tests/test_cli_ledger_tail.py` covers `dark ledger-tail` with a ledger written in a temporary directory, in the style of `test_cli_long.py`.

## The fake dark

`make_fake_dark(directory)` in `test_mcp.py` writes an executable file `dark` whose first line is `#!` followed by `sys.executable`, so it needs no `python3` on `PATH`. It is about fifteen lines and does four things, all driven by environment variables the test passes to the server. It reads all of its standard input. It appends one JSON line `{"argv": [...], "stdin": "<what it read>"}` to the file named by `FAKE_DARK_LOG`. It prints the text in `FAKE_DARK_OUT` to standard output, exactly and with no added newline, and the text in `FAKE_DARK_ERR` to standard error. It exits with the integer in `FAKE_DARK_EXIT`, default 0. A test reads the log afterwards, so an assertion is about what the server actually started, not about what it meant to. Because the fake stands in for the whole CLI, no test in `test_mcp.py` touches a ledger, Gitea, a sandbox or a model; the commands themselves are already covered by `test_cli_user.py`, `test_cli_long.py` and the other CLI tests.

## The docs page

`docs/mcp.md` says in plain words what the server is and how to use it, and holds five things: how to start it (`python3 -m dark mcp`, or through ssh as `ssh <runner-host> dark mcp`), a client configuration that starts it that way, a short transcript of `initialize`, `tools/list` and one `tools/call`, the six tools with their arguments as in `spec.md`, and what it does not do (authentication, remote transport, cancellation, a timeout). `README.md` gets one line in its Documentation list. `docs/concepts.md` gains the term "MCP server", defined once, as the constitution asks. `CHANGELOG.md` gets an entry under `## [Unreleased]` in the commit that adds each user-visible command, `dark ledger-tail` and `dark mcp`, and the docs task adds the page and the term to the same entry.

## Constitution check

The rules this feature touches and how each is kept: tests in `runner/tests` and `verify OK` before any push (rules 22 and 23) by every task ending in a passing command; the standard library only and an empty `dependencies` list (rules 32 and 33) by the import list above, which a task checks with a one-line script; the `CHANGELOG.md` entry (rule 27) and the term in `docs/concepts.md` (rule 31) by the docs task; no private hostname in the docs page (rule 40) by using `<runner-host>` and example names only.

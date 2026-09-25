# Spec: dark as an MCP server

This is the spec for one feature. A spec says what to build and why; its plan, `plan.md`, says how, and its tasks, `tasks.md`, is the ordered checklist. Read `AGENTS.md` and `memory/constitution.md` first.

## Terms

- **MCP**: the Model Context Protocol, the open protocol by which an agent (the client) lists and calls the tools a program (the server) offers.
- **tool**: a named operation the server offers, with a JSON Schema that says which arguments it takes.
- **JSON-RPC 2.0**: the message format MCP uses. A request has `jsonrpc`, `id`, `method` and `params` and gets one response with the same `id` holding a `result` or an `error`; a notification has no `id` and gets no response.
- **stdio**: the transport where the server reads one message per line from its standard input and writes one per line to its standard output, so a client only has to start the process and hold its pipes.
- **content item** and **`isError`**: a tool result is a list of content items, here always of type `text`, plus the field `isError`, which says whether the tool failed.
- **dark CLI**: the command `dark`, which is `python3 -m dark` after `pip install .`; its commands are listed by `dark --help`.
- **ledger**: the append-only file of JSON lines where dark writes every shift, run and verdict.
- **task**: one unit of work with a `task.toml`, as defined in `docs/concepts.md`; a **tier** is one model entry in `models.toml`.

## Purpose

dark is driven today by typing `python3 -m dark` commands on the machine that hosts the runner. This feature adds `python3 -m dark mcp`, a Model Context Protocol server over stdio, so that any MCP client can drive the factory without other tooling: it lists six tools. Five run the matching dark CLI command as a subprocess and return what that command printed; the sixth, `dark_ledger_tail`, reads the ledger itself and returns the rows it read. The server adds no behaviour of its own to the factory; everything a run does, records and judges stays where it is.

## The user

The user is an agent on another machine. It has an MCP client and a way to start a process on the runner host and hold that process's standard input and output, such as an ssh command. It has no checkout of dark and no shell it can rely on there. It needs to check that the factory is ready, start a run, read the outcome, and read the ledger, using nothing but tool calls.

## The protocol

The server speaks JSON-RPC 2.0 over stdio, one message per line with no newline inside a message, and it writes nothing else to standard output. It answers `initialize` with the protocol version string `2025-06-18`, the capability `tools` and its own name and version. It accepts the notification `notifications/initialized` and sends nothing back. It answers `tools/list` with the six tools below and `tools/call` with the result of one of them. Any other request gets the JSON-RPC error `-32601`, a line that is not JSON gets `-32700`, a line that is JSON but not one request gets `-32600`, and a tool called with arguments its schema refuses gets `-32602`. A request is answered before the next one is read, and the server ends with exit code 0 when its standard input closes.

## The tools

Each tool takes an object of arguments and returns one content item of type `text`: the standard output of the command it ran, or the ledger rows it read. Every path names a file or directory on the machine where the server runs. Arguments not listed are refused. Arguments in bold are required. The five tools that run a command also take `timeout_seconds`, an integer from 1 to 86400 (default 1800), after which the command is killed and the result says it timed out.

| tool | arguments | command it runs | text it returns |
|---|---|---|---|
| `dark_preflight` | `timeout_seconds` as above | `dark preflight` | one line per check, then `preflight OK` or the refusal line |
| `dark_run` | **`task`** string, **`tier`** string, `arm` string, `slot` integer from 0, `timeout_seconds` as above | `dark shift --no-push` | the digest of the one-task shift and its one-line summary |
| `dark_user` | **`task`** string, **`tier`** string, `timeout_seconds` as above | `dark user` | one JSON line: `run`, `outcome`, `fail_kind`, `detail`, `issue`, `records`, `steps_ok`, `steps_total` |
| `dark_long` | **`task`** string, **`tier`** string, `judge_tier` string, `timeout_seconds` as above | `dark long` | the lines `long: <outcome>` and `judge: <outcome>`, or `judge: none (no branch pushed)` |
| `dark_review` | **`task`** string, **`tier`** string, `review_branches` string, `timeout_seconds` as above | `dark review --arm=review` | one JSON line: `run`, `outcome`, `fail_kind`, `detail`, `issue`, `records` |
| `dark_ledger_tail` | `n` integer from 1 to 200 (default 10) | nothing: the server reads the ledger itself | the last rows of the ledger, oldest first, one JSON object per line |

For `dark_run`, `task` is one task directory, and the shift never pushes its digest; publishing it stays an operator action. `arm` is the run's arm name and `slot` its sandbox slot. For `dark_review`, `task` is the path of a Markdown file holding the review's instructions and becomes `--brief`, and `review_branches` is JSON text holding a list of `{name, url, branch}` objects, one repository each, which the server writes to a temporary file for `--review-branches`. For `dark_ledger_tail`, `n` is how many rows the server prints.

A tool call is a success when the command exits 0: `isError` is false and the content item holds the command's standard output, while its standard error is logged to the server's own standard error. When the command exits with another code, `isError` is true and the one content item holds its standard output, its standard error and a last line `exit code N`. When the command runs past `timeout_seconds` it is killed and the text ends `timed out after N seconds; the command was killed`. When the `dark` command cannot be started the result has `isError` true and one content item that says so. The command's standard input is empty, so it cannot read the protocol stream.

## Out of scope

- Authentication: whoever can start the server can call every tool, and access control is that of whatever carries the stdio, such as an ssh key.
- Remote transport: no HTTP, no server-sent events, no listening socket. The server is a process that talks over its standard input and output and nothing else.
- Progress notifications, cancellation, and answering a second request while a long call, such as `dark_long`, is still running.
- MCP resources, prompts and logging, and requests from the server to the client.
- JSON-RPC batches, which the `2025-06-18` protocol does not have.
- A `--conf` given to `dark mcp`: the commands the server runs read their configuration from `DARK_CONF` in the environment, as `docs/docker.md` describes.
- Any command beyond the six, such as `stage`, `void`, `abort`, or a shift over several tasks.

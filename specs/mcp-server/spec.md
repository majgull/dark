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

dark is driven today by typing `python3 -m dark` commands on the machine that hosts the runner. This feature adds `python3 -m dark mcp`, a Model Context Protocol server over stdio, so that any MCP client can drive the factory without other tooling: it lists six tools and each tool runs the matching dark CLI command as a subprocess and returns what that command printed. The server adds no behaviour of its own to the factory. It is a door to the commands that already exist, plus one read-only command, `dark ledger-tail`, which the ledger tool needs because the CLI has no command that prints ledger rows; everything a run does, records and judges stays where it is.

## The user

The user is an agent on another machine. It has an MCP client and a way to start a process on the runner host and hold that process's standard input and output, such as an ssh command. It has no checkout of dark and no shell it can rely on there. It needs to check that the factory is ready, start a run, read the outcome, and read the ledger, using nothing but tool calls.

## The protocol

The server speaks JSON-RPC 2.0 over stdio, one message per line with no newline inside a message, and it writes nothing else to standard output. It answers `initialize` with the protocol version string `2025-06-18`, the capability `tools` and its own name and version. It accepts the notification `notifications/initialized` and sends nothing back. It answers `tools/list` with the six tools below and `tools/call` with the result of one of them. Any other request gets the JSON-RPC error `-32601`, a line that is not JSON gets `-32700`, a line that is JSON but not one request gets `-32600`, and a tool called with arguments its schema refuses gets `-32602`. A request is answered before the next one is read, and the server ends with exit code 0 when its standard input closes.

## The tools

Each tool takes an object of arguments and returns one content item of type `text` holding the standard output of the command it ran. Every path names a file or directory on the machine where the server runs. Arguments not listed are refused. Arguments in bold are required.

| tool | arguments | command it runs | text it returns |
|---|---|---|---|
| `dark_preflight` | `no_vm` boolean, `no_model` boolean | `dark preflight` | one line per check, then `preflight OK` or the refusal line |
| `dark_run` | **`task_dir`** string, **`tier`** string, `think` one of `none`, `low`, `medium`, `high` | `dark shift --task-dir --tier --no-push` | the digest of the one-task shift and its one-line summary |
| `dark_user` | **`task`** string, **`tier`** string, `arm` string, `shift` string, `think` as above, `slot` integer | `dark user` | one JSON line: `run`, `outcome`, `fail_kind`, `detail`, `issue`, `records`, `steps_ok`, `steps_total` |
| `dark_long` | **`task`** string, **`tier`** string, `judge_tier` string, `arm` string, `slot` integer | `dark long` | the lines `long: <outcome>` and `judge: <outcome>`, or `judge: none (no branch pushed)` |
| `dark_review` | **`brief`** string, **`tier`** string, **`arm`** string, `files` array of strings, `shift` string, `think` as above, `frozen` string, `review_branches` string | `dark review` | one JSON line: `run`, `outcome`, `fail_kind`, `detail`, `issue`, `records` |
| `dark_ledger_tail` | `lines` integer from 1 to 200 (default 20), `kind` string | `dark ledger-tail` | the last rows of the ledger, oldest first, one JSON object per line |

For `dark_run`, `task_dir` is one task directory, and the shift never pushes its digest; publishing it stays an operator action. For `dark_review`, `brief` is the path of a Markdown file holding the review's instructions, `files` are paths staged read-only for the session, `frozen` is a pinned envelope file and `review_branches` is the path of a JSON list of `{name, url, branch}`. For `dark_ledger_tail`, `kind` keeps only rows of that kind, such as `run.end`.

A tool call is a success when the command exits 0. When it exits with another code the result has `isError` set to true, its first content item is still the standard output, and a second content item says `exit code N`. When the `dark` command cannot be started the result has `isError` true and one content item that says so. The command's standard input is empty, so it cannot read the protocol stream, and its standard error is passed through to the server's own standard error.

## Out of scope

- Authentication: whoever can start the server can call every tool, and access control is that of whatever carries the stdio, such as an ssh key.
- Remote transport: no HTTP, no server-sent events, no listening socket. The server is a process that talks over its standard input and output and nothing else.
- Progress notifications, cancellation, timeouts on a tool call, and answering a second request while a long call, such as `dark_long`, is still running.
- MCP resources, prompts and logging, and requests from the server to the client.
- JSON-RPC batches, which the `2025-06-18` protocol does not have.
- A `--conf` given to `dark mcp`: the commands the server runs read their configuration from `DARK_CONF` in the environment, as `docs/docker.md` describes.
- Any command beyond the six, such as `stage`, `void`, `abort`, or a shift over several tasks.

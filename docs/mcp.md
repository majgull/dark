# dark as an MCP server

**MCP** (Model Context Protocol) is the open protocol an agent uses to list and call the tools a program offers. A **tool** is a named operation with a **JSON Schema**, a JSON description of the arguments it takes. Messages are **JSON-RPC 2.0**: a request has an `id`, a `method` and `params` and gets one response with the same `id`, holding a `result` or an `error`, and a notification has no `id` and gets no reply. The **stdio transport** is one such message per line on the server's standard input and output, with nothing else on standard output. `python3 -m dark mcp` is a server that offers six tools, so an agent with an MCP client can drive dark with no checkout and no shell. It adds no behaviour of its own: five tools run the matching `python3 -m dark` command as a subprocess and return what it printed, and the sixth reads the **ledger**, the append-only file of JSON lines where dark writes every shift, run and verdict. dark's own terms (shift, run, arm, slot, digest) are defined in `docs/concepts.md`.

## Start it

`python3 -m dark mcp`, or `dark mcp` after `pip install .`, waits for messages on stdin and exits 0 when stdin closes. A client on another machine starts it over ssh, as `ssh <runner-host> dark mcp`.

## Register it

Claude Code documents the syntax `claude mcp add [options] <name> -- <command> [args...]` (fetched from https://code.claude.com/docs/en/mcp, where https://docs.claude.com/en/docs/claude-code/mcp redirects). For dark:

    claude mcp add dark -- python3 -m dark mcp

Codex documents `codex mcp add <server-name> --env VAR1=VALUE1 --env VAR2=VALUE2 -- <stdio server-command>` (fetched from https://developers.openai.com/codex/mcp, which redirects to https://learn.chatgpt.com/docs/extend/mcp). For dark, `codex mcp add dark -- python3 -m dark mcp`, or in `~/.codex/config.toml`:

    [mcp_servers.dark]
    command = "python3"
    args = ["-m", "dark", "mcp"]

pi: NOT FOUND. Neither page above names pi, and the README of the pi package on npm does not mention MCP.

## Environment

The server gives its own environment to every command it starts, so set the CLI's `DARK_*` variables where the server starts. `DARK_CONF` is the directory holding `models.toml`, `budgets.toml` and `host.toml`; it defaults to the files beside the package, and `dark mcp` ignores `--conf`. Each `host.toml` key has a `DARK_*` variable named in `runner/host.toml` and described in `docs/docker.md`, such as `DARK_STATE`, `DARK_BACKEND`, `DARK_GITEA_URL` and `DARK_ADMIN_TOKEN_FILE`.

## The tools

A **task** is one unit of work with a `task.toml`, and a **tier** is one model entry in `models.toml`. Every path names a file on the machine where the server runs. Arguments in bold are required; the others may be left out.

| tool | arguments | runs |
|---|---|---|
| `dark_preflight` | none | `dark preflight` |
| `dark_run` | **`task`** (a task directory), **`tier`**, `arm`, `slot` | `dark shift --task-dir --tier --no-push`, so the digest is never pushed |
| `dark_user` | **`task`** (a `task.toml` or its directory), **`tier`** | `dark user` |
| `dark_long` | **`task`**, **`tier`**, `judge_tier` | `dark long` |
| `dark_review` | **`task`** (a Markdown file of instructions), **`tier`**, `review_branches` (JSON text, a list of `{name, url, branch}`) | `dark review --arm=review`, with `task` as `--brief` |
| `dark_ledger_tail` | `n` (1 to 200, default 10) | nothing: the server reads the last `n` ledger rows, oldest first |

The five tools that run a command also take `timeout_seconds` (default 1800), after which the command is killed and reported. A result is one text item holding the command's standard output. When the command exits non-zero the result has `isError`, the flag that says the tool failed, set to true, and the text ends with `exit code N`, after any standard error. A bad argument is the JSON-RPC error `-32602`, an unknown method `-32601` and a line that is not JSON `-32700`.

## Example

`->` is what the client writes and `<-` what the server answers; the ledger holds one example row.

    -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"0"}}}
    <- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"dark","version":"0.4.1"}}}
    -> {"jsonrpc":"2.0","method":"notifications/initialized"}
    -> {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"dark_ledger_tail","arguments":{"n":1}}}
    <- {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"kind\":\"run.end\",\"run\":\"hello-1\",\"task\":\"hello\",\"tier\":\"mid\",\"outcome\":\"pass\"}\n"}],"isError":false}}

## Not done

There is no authentication: whoever can start the server can call every tool, so access is that of whatever carries the stdio, such as an ssh key. There is no HTTP transport, no cancellation or progress, and no resources or prompts. A call is answered before the next line is read, so a long `dark_long` blocks the session until it ends or times out.

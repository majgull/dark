# Report: 1056-a, the OTLP gate and one child span per user-arm browser action

Date: 2026-09-25.

## Terms, once

- **OTLP**: OpenTelemetry's wire format; a collector takes spans as JSON at `<endpoint>/v1/traces`, whose default port is 4318. A **collector** is the program that receives those spans.
- **`back` and `front`**: the two networks in `compose.yaml`. `back` is `internal: true`, so no container on it has a route off the host; `front` is a normal compose network.
- **`model-gate`**: the existing caddy reverse proxy on `back` and `front` that forwards its port 11434 to `DARK_MODEL_UPSTREAM`, the one model hop a sandbox may speak to.
- **`otlp-gate`**: the second reverse proxy added here, built like `model-gate`, on `back` and `front`, forwarding its port 4318 to `DARK_OTLP_UPSTREAM`.
- **runner**: dark's coordinator container, attached to `back` only; its one way out today was `model-gate`.
- **user-arm run**: a run whose arm drives a browser step by step; its records hold `stream.jsonl` rows shaped `{"action": {...}, "call": N, "result": {...}, "snapshot_chars": N, "step": N, "url": "..."}`, one row per browser action.

## Item 1, the `otlp-gate` service

- `compose.yaml`: added an `otlp-gate` service built like `model-gate`, the same pinned image `caddy:2.11.4-alpine`, the same `entrypoint: ["/bin/sh", "-c"]` shape, the networks `back` and `front`, and the command `caddy reverse-proxy --from :4318 --to "$DARK_OTLP_UPSTREAM"`. The environment default is `DARK_OTLP_UPSTREAM=http://127.0.0.1:9`, a port inside the gate container that refuses at once, so an unset collector costs a run nothing. Updated the file header to name the second gate and the shared `front` paragraph to say `the gates`.
- `runner/host.toml`: the `otlp_endpoint` comment now names the compose `otlp-gate` service and `DARK_OTLP_UPSTREAM`, and keeps `http://localhost:4318` for a bare host.
- `docs/docker.md`, the Traces section near line 317: the same instruction, and the user-arm sentence updated for item 2.
- No entry was added to `CHANGELOG.md`; the merge adds it, as the brief says.

### Acceptance

`docker compose config -q` exits 0:

```
$ docker compose config -q
$ echo $?
0
```

Then, with a stdlib HTTP server on the host at port 42631, bound to `0.0.0.0`, and `DARK_OTLP_UPSTREAM=http://172.18.0.1:42631` (the `dark1056_front` bridge gateway), only `otlp-gate` was brought up as project `dark1056`. The POST from a container on `dark1056_back` returned 501, a non-000 code, and the host server logged the request. The server is `python3 -m http.server`, which answers POST with 501; that 501 is the code the gate passed back.

```
$ docker run --rm --network dark1056_back curlimages/curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' http://otlp-gate:4318/v1/traces
501
```

The server log, at `/tmp/fx/http.log` (client `172.18.0.2` is the gate's `front` address):

```
172.18.0.2 - - [25/Sep/2026 23:37:12] code 501, message Unsupported method ('POST')
172.18.0.2 - - [25/Sep/2026 23:37:12] "POST /v1/traces HTTP/1.1" 501 -
```

`docker compose -p dark1056 down` removed the container and both networks:

```
 Container dark1056-otlp-gate-1 Removed 
 Network dark1056_back Removing 
 Network dark1056_front Removing 
 Network dark1056_front Removed 
 Network dark1056_back Removed 
```

The host server, PID 1330463, was stopped with `kill`, and no `dark1056` container remains.

## Item 2, child spans for a user-arm run

- `runner/dark/otel.py`: `tool_calls()` now also reads a user-arm row, one that carries `action` and `step`, and returns it as `{"id": None, "name": "browser.<do>", "args": <action>, "result": <row result>, "step": <step>, "start_ms": ..., "end_ms": ...}`. Pi rows are unchanged, and stream order is kept.
- `build_body()` adds `dark.step` to a call that carries a step, and, since those rows carry no time of their own, spaces their child spans evenly across the parent span, in stream order; a user row that does carry `start_ms`/`end_ms` keeps them. The code comment says which. The span id for a call with no id is derived from its position, so a user call still has a stable span id, and `gen_ai.tool.call.id` is left out because a user row has no tool call id.
- `runner/tests/test_otel_user.py` builds the body from four user-arm rows (two steps, one verdict each) and asserts four child spans named `execute_tool browser.click`, `execute_tool browser.verdict`, `execute_tool browser.fill`, `execute_tool browser.verdict`, with `gen_ai.tool.call.arguments` equal to the action JSON, `gen_ai.tool.call.result` equal to the result JSON, `dark.step` equal to the step, no `gen_ai.tool.call.id`, and times spaced evenly across the parent. A fifth test checks that a row carrying `start_ms`/`end_ms` keeps them.

Scope run before the item 2 commit: `cd runner && python3 -m unittest tests.test_otel_user tests.test_otel -q` prints OK (27 tests), and `cd runner && bash verify.sh` prints `verify OK` (614 tests OK, config OK, no-binaries OK).

## Scope runs before the commits

- Before the item 2 commit, `cd runner && bash verify.sh` printed `verify OK` (614 tests).
- Before the item 1 commit, `cd runner && bash verify.sh` printed `verify OK` (614 tests). The first attempt failed with `OSError: [Errno 28] No space left on device` in `tempfile.mkdtemp`, and a second attempt with `TMPDIR=/var/tmp/dark-t1056-tmp` printed `verify OK`. See the note below.

## Out of scope, reported not fixed

- The shared `/tmp` (a 16 GiB tmpfs) was at 100 percent of its inodes during this session, from other sessions' files, so `tempfile.mkdtemp()` raised `OSError: [Errno 28] No space left on device` and the first item 1 verify run failed with 115 errors. Re-running the gate with `TMPDIR=/var/tmp/dark-t1056-tmp` printed `verify OK`. Nothing under another session's `/tmp` entries was touched.
- One intermediate verify run under load had 1 failure and 37 errors from other suites, then a repeat run was clean; those suites are timing sensitive and were not in this item's scope.

## Not done / not verified / blocked

NONE

"""dark/otel.py: one run as OpenTelemetry spans, stdlib only.

A span is one timed operation with a name, start and end times and key-value
attributes; a trace is a span with the spans nested under it. A finished run
becomes one parent span, `invoke_agent <task>`, with one child span,
`execute_tool <tool>`, per tool call the run's session made, named and
attributed after the OpenTelemetry GenAI semantic conventions
(gen-ai-agent-spans.md and gen-ai-spans.md), and is posted as OTLP/HTTP JSON
to <endpoint>/v1/traces. Best effort like notify.py: an export that fails is
logged once and never raised, so a collector that is down cannot change a
run's outcome or hold up a shift.

The rows are what the runner already keeps. The parent comes from the run.end
row (dark/ledger.py); a field that row does not carry is left out. The tool
calls come from the session's stream.jsonl (dark/session.py), the JSON lines
pi writes. One call is the rows that share a toolCallId: the assistant
message that asked for it (toolCall content part, and that message's
timestamp), tool_execution_start (toolName, args), tool_execution_end
(result, isError) and the toolResult message (its timestamp is the moment the
tool finished). tool_execution_start and tool_execution_end carry no time of
their own, so a call starts at the timestamp of the assistant message that
asked for it, which includes the model's time to write the call.
"""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "dark"
MAX_TEXT = 2000           # characters of a tool call's arguments and of its result kept on its span
STREAM_CAP = 16 << 20     # bytes of stream.jsonl read back from the records repo
INTERNAL = 1              # OTLP SpanKind: the agent and its tools run inside the sandbox


def _ids(run):
    """(trace id, parent span id, child span id of call i): hex, derived from the
    run id so that exporting one run twice lands on the same trace."""
    def h(nbytes, *parts):
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:nbytes * 2]
    return h(16, "trace", run), h(8, "run", run), lambda i, call: h(8, "tool", run, str(i), call)


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ns(seconds):
    return round(seconds * 1e6) * 1000


def _ms_ns(ms):
    return int(ms) * 1_000_000 if _number(ms) else None


def _attrs(pairs):
    """OTLP key/value list. None means the row did not carry the field: no attribute."""
    out = []
    for key, v in pairs:
        if v is None:
            continue
        if isinstance(v, bool):
            v = {"boolValue": v}
        elif isinstance(v, int):
            v = {"intValue": str(v)}  # OTLP JSON writes 64-bit integers as strings
        elif isinstance(v, float):
            v = {"doubleValue": v}
        else:
            v = {"stringValue": str(v)}
        out.append({"key": key, "value": v})
    return out


def _text(v):
    """A tool call's arguments or result as the string a span keeps: JSON, cut at MAX_TEXT."""
    if v is None:
        return None
    return (v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False))[:MAX_TEXT]


def tool_calls(stream_rows):
    """One dict per tool call that ran, in stream order: id, name, args, result,
    start_ms, end_ms (None where the stream does not say). A call is found by its
    tool_execution_start or tool_execution_end row, the same rows session.py counts
    as tool_calls; rows without a toolCallId are not calls."""
    asked, ended, calls = {}, {}, {}
    for row in stream_rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        if kind == "message_end" and msg.get("role") == "assistant":
            for part in msg.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "toolCall" and part.get("id"):
                    asked[part["id"]] = msg.get("timestamp")
        elif kind == "message_end" and msg.get("role") == "toolResult" and msg.get("toolCallId"):
            ended[msg["toolCallId"]] = msg.get("timestamp")
        elif kind in ("tool_execution_start", "tool_execution_end") and row.get("toolCallId"):
            call = calls.setdefault(row["toolCallId"], {"id": row["toolCallId"]})
            call["name"] = call.get("name") or row.get("toolName")
            if kind == "tool_execution_start":
                call["args"] = row.get("args")
            else:
                call["result"] = row.get("result")
    return [{"name": None, "args": None, "result": None, **c,
             "start_ms": asked.get(c["id"]), "end_ms": ended.get(c["id"])} for c in calls.values()]


def build_body(run_row, stream_rows):
    """The OTLP/HTTP JSON body (the resourceSpans shape of opentelemetry-proto's
    examples/trace.json) for one run.end row and the run's stream rows."""
    run = str(run_row.get("run"))
    trace, parent_id, child_id = _ids(run)
    end = _ns(run_row["ts"] if _number(run_row.get("ts")) else time.time())
    took = run_row["wall_seconds"] if _number(run_row.get("wall_seconds")) else run_row.get("seconds")
    start = end - _ns(took) if _number(took) else end
    children = []
    for i, call in enumerate(tool_calls(stream_rows)):
        c_start = _ms_ns(call["start_ms"])
        c_end = _ms_ns(call["end_ms"])
        c_start = start if c_start is None else c_start
        c_end = max(end if c_end is None else c_end, c_start)
        children.append({
            "traceId": trace, "spanId": child_id(i, call["id"]), "parentSpanId": parent_id,
            "name": f"execute_tool {call['name']}" if call["name"] else "execute_tool",
            "kind": INTERNAL, "startTimeUnixNano": str(c_start), "endTimeUnixNano": str(c_end),
            "attributes": _attrs([("gen_ai.operation.name", "execute_tool"),
                                  ("gen_ai.tool.name", call["name"]),
                                  ("gen_ai.tool.call.id", call["id"]),
                                  ("gen_ai.tool.call.arguments", _text(call["args"])),
                                  ("gen_ai.tool.call.result", _text(call["result"]))])})
    # the tool times are the sandbox's clock and the run.end time the runner's: the
    # parent widens to hold its children rather than exporting a child outside it
    start = min([start] + [int(c["startTimeUnixNano"]) for c in children])
    end = max([end] + [int(c["endTimeUnixNano"]) for c in children])
    parent = {
        "traceId": trace, "spanId": parent_id,
        "name": f"invoke_agent {run_row['task']}" if run_row.get("task") else "invoke_agent",
        "kind": INTERNAL, "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
        "attributes": _attrs([("gen_ai.operation.name", "invoke_agent"),
                              ("gen_ai.agent.name", run_row.get("arm")),
                              ("gen_ai.request.model", run_row.get("tier")),
                              ("gen_ai.conversation.id", run_row.get("run")),
                              ("dark.outcome", run_row.get("outcome")),
                              ("dark.calls", run_row.get("calls")),
                              ("dark.seconds", run_row.get("seconds")),
                              ("dark.class", run_row.get("cls"))])}
    return {"resourceSpans": [{
        "resource": {"attributes": _attrs([("service.name", SERVICE)])},
        "scopeSpans": [{"scope": {"name": "dark.otel"}, "spans": [parent, *children]}]}]}


def _failure(e):
    """`e` as the line a log keeps. An HTTPError still holds its response's
    connection open; it is closed here."""
    if isinstance(e, urllib.error.HTTPError):
        e.close()
    return str(e)


def export_run(endpoint, run_row, stream_rows, timeout=5, log=print):
    """Post one run to <endpoint>/v1/traces; True when the collector took it. Any
    failure is logged once and never raised, and the call is back inside `timeout`
    seconds whatever the collector does: urlopen's timeout is per socket operation
    and does not cover name resolution, so the post runs on a thread that is
    abandoned at the deadline."""
    if not endpoint:
        return False
    url = endpoint.rstrip("/") + "/v1/traces"
    failed = []

    def post():
        try:
            req = urllib.request.Request(
                url, method="POST", data=json.dumps(build_body(run_row, stream_rows)).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
        except Exception as e:  # noqa: BLE001 (a trace must never touch a run)
            failed.append(_failure(e))

    worker = threading.Thread(target=post, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        log(f"otlp export to {url} gave up after {timeout}s")
        return False
    if failed:
        log(f"otlp export to {url} failed: {failed[0]}")
        return False
    return True


def fetch_stream(gitea, records, timeout=5, log=print):
    """The rows of stream.jsonl a run pushed to its records repository. `records` is
    run.end's field of that name, dark-records/<shift>/<run>: the stream is
    <run>/stream.jsonl on main of dark-records/<shift>, read through Gitea's raw
    file endpoint. [] when the run pushed none (a killed run, a failed push) or it
    cannot be read; a torn last line is skipped like the ledger's."""
    if not records or str(records).startswith("PUSH FAILED"):
        return []
    try:
        owner, repo, run = str(records).split("/", 2)
        url = "{}/api/v1/repos/{}/{}/raw/{}?ref=main".format(
            gitea.url, *(urllib.parse.quote(p, safe="") for p in (owner, repo)),
            urllib.parse.quote(f"{run}/stream.jsonl"))
        req = urllib.request.Request(url, headers={"Authorization": f"token {gitea.token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(STREAM_CAP).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 (the trace goes without its tool spans)
        log(f"otlp: stream of {records} not read: {_failure(e)}")
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows

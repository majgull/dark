"""OpenTelemetry export tests: one run in, one OTLP/HTTP JSON body out.

The collector is a stdlib HTTP server on 127.0.0.1 that records what it is
sent. The stream rows are built in the shape pi writes them, and once from the
real capture in fixtures/pi-stream-tools.jsonl.
"""
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest import mock

from dark import __main__ as M
from dark import config, otel
from dark import ledger as L
from dark import run as R

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.dirname(HERE)

T_END = 1788576830.5  # the run.end row's ts: epoch seconds
RUN_ROW = {"v": 2, "ts": T_END, "iso": "2026-09-05T02:53:50+00:00", "kind": "run.end", "task": "hello",
           "run": "hello-20260905-025327", "cls": "additive", "tier": "local-a", "outcome": "pass",
           "seconds": 40, "wall_seconds": 45, "calls": 3, "arm": "session"}
START_NS, END_NS = 1788576785500000000, 1788576830500000000


def call_rows(cid, name, args, text, asked_ms, done_ms, error=False):
    """The rows pi writes for one tool call that ran."""
    body = [{"type": "text", "text": text}]
    return [
        {"type": "message_end", "message": {"role": "assistant", "timestamp": asked_ms, "content": [
            {"type": "toolCall", "id": cid, "name": name, "arguments": args}]}},
        {"type": "tool_execution_start", "toolCallId": cid, "toolName": name, "args": args},
        {"type": "tool_execution_end", "toolCallId": cid, "toolName": name,
         "result": {"content": body}, "isError": error},
        {"type": "message_end", "message": {"role": "toolResult", "toolCallId": cid, "toolName": name,
                                            "content": body, "isError": error, "timestamp": done_ms}},
    ]


def three_calls():
    rows = []
    rows += call_rows("call_a", "write", {"path": "hello.txt", "content": "hello"}, "wrote 5 bytes",
                      1788576807794, 1788576808803)
    rows += call_rows("call_b", "read", {"path": "hello.txt"}, "hello", 1788576808804, 1788576810190)
    rows += call_rows("call_c", "bash", {"command": "cat " + "x" * 5000}, "y" * 5000,
                      1788576810191, 1788576812000)
    return rows


class Collector:
    """A stdlib HTTP server on 127.0.0.1: records every POST body, and serves one stream file."""

    def __init__(self, status=200, stream=""):
        self.status = status
        self.stream = stream
        self.posts = []   # (path, content type, parsed body)
        self.gets = []    # (path with query, authorization)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.posts.append((self.path, self.headers.get("Content-Type"), json.loads(self.rfile.read(n))))
                self.reply(outer.status, b"{}")

            def do_GET(self):
                outer.gets.append((self.path, self.headers.get("Authorization")))
                self.reply(200, outer.stream.encode())

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def spans_of(body):
    (resource,) = body["resourceSpans"]
    (scope,) = resource["scopeSpans"]
    return scope["spans"]


def attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


class Export(unittest.TestCase):
    def setUp(self):
        self.collector = Collector()
        self.logged = []

    def tearDown(self):
        self.collector.close()

    def export(self, rows, run_row=RUN_ROW, **kw):
        ok = otel.export_run(self.collector.url, run_row, rows, log=self.logged.append, **kw)
        return ok, self.collector.posts[-1][2] if self.collector.posts else None

    def test_one_run_and_three_tool_calls(self):
        ok, body = self.export(three_calls())
        self.assertTrue(ok)
        self.assertEqual(self.logged, [])
        path, ctype, _ = self.collector.posts[0]
        self.assertEqual((path, ctype), ("/v1/traces", "application/json"))
        self.assertEqual(len(body["resourceSpans"]), 1)
        self.assertEqual(body["resourceSpans"][0]["resource"]["attributes"],
                         [{"key": "service.name", "value": {"stringValue": "dark"}}])
        spans = spans_of(body)
        parents = [s for s in spans if s["name"].startswith("invoke_agent")]
        tools = [s for s in spans if s["name"].startswith("execute_tool")]
        self.assertEqual((len(spans), len(parents), len(tools)), (4, 1, 3))

        (parent,) = parents
        self.assertEqual(parent["name"], "invoke_agent hello")
        self.assertRegex(parent["traceId"], r"^[0-9a-f]{32}$")
        self.assertRegex(parent["spanId"], r"^[0-9a-f]{16}$")
        self.assertNotIn("parentSpanId", parent)
        a = attrs(parent)
        self.assertEqual({k: a[k] for k in a if k.startswith("gen_ai.")}, {
            "gen_ai.operation.name": {"stringValue": "invoke_agent"},
            "gen_ai.agent.name": {"stringValue": "session"},
            "gen_ai.request.model": {"stringValue": "local-a"},
            "gen_ai.conversation.id": {"stringValue": "hello-20260905-025327"}})
        self.assertEqual({k: a[k] for k in a if k.startswith("dark.")}, {
            "dark.outcome": {"stringValue": "pass"}, "dark.calls": {"intValue": "3"},
            "dark.seconds": {"intValue": "40"}, "dark.class": {"stringValue": "additive"}})
        self.assertEqual((parent["startTimeUnixNano"], parent["endTimeUnixNano"]), (str(START_NS), str(END_NS)))

        for span, (cid, name, asked, done) in zip(tools, (("call_a", "write", 1788576807794, 1788576808803),
                                                          ("call_b", "read", 1788576808804, 1788576810190),
                                                          ("call_c", "bash", 1788576810191, 1788576812000))):
            self.assertEqual(span["name"], f"execute_tool {name}")
            self.assertEqual((span["traceId"], span["parentSpanId"]), (parent["traceId"], parent["spanId"]))
            self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
            for stamp in (span["startTimeUnixNano"], span["endTimeUnixNano"]):
                self.assertRegex(stamp, r"^\d{19}$")
            self.assertEqual((span["startTimeUnixNano"], span["endTimeUnixNano"]),
                             (str(asked * 1_000_000), str(done * 1_000_000)))
            self.assertLessEqual(int(parent["startTimeUnixNano"]), int(span["startTimeUnixNano"]))
            self.assertLessEqual(int(span["endTimeUnixNano"]), int(parent["endTimeUnixNano"]))
            t = attrs(span)
            self.assertEqual(sorted(t), ["gen_ai.operation.name", "gen_ai.tool.call.arguments",
                                         "gen_ai.tool.call.id", "gen_ai.tool.call.result", "gen_ai.tool.name"])
            self.assertEqual(t["gen_ai.operation.name"], {"stringValue": "execute_tool"})
            self.assertEqual(t["gen_ai.tool.name"], {"stringValue": name})
            self.assertEqual(t["gen_ai.tool.call.id"], {"stringValue": cid})
        self.assertEqual(len({s["spanId"] for s in spans}), 4)

    def test_arguments_and_result_are_cut_at_2000_characters(self):
        _, body = self.export(three_calls())
        t = attrs(spans_of(body)[3])  # the bash call
        self.assertEqual(len(t["gen_ai.tool.call.arguments"]["stringValue"]), 2000)
        self.assertEqual(len(t["gen_ai.tool.call.result"]["stringValue"]), 2000)
        short = attrs(spans_of(body)[1])  # the write call: kept whole, as JSON
        self.assertEqual(json.loads(short["gen_ai.tool.call.arguments"]["stringValue"]),
                         {"path": "hello.txt", "content": "hello"})
        self.assertEqual(json.loads(short["gen_ai.tool.call.result"]["stringValue"]),
                         {"content": [{"type": "text", "text": "wrote 5 bytes"}]})

    def test_the_real_pi_stream(self):
        with open(os.path.join(HERE, "fixtures", "pi-stream-tools.jsonl")) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        _, body = self.export(rows)
        tools = [s for s in spans_of(body) if s["name"].startswith("execute_tool")]
        self.assertEqual([s["name"] for s in tools], ["execute_tool write", "execute_tool read"])
        self.assertEqual([attrs(s)["gen_ai.tool.call.id"]["stringValue"] for s in tools],
                         ["call_n6x4gerj", "call_5469pp6j"])
        self.assertEqual([(s["startTimeUnixNano"], s["endTimeUnixNano"]) for s in tools],
                         [("1788576807794000000", "1788576808803000000"),
                          ("1788576808804000000", "1788576810190000000")])
        self.assertEqual(json.loads(attrs(tools[0])["gen_ai.tool.call.arguments"]["stringValue"]),
                         {"path": "hello.txt", "content": "hello"})

    def test_a_run_with_no_stream_is_one_span(self):
        _, body = self.export([])
        (span,) = spans_of(body)
        self.assertEqual(span["name"], "invoke_agent hello")

    def test_rows_that_do_not_carry_a_field_leave_the_attribute_out(self):
        row = {k: v for k, v in RUN_ROW.items() if k not in ("arm", "wall_seconds")}
        bare = [{"type": "tool_execution_end", "toolCallId": f"c{i}", "toolName": "bash", "result": "ok"}
                for i in range(3)]
        _, body = self.export(bare, run_row=row)
        parent, *tools = spans_of(body)
        self.assertNotIn("gen_ai.agent.name", attrs(parent))
        self.assertEqual(len(tools), 3)
        for span in tools:
            self.assertEqual(span["parentSpanId"], parent["spanId"])
            self.assertNotIn("gen_ai.tool.call.arguments", attrs(span))
            self.assertEqual(attrs(span)["gen_ai.tool.call.result"], {"stringValue": "ok"})
            # no time in the rows: the call takes the run's window, seconds standing in for wall_seconds
            self.assertEqual((span["startTimeUnixNano"], span["endTimeUnixNano"]),
                             (str(END_NS - 40_000_000_000), str(END_NS)))

    def test_a_child_outside_the_run_widens_the_parent(self):
        early = call_rows("c", "bash", {}, "ok", 1788576000000, 1788576001000)
        _, body = self.export(early)
        parent, child = spans_of(body)
        self.assertEqual(parent["startTimeUnixNano"], child["startTimeUnixNano"])
        self.assertEqual(parent["endTimeUnixNano"], str(END_NS))

    def test_the_same_run_exports_to_the_same_trace(self):
        self.export(three_calls())
        self.export(three_calls())
        first, second = (spans_of(p[2]) for p in self.collector.posts)
        self.assertEqual([(s["traceId"], s["spanId"]) for s in first], [(s["traceId"], s["spanId"]) for s in second])

    def test_a_trailing_slash_on_the_endpoint(self):
        otel.export_run(self.collector.url + "/", RUN_ROW, [], log=self.logged.append)
        self.assertEqual(self.collector.posts[0][0], "/v1/traces")


class Failures(unittest.TestCase):
    def test_a_refused_endpoint_neither_raises_nor_blocks_past_the_timeout(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        logged = []
        t0 = time.monotonic()
        ok = otel.export_run(f"http://127.0.0.1:{port}", RUN_ROW, three_calls(), timeout=2, log=logged.append)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(len(logged), 1)
        self.assertIn("failed", logged[0])

    def test_a_collector_that_never_answers_is_given_up_on_at_the_timeout(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        logged = []
        t0 = time.monotonic()
        ok = otel.export_run(f"http://127.0.0.1:{s.getsockname()[1]}", RUN_ROW, [], timeout=1, log=logged.append)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(len(logged), 1)

    def test_a_collector_that_answers_500_is_logged_once(self):
        c = Collector(status=500)
        self.addCleanup(c.close)
        logged = []
        self.assertFalse(otel.export_run(c.url, RUN_ROW, [], log=logged.append))
        self.assertEqual(len(logged), 1)
        self.assertIn("500", logged[0])

    def test_a_row_that_cannot_be_built_is_logged_not_raised(self):
        c = Collector()
        self.addCleanup(c.close)
        logged = []
        self.assertFalse(otel.export_run(c.url, None, [], log=logged.append))
        self.assertEqual((len(logged), c.posts), (1, []))

    def test_an_empty_endpoint_sends_nothing(self):
        logged = []
        self.assertFalse(otel.export_run("", RUN_ROW, three_calls(), log=logged.append))
        self.assertEqual(logged, [])


class FetchStream(unittest.TestCase):
    def setUp(self):
        text = "".join(json.dumps(r) + "\n" for r in three_calls()) + '{"type": "tool_exec'  # a torn last line
        self.collector = Collector(stream=text)
        self.gitea = SimpleNamespace(url=self.collector.url, token="tok")
        self.logged = []

    def tearDown(self):
        self.collector.close()

    def test_the_stream_is_read_from_the_records_repo(self):
        rows = otel.fetch_stream(self.gitea, "dark-records/s1/hello-run", log=self.logged.append)
        self.assertEqual(len(rows), 12)
        self.assertEqual(len(otel.tool_calls(rows)), 3)
        self.assertEqual(self.collector.gets,
                         [("/api/v1/repos/dark-records/s1/raw/hello-run/stream.jsonl?ref=main", "token tok")])

    def test_no_stream_to_read(self):
        for records in (None, "", "PUSH FAILED: git: 403 for /a/b/c"):
            self.assertEqual(otel.fetch_stream(self.gitea, records, log=self.logged.append), [])
        self.assertEqual((self.collector.gets, self.logged), ([], []))

    def test_an_unreachable_gitea_is_logged_and_the_trace_goes_without_tool_spans(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        gitea = SimpleNamespace(url=f"http://127.0.0.1:{port}", token="tok")
        self.assertEqual(otel.fetch_stream(gitea, "dark-records/s1/hello-run", timeout=2, log=self.logged.append), [])
        self.assertEqual(len(self.logged), 1)


class Wiring(unittest.TestCase):
    """_Run.end, the one writer of run.end, exports after the row is written when
    otlp_endpoint is set, and only then."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stream = "".join(json.dumps(r) + "\n" for r in three_calls())
        self.collector = Collector(stream=stream)
        self.addCleanup(self.collector.close)
        self.ledger = L.Ledger(os.path.join(self.tmp, "ledger.jsonl"))
        self.logged = []

    def end(self, otlp_endpoint, records="dark-records/s1/hello-run"):
        model = SimpleNamespace(paid=False, watts="low", local=True)
        runner = SimpleNamespace(
            catalog=SimpleNamespace(model=lambda tier: model, provider_of=lambda tier: SimpleNamespace(think_api="none")),
            host=config.Host(otlp_endpoint=otlp_endpoint), ledger=self.ledger, log=self.logged.append, shift="s1",
            clock=time.time, gitea=SimpleNamespace(url=self.collector.url, token="tok"), tests_version=lambda: "abc")
        task = SimpleNamespace(id="hello", cls="additive", repo_name="t-hello", after=None)
        st = R._Run(runner, task, "local-a", "session", "hello-run", "run/hello-run", time.time() - 60)
        usage = {"seconds": 40, "calls": 3, "tokens_in": 1, "tokens_out": 1, "reasoning_chars": 0, "records": records}
        return st.end("abort", None, "test", usage=usage)

    def test_the_row_is_written_and_then_exported_with_the_streams_tool_calls(self):
        self.end(self.collector.url)
        (row,) = self.ledger.events("run.end")
        (path, _, body) = self.collector.posts[0]
        self.assertEqual(path, "/v1/traces")
        parent, *tools = spans_of(body)
        self.assertEqual(parent["name"], "invoke_agent hello")
        self.assertEqual(attrs(parent)["gen_ai.conversation.id"], {"stringValue": row["run"]})
        self.assertEqual(attrs(parent)["dark.outcome"], {"stringValue": "abort"})
        self.assertEqual([s["name"] for s in tools], ["execute_tool write", "execute_tool read", "execute_tool bash"])
        self.assertEqual(self.logged, [])

    def test_a_run_that_pushed_no_records_is_exported_without_tool_spans(self):
        self.end(self.collector.url, records=None)
        self.assertEqual(len(spans_of(self.collector.posts[0][2])), 1)
        self.assertEqual(self.collector.gets, [])

    def test_a_dead_collector_changes_nothing_about_the_run(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = self.end(f"http://127.0.0.1:{port}", records=None)
        self.assertEqual(res.outcome, "abort")
        self.assertEqual(len(self.ledger.events("run.end")), 1)
        self.assertEqual(len(self.logged), 1)

    def test_with_no_endpoint_nothing_is_sent_and_nothing_is_fetched(self):
        self.end("")
        self.assertEqual((self.collector.posts, self.collector.gets), ([], []))
        self.assertEqual(len(self.ledger.events("run.end")), 1)


class HostKey(unittest.TestCase):
    """otlp_endpoint: the default, the file value, the environment override named
    in the example file, and `dark check-config` with and without it. The
    existing config tests are not touched: this file is the one the change owns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("models.toml", "budgets.toml"):
            shutil.copy(os.path.join(RUNNER, name), os.path.join(self.tmp, name))

    def host_file(self, body):
        with open(os.path.join(self.tmp, "host.toml"), "w") as f:
            f.write(body)
        return os.path.join(self.tmp, "host.toml")

    def test_default_file_and_environment(self):
        self.assertEqual(config.Host().otlp_endpoint, "")
        p = self.host_file('[host]\notlp_endpoint = "http://collector:4318"\n')
        self.assertEqual(config.load_host(p, environ={}).otlp_endpoint, "http://collector:4318")
        self.assertEqual(config.load_host(p, environ={"DARK_OTLP_ENDPOINT": "http://other:4318"}).otlp_endpoint,
                         "http://other:4318")
        self.assertEqual(config.load_host(self.host_file("[host]\n"), environ={}).otlp_endpoint, "")
        self.assertEqual(config.HOST_ENV["otlp_endpoint"], "DARK_OTLP_ENDPOINT")

    def test_the_example_host_file_names_the_key_and_its_variable(self):
        with open(os.path.join(RUNNER, "host.toml")) as f:
            text = f.read()
        self.assertRegex(text, r"(?m)^otlp_endpoint = \"\" .*# DARK_OTLP_ENDPOINT\b")
        self.assertEqual(config.load_host(os.path.join(RUNNER, "host.toml"), environ={}).otlp_endpoint, "")

    def check_config(self, body):
        self.host_file(body)
        buf = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(buf):
            os.environ.pop("DARK_OTLP_ENDPOINT", None)
            rc = M.main(["--conf", self.tmp, "check-config"])
        return rc, buf.getvalue()

    def test_check_config_accepts_a_host_file_with_and_without_the_key(self):
        for body in ("[host]\n", '[host]\notlp_endpoint = ""\n', '[host]\notlp_endpoint = "http://127.0.0.1:4318"\n'):
            with self.subTest(body=body):
                rc, out = self.check_config(body)
                self.assertEqual(rc, 0, out)
                self.assertIn("config OK", out)


if __name__ == "__main__":
    unittest.main()

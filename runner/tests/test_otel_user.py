"""OpenTelemetry export tests for a user-arm run: every browser action is a child span.

A user-arm run writes stream.jsonl rows shaped {"action": {...}, "call": N,
"result": {...}, "snapshot_chars": N, "step": N, "url": "..."} (dark/user.py),
one per browser action. otel.tool_calls() turns each row into one
`execute_tool browser.<do>` child span carrying the action as the arguments,
the result as the result and `dark.step`; the rows carry no time of their own,
so the spans are spaced evenly across the parent span. The pi-row tests stay
in test_otel.py: this file is the user-arm half.
"""
import json
import unittest

from dark import otel

T_END = 1788576830.5  # the run.end row's ts: epoch seconds
RUN_ROW = {"v": 2, "ts": T_END, "iso": "2026-09-05T02:53:50+00:00", "kind": "run.end", "task": "shop",
           "run": "shop-user-20260905-025327", "cls": "additive", "tier": "local-a", "outcome": "pass",
           "seconds": 40, "wall_seconds": 45, "calls": 4, "tool_calls": 4, "arm": "user"}
START_NS, END_NS = 1788576785500000000, 1788576830500000000


def user_rows():
    """Four rows, two steps with one verdict each, as dark/user.py appends them."""
    def row(step, call, url, action, result):
        return {"step": step, "call": call, "url": url, "snapshot_chars": 100 + call,
                "action": action, "result": result}
    click = {"do": "click", "selector": "#buy"}
    fill = {"do": "fill", "selector": "#qty", "value": "2"}
    return [
        row(1, 1, "http://shop.example/", click, {"action": click, "ok": True}),
        row(1, 2, "http://shop.example/cart", {"do": "verdict", "verdict": "pass", "note": "in the cart"},
            {"verdict": "pass"}),
        row(2, 3, "http://shop.example/cart", fill, {"action": fill, "ok": True}),
        row(2, 4, "http://shop.example/cart", {"do": "verdict", "verdict": "pass", "note": "two in the cart"},
            {"verdict": "pass"}),
    ]


def spans_of(body):
    (resource,) = body["resourceSpans"]
    (scope,) = resource["scopeSpans"]
    return scope["spans"]


def attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


class UserArm(unittest.TestCase):
    def setUp(self):
        self.rows = user_rows()
        self.spans = spans_of(otel.build_body(RUN_ROW, self.rows))

    def test_four_rows_become_four_child_spans(self):
        parent, *tools = self.spans
        self.assertEqual(len(self.spans), 5)
        self.assertEqual(parent["name"], "invoke_agent shop")
        self.assertEqual(attrs(parent)["gen_ai.agent.name"], {"stringValue": "user"})
        self.assertNotIn("parentSpanId", parent)
        self.assertEqual([s["name"] for s in tools],
                         ["execute_tool browser.click", "execute_tool browser.verdict",
                          "execute_tool browser.fill", "execute_tool browser.verdict"])

    def test_the_arguments_the_result_and_the_step(self):
        parent, *tools = self.spans
        for span, row in zip(tools, self.rows):
            self.assertEqual((span["traceId"], span["parentSpanId"]), (parent["traceId"], parent["spanId"]))
            t = attrs(span)
            self.assertEqual(t["gen_ai.operation.name"], {"stringValue": "execute_tool"})
            self.assertEqual(t["gen_ai.tool.name"], {"stringValue": span["name"].split(" ", 1)[1]})
            self.assertEqual(json.loads(t["gen_ai.tool.call.arguments"]["stringValue"]), row["action"])
            self.assertEqual(json.loads(t["gen_ai.tool.call.result"]["stringValue"]), row["result"])
            self.assertEqual(t["dark.step"], {"intValue": str(row["step"])})
            self.assertNotIn("gen_ai.tool.call.id", t)  # a user row has no tool call id

    def test_the_spans_are_spaced_evenly_across_the_parent(self):
        parent, *tools = self.spans
        self.assertEqual((parent["startTimeUnixNano"], parent["endTimeUnixNano"]),
                         (str(START_NS), str(END_NS)))
        self.assertEqual([(s["startTimeUnixNano"], s["endTimeUnixNano"]) for s in tools],
                         [(str(START_NS + n * 11_250_000_000), str(START_NS + (n + 1) * 11_250_000_000))
                          for n in range(4)])

    def test_a_user_call_that_carries_a_time_keeps_it(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["start_ms"], rows[0]["end_ms"] = 1788576800000, 1788576800500
        _, *tools = spans_of(otel.build_body(RUN_ROW, rows))
        self.assertEqual((tools[0]["startTimeUnixNano"], tools[0]["endTimeUnixNano"]),
                         (str(1788576800000 * 1_000_000), str(1788576800500 * 1_000_000)))
        self.assertLessEqual(tools[1]["startTimeUnixNano"], tools[1]["endTimeUnixNano"])
        self.assertNotEqual(tools[0]["startTimeUnixNano"], tools[1]["startTimeUnixNano"])


if __name__ == "__main__":
    unittest.main()

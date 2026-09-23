import os
import tempfile
import unittest

from dark import config, llm, review, tasks
from dark import ledger as L
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_tasks import make_task

REPLY = """CHECK output: IMPLIED — the spec pins the exact line
CHECK exit-code: NOT IMPLIED — the spec never mentions the exit status
VERDICT: OK — looks fine"""


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # the fixture's 1000-token window cannot hold one review call; widen it
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS.replace("daily_tokens = 1000", "daily_tokens = 100000")))
        self.led = L.Ledger(os.path.join(self.tmp, "l.jsonl"))
        self.task = tasks.load_task(make_task(os.path.join(self.tmp, "bench"), "hello", start={"a.py": "x\n"},
                                              oracle={"hello.txt": "hello\n"}))

    def test_parse_line_list_wins_over_verdict(self):
        verdict, checks = review.parse(REPLY)
        self.assertEqual(verdict, "AMBIGUOUS")
        self.assertEqual([(n, ok) for n, ok, _ in checks], [("output", True), ("exit-code", False)])
        self.assertEqual(checks[1][2], "the spec never mentions the exit status")
        self.assertEqual(review.parse("nonsense")[0], "UNPARSED")
        self.assertEqual(review.parse("CHECK a: IMPLIED - yes\nVERDICT: OK")[0], "OK")
        # the acceptance's own line shape, copied by the validator (lru-cache-go)
        verdict, checks = review.parse("CHECK files ok: IMPLIED — yes\nCHECK n-x fail: NOT IMPLIED — no\nVERDICT: OK")
        self.assertEqual([(n, ok) for n, ok, _ in checks], [("files", True), ("n-x", False)])
        self.assertEqual(verdict, "AMBIGUOUS")

    def test_prompt_carries_spec_start_acceptance_oracle(self):
        p = review.prompt(self.task)
        for needle in ("SPEC:", "Create hello.txt", "STARTING FILES", "a.py", "ACCEPTANCE", "run.sh", "ORACLE", "hello.txt"):
            self.assertIn(needle, p)

    def test_review_task_records_a_call(self):
        seen = {}

        def chat(url, model, messages, max_tokens, timeout, **kw):
            seen.update(url=url, model=model, n=len(messages), max_tokens=max_tokens)
            return REPLY, {"tokens_in": 500, "tokens_out": 50, "reasoning_chars": 0, "seconds": 1}
        verdict, checks, reply, usage = review.review_task(self.task, self.cat, self.bud, self.led, "cloud-x", chat=chat)
        self.assertEqual(verdict, "AMBIGUOUS")
        self.assertEqual(seen["model"], "cloud-x")
        self.assertEqual(seen["n"], 2)
        call = self.led.last("call")
        self.assertEqual((call["purpose"], call["cls"], call["tier"], call["ok"], call["tokens_in"], call["task"]),
                         ("spec-review", "spec", "cloud-x", True, 500, "hello"))
        text = review.render(self.task, "cloud-x", verdict, checks, reply, usage, 0)
        self.assertIn("**VERDICT: AMBIGUOUS**", text)
        self.assertIn("- exit-code: NOT IMPLIED", text)

    def test_validator_share_gates_the_call(self):
        cat, bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))  # 1000-token window: too small
        called = []
        verdict, checks, reply, usage = review.review_task(self.task, cat, bud, self.led, "cloud-x",
                                                           chat=lambda *a, **kw: called.append(1))
        self.assertEqual(verdict, "SKIPPED")
        self.assertIn("window", reply)
        self.assertEqual(called, [])
        self.assertIsNone(self.led.last("call"))

    def test_call_failure_is_unparsed_and_recorded(self):
        def chat(*a, **kw):
            raise llm.LLMError("rate", "429")
        verdict, checks, reply, usage = review.review_task(self.task, self.cat, self.bud, self.led, "cloud-x", chat=chat)
        self.assertEqual(verdict, "UNPARSED")
        self.assertFalse(self.led.last("call")["ok"])

    def test_reasoning_over_spec_cap(self):
        def chat(*a, **kw):
            return REPLY.replace("NOT IMPLIED", "IMPLIED"), {"tokens_in": 1, "tokens_out": 1, "reasoning_chars": 10**6, "seconds": 1}
        verdict, _, reply, _ = review.review_task(self.task, self.cat, self.bud, self.led, "cloud-x", chat=chat)
        self.assertEqual(verdict, "UNPARSED")
        self.assertIn("reasoning over the spec cap", reply)


if __name__ == "__main__":
    unittest.main()

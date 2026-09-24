"""dark/budget.py, dark/ledger.py and dark/admission.py: the accounting rules
for unreported spend, the validator share, the pass-rate window, cooldown and
the soft-limit floor.
"""

import math
import os
import shutil
import tempfile
import unittest

from dark import admission, budget, config
from dark import ledger as L
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_ledger import Clock, run_end
from tests.test_run import FILE_HELLO, Base

DARK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dark")


class UnreportedSpend(Base):
    """A window is only ever advanced by what the executor self-reported in
    its AGENT-DONE tag. A run the runner itself terminated (deadline, silent
    kill, abort) never carries one, so its real provider calls are recorded as
    zero and the day's allowance never moves."""

    def setUp(self):
        super().setUp()
        self.cat, self.bud = config.load(write_conf(
            self.tmp, MODELS, BUDGETS.replace("daily_tokens = 1000", "daily_tokens = 10000000")))

    def test_a_run_killed_on_its_deadline_still_charges_the_window(self):
        r = self.runner([{"content": FILE_HELLO, "sleep": 3}], tier="cloud-x")
        res = r.run(self.task, "cloud-x", self.env(seconds=1))
        self.assertEqual((res.outcome, res.fail_kind), ("fail:budget", "seconds"))
        self.assertEqual(len(self.llm.requests), 1, "the provider really served a call")

        end = self.led.last("run.end")
        self.assertEqual(end["calls"], 1, "the served call was recorded as zero")
        # tokens the runner never saw stay NOT MEASURED (None); the window is
        # charged the reservation per served call instead
        self.assertIsNone(end["tokens_in"])
        self.assertGreater(end["reserved_per_call"], 0)

        day = budget.today(self.led)
        win = budget.windows(self.bud, self.cat, self.led, day)["cloud"]
        self.assertEqual(win.calls_used, 1)
        self.assertEqual(win.tokens_used, end["reserved_per_call"])


class ValidatorShare(unittest.TestCase):
    """"Validators may consume at most (1 - reserve_for_data_runs) of a
    window." WindowState.open_for implements the share; the runner's tier
    gate only evaluates it when the call site passes a role."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat, self.bud = config.load(write_conf(
            self.tmp, MODELS, BUDGETS.replace("daily_tokens = 1000", "daily_tokens = 10000000")))
        self.clock = Clock()
        self.led = L.Ledger(os.path.join(self.tmp, "l.jsonl"), clock=self.clock)
        self.day = L.day_of(self.clock.t + 1)

    def test_some_production_gate_asks_open_for_about_validators(self):
        callers = []
        for name in sorted(os.listdir(DARK_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(DARK_DIR, name)) as f:
                for i, line in enumerate(f, 1):
                    if "validator=" in line and "def " not in line:
                        callers.append(f"dark/{name}:{i}: {line.strip()}")
        # the share is evaluated where the argument is not the literal False
        # (tier_open computes it from the class and the role; review.py passes
        # the role)
        self.assertTrue([c for c in callers if "validator=False" not in c],
                        "no module in dark/ ever evaluates the validator share; "
                        f"every call site is: {callers}")

    def test_a_validator_past_its_share_is_closed(self):
        # cloud-x is the catalog's validator; reserve 0.5 -> 5 of 10 calls.
        for i in range(6):
            self.led.emit("call", shift="s", cls="review", tier="cloud-x", seconds=1,
                          tokens_in=10, tokens_out=5, paid=True, reasoning_chars=0,
                          purpose="validate", ok=True)
        win = budget.windows(self.bud, self.cat, self.led, self.day)["cloud"]
        self.assertEqual(win.validator_calls, 6)
        self.assertFalse(win.open_for(1, 10, validator=True)[0])  # the share is spent

        env = budget.envelope(self.bud, self.led, "review")
        ok, why = budget.tier_open(self.cat, self.bud, self.led, "cloud-x", env, self.day)
        self.assertFalse(ok, f"the validator tier is still open past its share ({why!r})")


class PassRateAndCooldown(unittest.TestCase):
    """The last-N-runs window deadmits and readmits a tier; a cooldown lapses
    on its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))
        self.clock = Clock()
        self.led = L.Ledger(os.path.join(self.tmp, "l.jsonl"), clock=self.clock)

    def test_last_runs_failures_deadmit_the_tier(self):
        self.assertIn("local-a", admission.admitted(self.bud, self.cat, self.led, "additive"))
        for i in range(self.bud.last_runs):
            self.led.emit("run.end", **run_end(run=f"f{i}", outcome="fail:capability"))
        self.assertNotIn("local-a", admission.admitted(self.bud, self.cat, self.led, "additive"))
        # and a run of passes readmits it: the window really is the last N
        for i in range(self.bud.last_runs):
            self.led.emit("run.end", **run_end(run=f"p{i}"))
        self.assertIn("local-a", admission.admitted(self.bud, self.cat, self.led, "additive"))

    def test_cooldown_lapses(self):
        rec = self.led.emit("tier.cooldown", tier="local-a", seconds=10, reason="429")
        self.assertEqual(admission.cooling(self.led, rec["ts"] + 5), {"local-a": 5})
        self.assertEqual(admission.cooling(self.led, rec["ts"] + 10), {})
        self.assertEqual(admission.cooling(self.led, rec["ts"] + 10_000), {})

    def test_p95_over_one_to_three_passes_is_the_worst_pass(self):
        for n, vals in ((1, [4]), (2, [2, 5]), (3, [2, 5, 3])):
            led = L.Ledger(os.path.join(self.tmp, f"p{n}.jsonl"), clock=Clock())
            for i, v in enumerate(vals):
                led.emit("run.end", **run_end(run=f"r{i}", calls=v))
            self.assertEqual(led.p95("additive", "calls"), max(vals))

    def test_zero_reasoning_over_passes_keeps_the_hard_cap(self):
        for i in range(self.bud.min_runs):
            self.led.emit("run.end", **run_end(run=f"r{i}", reasoning_chars=0))
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual(env.basis, "soft")
        self.assertEqual(env.max_reasoning_chars, self.bud.cls("additive").hard.max_reasoning_chars)

    def test_a_single_near_zero_reasoning_pass_collapses_the_class_cap(self):
        """Characterisation: the formula is the runner's, but _soft's p95 <= 0
        guard is a point test, so one pass reporting 4 reasoning chars takes
        the whole class from 20000 to 6."""
        for i in range(self.bud.min_runs - 1):
            self.led.emit("run.end", **run_end(run=f"z{i}", reasoning_chars=0))
        self.led.emit("run.end", **run_end(run="tiny", reasoning_chars=4))
        env = budget.envelope(self.bud, self.led, "additive")
        # every soft limit keeps a floor of half of hard, and the soft limits
        # are per (class, tier) pair in the shift
        hard = self.bud.cls("additive").hard.max_reasoning_chars
        self.assertEqual(env.max_reasoning_chars, hard // 2)
        self.assertGreater(env.max_reasoning_chars, math.ceil(4 * (1 + self.bud.cls("additive").headroom)))


if __name__ == "__main__":
    unittest.main()

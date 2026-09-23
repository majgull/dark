import os
import tempfile
import unittest

from dark import admission, budget, config
from dark import ledger as L
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_ledger import Clock, run_end


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))
        self.clock = Clock()
        self.led = L.Ledger(os.path.join(self.tmp, "l.jsonl"), clock=self.clock)
        self.day = L.day_of(self.clock.t + 1)

    def passes(self, n, cls="additive", tier="local-a", **kw):
        for i in range(n):
            self.led.emit("run.end", **run_end(run=f"p{cls}{tier}{i}", cls=cls, tier=tier, **kw))

    def fails(self, n, cls="additive", tier="local-a"):
        for i in range(n):
            self.led.emit("run.end", **run_end(run=f"f{cls}{tier}{i}", cls=cls, tier=tier,
                                               outcome="fail:capability"))


class Envelopes(Fixture):
    def test_hard_before_min_runs(self):
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual((env.calls, env.seconds, env.max_reasoning_chars, env.basis), (6, 900, 20000, "hard"))
        self.passes(2)  # min_runs is 3 in the fixture
        self.assertEqual(budget.envelope(self.bud, self.led, "additive").basis, "hard")

    def test_soft_from_p95_clipped_by_hard(self):
        self.passes(3, calls=2, seconds=100, reasoning_chars=0)
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual(env.basis, "soft")
        self.assertEqual(env.calls, 3)            # max(6 // 2, ceil(2 * 1.3))
        self.assertEqual(env.seconds, 450)        # ceil(100 * 1.3) = 130 is under the floor of hard / 2
        self.assertEqual(env.max_reasoning_chars, 20000)  # p95 of 0 keeps the hard cap
        self.assertEqual(env.runs, 2)

    def test_soft_never_above_hard(self):
        self.passes(3, calls=6, seconds=5000)
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual((env.calls, env.seconds), (6, 900))

    def test_soft_never_below_half_of_hard(self):
        # the pilot's soft caps came from hello-* passes and killed a medium
        # task that was going to pass; the floor stops that ratchet
        self.passes(3, calls=1, seconds=10, reasoning_chars=4)
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual((env.calls, env.seconds, env.max_reasoning_chars), (3, 450, 10000))

    def test_failures_do_not_feed_the_soft_limit(self):
        self.passes(3, calls=3, seconds=10)
        self.fails(5)
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual(env.calls, 4)            # ceil(3 * 1.3); the fails' calls are not in the p95

    def test_soft_is_per_class_tier_pair(self):
        self.passes(3, calls=3, seconds=600, tier="local-a")
        self.assertEqual(budget.envelope(self.bud, self.led, "additive", "local-a").basis, "soft")
        self.assertEqual(budget.envelope(self.bud, self.led, "additive", "local-a").seconds, 780)
        # another tier of the class has no passes: it keeps the hard envelope
        self.assertEqual(budget.envelope(self.bud, self.led, "additive", "cloud-x").basis, "hard")
        # the class-wide envelope (no tier) still sees the passes
        self.assertEqual(budget.envelope(self.bud, self.led, "additive").basis, "soft")


class Windows(Fixture):
    def test_fresh_window(self):
        w = budget.windows(self.bud, self.cat, self.led, self.day)["cloud"]
        self.assertEqual((w.calls_left, w.tokens_left), (10, 1000))
        self.assertFalse(w.exhausted)
        self.assertEqual(w.open_for(3, 100), (True, ""))

    def test_spend_counts_only_that_window_and_day(self):
        self.passes(1, tier="cloud-x", paid=True, watts_class=None, calls=4, tokens_in=300, tokens_out=100)
        self.passes(1, tier="local-a", calls=4, tokens_in=300, tokens_out=100)
        w = budget.windows(self.bud, self.cat, self.led, self.day)["cloud"]
        self.assertEqual((w.calls_used, w.tokens_used), (4, 400))
        ok, why = w.open_for(7, 100)
        self.assertFalse(ok)
        self.assertIn("calls left", why)
        ok, why = w.open_for(1, 700)
        self.assertFalse(ok)
        self.assertIn("tokens left", why)
        self.assertEqual(budget.windows(self.bud, self.cat, self.led, "1999-01-01")["cloud"].calls_used, 0)

    def test_validator_share(self):
        # reserve 0.5 -> validators get 5 calls / 500 tokens
        for i in range(5):
            self.led.emit("call", shift="s", cls="review", tier="cloud-x", seconds=1, tokens_in=10,
                          tokens_out=10, reasoning_chars=0, paid=True, purpose="validate")
        w = budget.windows(self.bud, self.cat, self.led, self.day)["cloud"]
        self.assertEqual(w.open_for(1, 10, validator=False), (True, ""))
        ok, why = w.open_for(1, 10, validator=True)
        self.assertFalse(ok)
        self.assertIn("validator share", why)

    def test_rate_limit_signal_closes_the_day(self):
        self.led.emit("window.exhausted", provider="cloud", day=self.day)
        w = budget.windows(self.bud, self.cat, self.led, self.day)["cloud"]
        self.assertTrue(w.exhausted)
        self.assertFalse(w.open_for(1, 1)[0])
        self.assertFalse(budget.windows(self.bud, self.cat, self.led, "1999-01-01")["cloud"].exhausted)

    def test_tier_open(self):
        env = budget.envelope(self.bud, self.led, "additive")
        self.assertEqual(budget.tier_open(self.cat, self.bud, self.led, "local-a", env, self.day), (True, ""))
        ok, why = budget.tier_open(self.cat, self.bud, self.led, "cloud-x", env, self.day)
        self.assertFalse(ok)  # 6 calls * (32768 + 2048) tokens reserved > 1000 daily
        self.assertIn("tokens left", why)
        self.assertEqual(budget.reservation(self.cat, "cloud-x", env), (6, 6 * (32768 + 2048)))


class Watts(Fixture):
    def test_watts_state(self):
        self.passes(1, tier="local-b", watts_class="high", seconds=1200)  # 300W * 1200s = 100 Wh
        wts = budget.watts(self.bud, self.cat, self.led, self.day)
        self.assertAlmostEqual(wts.used_wh, 100.0)
        self.assertTrue(wts.exhausted)
        env = budget.envelope(self.bud, self.led, "additive")
        ok, why = budget.tier_open(self.cat, self.bud, self.led, "local-a", env, self.day)
        self.assertFalse(ok)
        self.assertIn("watts", why)


class Admission(Fixture):
    def test_provisional_until_min_runs(self):
        adm = admission.admitted(self.bud, self.cat, self.led, "additive")
        self.assertEqual(adm, ["local-a", "cloud-x", "local-b"])  # the seed list's order until data exists
        self.assertEqual(admission.admitted(self.bud, self.cat, self.led, "mechanical"), ["local-a"])
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "mechanical")}
        self.assertEqual(rows["cloud-x"].basis, "excluded")
        self.assertEqual(rows["local-a"].basis, "provisional")

    def test_measured_replaces_provisional(self):
        self.passes(1, cls="mechanical", tier="cloud-x", paid=True, watts_class=None)
        self.fails(2, cls="mechanical", tier="cloud-x")  # 3 runs, rate 0.33 < 0.7
        self.assertNotIn("cloud-x", admission.admitted(self.bud, self.cat, self.led, "mechanical"))
        self.passes(4, cls="mechanical", tier="cloud-x", paid=True, watts_class=None)  # last 5: 4 pass
        self.assertIn("cloud-x", admission.admitted(self.bud, self.cat, self.led, "mechanical"))
        row = [r for r in admission.rows(self.bud, self.cat, self.led, "mechanical") if r.tier == "cloud-x"][0]
        self.assertEqual(row.basis, "measured")
        self.assertEqual(row.n, 5)

    def test_measured_can_evict_a_provisional_tier(self):
        self.fails(3, cls="additive", tier="local-a")
        self.assertEqual(admission.admitted(self.bud, self.cat, self.led, "additive"), ["cloud-x", "local-b"])

    def test_measured_tiers_rank_first_by_cost(self):
        self.passes(3, cls="additive", tier="local-b", watts_class="high")
        self.assertEqual(admission.admitted(self.bud, self.cat, self.led, "additive"), ["local-b", "local-a", "cloud-x"])
        self.passes(3, cls="additive", tier="local-a")
        self.assertEqual(admission.admitted(self.bud, self.cat, self.led, "additive"), ["local-a", "local-b", "cloud-x"])

    def test_pick_and_single_escalation(self):
        adm = ["local-a", "local-b", "cloud-x"]
        self.assertEqual(admission.pick(adm), "local-a")
        self.assertEqual(admission.pick(adm, exclude={"local-a"}), "local-b")
        self.assertEqual(admission.pick(adm, cooling_tiers={"local-a": 30, "local-b": 5}), "cloud-x")
        self.assertIsNone(admission.pick(adm, exclude=set(adm)))
        closed = lambda t: (t != "cloud-x", f"{t} closed")  # noqa: E731
        self.assertEqual(admission.pick(adm, is_open=closed), "local-a")
        self.assertEqual(admission.pick(["cloud-x", "local-b"], is_open=closed), "local-b")
        out, why = admission.candidates(adm, is_open=closed)
        self.assertEqual((out, why), (["local-a", "local-b"], ["cloud-x: cloud-x closed"]))
        self.assertEqual(admission.escalation(adm, "local-a"), "local-b")
        self.assertEqual(admission.escalation(adm, "local-a", cooling_tiers={"local-b": 1}), "cloud-x")
        self.assertEqual(admission.escalation(adm, "local-a", is_open=lambda t: (t == "cloud-x", "x")), "cloud-x")
        self.assertIsNone(admission.escalation(adm, "cloud-x"))
        self.assertIsNone(admission.escalation(adm, "ghost"))

    def test_escalation_goes_to_the_best_open_tier(self):
        # decision 20: three tiers tied ahead of a better one; the retry
        # reaches the better one, and a closed or cooling best falls back
        adm = ["local-a", "local-b", "local-c", "cloud-x"]
        rates = {"local-a": 0.83, "local-b": 0.83, "local-c": 0.83, "cloud-x": 1.0}
        self.assertEqual(admission.escalation(adm, "local-a", rates=rates), "cloud-x")
        self.assertEqual(admission.escalation(adm, "local-a", rates=rates, cooling_tiers={"cloud-x": 9}), "local-b")
        self.assertEqual(admission.escalation(adm, "local-a", rates=rates, is_open=lambda t: (t != "cloud-x", "closed")), "local-b")
        # a provisional tier (no rate) loses to any measured one, and ties keep admitted order
        self.assertEqual(admission.escalation(adm, "local-b", rates={"local-a": None, "local-c": 0.5, "cloud-x": 0.5}), "local-c")
        self.assertIsNone(admission.escalation(["local-a"], "local-a", rates={"local-a": 1.0}))

    def test_cooling(self):
        self.led.emit("tier.cooldown", tier="cloud-x", seconds=100, reason="429")
        now = self.clock.t
        self.assertIn("cloud-x", admission.cooling(self.led, now))
        self.assertEqual(admission.cooling(self.led, now + 200), {})

    def test_table_shape(self):
        t = admission.table(self.bud, self.cat, self.led)
        self.assertEqual(set(t), set(self.bud.classes))
        self.assertEqual({r["tier"] for r in t["additive"]}, set(self.cat.models))


if __name__ == "__main__":
    unittest.main()


class Levels(Fixture):
    """Decision 14: rates and envelopes are per thinking level. A tier's runs
    before the levels existed count as "none" when it does not think and as
    no level when it does; the class's level is what the factory runs at."""

    def with_level(self, cls="additive", lvl="low"):
        import dataclasses
        self.bud.classes[cls] = dataclasses.replace(self.bud.cls(cls), think=lvl)

    def thinking(self, tier):
        import dataclasses
        self.cat.models[tier] = dataclasses.replace(self.cat.models[tier], thinking_tokens=1000)

    def test_rate_counts_only_runs_at_the_class_level(self):
        self.with_level("additive", "low")
        self.thinking("local-a")
        self.fails(3, cls="additive", tier="local-a")                       # uncontrolled past: no level
        self.passes(3, cls="additive", tier="local-a", think="medium")      # another level
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "additive")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].n), ("provisional", 0))
        self.passes(3, cls="additive", tier="local-a", think="low")
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "additive")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].rate, rows["local-a"].n), ("measured", 1.0, 3))

    def test_legacy_runs_of_a_non_thinking_tier_count_as_none(self):
        self.with_level("mechanical", "none")
        self.passes(3, cls="mechanical", tier="local-a")                    # local-a: thinking_tokens 0, no think field
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "mechanical")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].n), ("measured", 3))
        self.with_level("mechanical", "low")                                # at low they are not evidence
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "mechanical")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].n), ("provisional", 0))

    def test_shift_override_wins_over_the_class_level(self):
        self.with_level("additive", "low")
        self.passes(3, cls="additive", tier="local-a", think="medium")
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "additive", think="medium")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].n), ("measured", 3))

    def test_envelope_is_per_level(self):
        self.with_level("additive", "low")
        self.passes(3, cls="additive", tier="local-a", think="medium", seconds=800)
        think, legacy = budget.level(self.bud, self.cat, "additive", "local-a")
        self.assertEqual((think, legacy), ("low", "none"))
        env = budget.envelope(self.bud, self.led, "additive", "local-a", think=think, legacy=legacy)
        self.assertEqual(env.basis, "hard")                                 # medium passes do not size low
        self.passes(3, cls="additive", tier="local-a", think="low", seconds=100)
        env = budget.envelope(self.bud, self.led, "additive", "local-a", think=think, legacy=legacy)
        self.assertEqual((env.basis, env.seconds), ("soft", 450))          # floor: half of hard, from low passes only
        self.assertEqual(budget.level(self.bud, self.cat, "additive", "local-a", "medium")[0], "medium")

    def test_no_level_anywhere_judges_all_runs(self):
        self.passes(2, cls="additive", tier="local-a")
        self.passes(1, cls="additive", tier="local-a", think="low")
        rows = {r.tier: r for r in admission.rows(self.bud, self.cat, self.led, "additive")}
        self.assertEqual((rows["local-a"].basis, rows["local-a"].n), ("measured", 3))

import json
import os
import tempfile
import unittest

from dark import ledger as L

DAY = 1_800_000_000.0  # some epoch; the clock in tests is explicit


def run_end(**kw):
    base = dict(task="t1", run="r1", cls="additive", tier="local-a", outcome="pass",
                seconds=100, calls=2, tokens_in=1000, tokens_out=500, reasoning_chars=0,
                paid=False, watts_class="low")
    base.update(kw)
    return base


class Clock:
    def __init__(self, t=DAY):
        self.t = t

    def __call__(self):
        self.t += 1
        return self.t


class Emit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "ledger.jsonl")
        self.led = L.Ledger(self.path, clock=Clock())

    def test_emit_writes_one_validated_line(self):
        rec = self.led.emit("park", task="t1", reason="window")
        self.assertEqual(rec["v"], 2)
        self.assertIn("iso", rec)
        with open(self.path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["kind"], "park")

    def test_unregistered_kind_refused(self):
        with self.assertRaises(L.LedgerError):
            self.led.emit("nope", task="t")
        self.assertFalse(os.path.exists(self.path))

    def test_undeclared_field_refused(self):
        with self.assertRaises(L.LedgerError):
            self.led.emit("park", task="t", reason="x", bogus=1)

    def test_missing_required_refused(self):
        with self.assertRaises(L.LedgerError):
            self.led.emit("run.end", **{k: v for k, v in run_end().items() if k != "tier"})

    def test_events_filter_and_order(self):
        self.led.emit("shift.start", shift="s1")
        self.led.emit("run.end", **run_end())
        self.led.emit("run.end", **run_end(run="r2", outcome="fail:capability"))
        self.assertEqual([r["kind"] for r in self.led.events()], ["shift.start", "run.end", "run.end"])
        self.assertEqual(len(self.led.events("run.end")), 2)
        self.assertEqual(len(self.led.runs(cls="additive", tier="local-a")), 2)
        self.assertEqual(len(self.led.runs(cls="repair")), 0)

    def test_torn_last_line_tolerated_and_counted(self):
        self.led.emit("shift.start", shift="s1")
        with open(self.path, "a") as f:
            f.write('{"v": 2, "ts": 1, "kind": "run.en')
        self.assertEqual(len(self.led.events()), 1)
        self.assertEqual(self.led.torn, 1)

    def test_corrupt_middle_line_raises(self):
        self.led.emit("shift.start", shift="s1")
        with open(self.path, "a") as f:
            f.write("garbage\n")
        self.led.emit("shift.start", shift="s2")
        with self.assertRaises(L.LedgerError):
            self.led.events()

    def test_unregistered_kind_on_disk_is_skipped_and_counted(self):
        # the first pilot shift (old code) died on a run.void a newer `dark void`
        # appended beside it: a reader never raises on a kind it does not know
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(json.dumps({"v": 2, "ts": 1, "iso": "x", "kind": "made.up"}) + "\n")
        self.led.emit("shift.start", shift="s1")
        self.assertEqual([e["kind"] for e in self.led.events()], ["shift.start"])
        self.assertEqual(self.led.unknown, 1)
        self.assertEqual(self.led.torn, 0)

    def test_missing_file_is_empty(self):
        self.assertEqual(L.Ledger(os.path.join(self.tmp, "none.jsonl")).events(), [])


class Queries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.led = L.Ledger(os.path.join(self.tmp, "l.jsonl"), clock=self.clock)

    def test_pass_rate_last_n(self):
        self.assertEqual(self.led.pass_rate("additive", "local-a", 20), (None, 0))
        for i in range(6):
            self.led.emit("run.end", **run_end(run=f"r{i}", outcome="pass" if i < 3 else "fail:capability"))
        rate, n = self.led.pass_rate("additive", "local-a", 20)
        self.assertEqual((rate, n), (0.5, 6))
        rate, n = self.led.pass_rate("additive", "local-a", 3)  # the last three all failed
        self.assertEqual((rate, n), (0.0, 3))
        self.assertEqual(self.led.passes("additive", "local-a"), 3)
        # decision 11: structural failures and aborts are not the model's and
        # leave the denominator (a provider 502, a VM that would not start)
        self.led.emit("run.end", **run_end(run="s1", outcome="fail:structural", fail_kind="llm"))
        self.led.emit("run.end", **run_end(run="s2", outcome="abort"))
        self.assertEqual(self.led.pass_rate("additive", "local-a", 20), (0.5, 6))
        self.assertEqual(self.led.pass_rate("additive", "local-a", 3), (0.0, 3))

    def test_p95_over_passes_only(self):
        self.assertIsNone(self.led.p95("additive", "seconds"))
        for i, s in enumerate((10, 20, 30, 40, 50, 60, 70, 80, 90, 100)):
            self.led.emit("run.end", **run_end(run=f"r{i}", seconds=s))
        self.led.emit("run.end", **run_end(run="bad", seconds=9999, outcome="fail:budget"))
        self.assertEqual(self.led.p95("additive", "seconds"), 100)
        self.assertEqual(self.led.p95("additive", "seconds", tier="local-a"), 100)
        self.assertIsNone(self.led.p95("additive", "seconds", tier="other"))

    def test_window_used_per_day_and_validator_share(self):
        self.led.emit("run.end", **run_end(tier="cloud-x", paid=True, watts_class=None,
                                           calls=3, tokens_in=100, tokens_out=50))
        self.led.emit("call", shift="s", cls="review", tier="cloud-x", seconds=5,
                      tokens_in=10, tokens_out=5, reasoning_chars=0, paid=True, purpose="validate")
        self.led.emit("call", shift="s", cls="review", tier="other-cloud", seconds=5,
                      tokens_in=1000, tokens_out=5, reasoning_chars=0, paid=True)
        day = L.day_of(self.clock.t)
        used = self.led.window_used({"cloud-x"}, day, validator_tiers={"cloud-x"})
        self.assertEqual(used["calls"], 4)
        self.assertEqual(used["tokens"], 165)
        self.assertEqual(used["validator_calls"], 1)
        self.assertEqual(used["validator_tokens"], 15)
        self.assertEqual(self.led.window_used({"cloud-x"}, "1970-01-01")["calls"], 0)

    def test_watts_used(self):
        self.led.emit("run.end", **run_end(seconds=3600, watts_class="low"))
        self.led.emit("run.end", **run_end(run="r2", tier="cloud-x", paid=True, watts_class=None, seconds=3600))
        self.led.emit("call", shift="s", cls="spec", tier="local-b", seconds=1800,
                      tokens_in=1, tokens_out=1, reasoning_chars=0, paid=False)
        day = L.day_of(self.clock.t)
        wh = self.led.watts_used(day, {"low": 100.0, "mid": 200.0, "high": 300.0},
                                 lambda t: {"local-a": "low", "local-b": "high"}.get(t))
        self.assertAlmostEqual(wh, 100.0 + 150.0)

    def test_watts_used_prefers_the_measured_wh(self):
        # a metered run counts its own Wh whatever its class says; a cloud-tier
        # run that was metered (the executor VM drew on the host) counts too
        self.led.emit("run.end", **run_end(seconds=3600, watts_class="low", wh=7.5, wh_cpu=2.5, wh_gpu=5.0))
        self.led.emit("run.end", **run_end(run="r2", tier="cloud-x", paid=True, watts_class=None, seconds=3600, wh=0.25))
        self.led.emit("run.end", **run_end(run="r3", seconds=3600, watts_class="low"))  # legacy row, placeholder
        day = L.day_of(self.clock.t)
        wh = self.led.watts_used(day, {"low": 100.0, "mid": 200.0, "high": 300.0}, lambda t: None)
        self.assertAlmostEqual(wh, 7.5 + 0.25 + 100.0)
        self.assertEqual(self.led.metered(day), (7.75, 2, 1))

    def test_void_excludes_a_run_from_rates(self):
        self.led.emit("run.end", **run_end(run="good"))
        self.led.emit("run.end", **run_end(run="harness-broke", outcome="fail:capability"))
        self.assertEqual(self.led.pass_rate("additive", "local-a", 20), (0.5, 2))
        self.led.emit("run.void", task="t1", run="harness-broke", reason="empty branch pushed by the harness")
        self.assertEqual(self.led.pass_rate("additive", "local-a", 20), (1.0, 1))
        self.assertEqual([r["run"] for r in self.led.runs()], ["good"])
        self.assertEqual(len(self.led.runs(include_void=True)), 2)
        self.assertEqual(self.led.voided(), {"harness-broke"})

    def test_last(self):
        self.led.emit("shift.start", shift="s1")
        self.led.emit("shift.start", shift="s2")
        self.assertEqual(self.led.last("shift.start")["shift"], "s2")
        self.assertEqual(self.led.last("shift.start", shift="s1")["shift"], "s1")
        self.assertIsNone(self.led.last("run.end"))


if __name__ == "__main__":
    unittest.main()

import hashlib
import os
import tempfile
import unittest

from dark import bench as B
from dark import ledger as L
from tests.test_ledger import Clock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VALID = '''
name = "exp1"
question = "does X help"
tasks = ["t1", "t2"]
judge = "acceptance-v4"
envelope = "frozen/e.toml"
rounds = 2
order = "alternate"

[[arm]]
name = "pipeline"
executor = "agent"
tier = "tier-a"
think = "low"

[[arm]]
name = "session"
executor = "session"
tier = "tier-a"
think = "low"

[orgs]
work = "w"
archive = "a"
records = "r"
results = "res"
'''


class Parse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, text):
        path = os.path.join(self.tmp, "m.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_valid_manifest_parses(self):
        m = B.parse(self.write(VALID))
        self.assertEqual(m.name, "exp1")
        self.assertEqual(m.tasks, ("t1", "t2"))
        self.assertEqual(m.rounds, 2)
        self.assertEqual(m.order, "alternate")
        self.assertEqual(m.arms, (B.Arm("pipeline", "agent", "tier-a", "low"),
                                  B.Arm("session", "session", "tier-a", "low")))
        self.assertEqual(m.orgs, {"work": "w", "archive": "a", "records": "r", "results": "res"})
        self.assertIsNone(m.stop)
        self.assertIsNone(m.smoke)
        self.assertEqual(len(m.sha256), 64)

    def test_sha256_is_the_files_own_bytes(self):
        path = self.write(VALID)
        with open(path, "rb") as f:
            expect = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(B.parse(path).sha256, expect)

    def test_missing_file_is_a_manifest_error(self):
        with self.assertRaises(B.ManifestError):
            B.parse(os.path.join(self.tmp, "nope.toml"))

    def test_not_toml_is_a_manifest_error(self):
        with self.assertRaises(B.ManifestError):
            B.parse(self.write("this is not = [ toml"))

    def test_missing_arm_name_refused(self):
        bad = VALID.replace('name = "pipeline"\n', '')
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("name", str(c.exception))

    def test_bad_executor_refused(self):
        bad = VALID.replace('executor = "agent"', 'executor = "robot"')
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("executor", str(c.exception))

    def test_bad_think_refused(self):
        bad = VALID.replace('think = "low"\n\n[[arm]]', 'think = "extreme"\n\n[[arm]]')
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("think", str(c.exception))

    def test_rounds_must_be_a_positive_int(self):
        for bad_rounds in ("rounds = 0\n", "rounds = -1\n", 'rounds = "5"\n'):
            bad = VALID.replace("rounds = 2\n", bad_rounds)
            with self.assertRaises(B.ManifestError):
                B.parse(self.write(bad))

    def test_order_must_be_alternate(self):
        bad = VALID.replace('order = "alternate"', 'order = "random"')
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("alternate", str(c.exception))

    def test_orgs_missing_a_name_refused(self):
        bad = VALID.replace('results = "res"\n', "")
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("results", str(c.exception))

    def test_no_arms_refused(self):
        bad = VALID.split("[[arm]]")[0] + '[orgs]\nwork="w"\narchive="a"\nrecords="r"\nresults="res"\n'
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("arm", str(c.exception))

    def test_stop_is_optional_and_validated(self):
        ok = VALID + '\n[stop]\ncalls_left_min = 200\ndeadline = "07:30"\n'
        m = B.parse(self.write(ok))
        self.assertEqual(m.stop, {"calls_left_min": 200, "deadline": "07:30"})

    def test_stop_bad_deadline_refused(self):
        bad = VALID + '\n[stop]\ndeadline = "7:30"\n'
        with self.assertRaises(B.ManifestError) as c:
            B.parse(self.write(bad))
        self.assertIn("deadline", str(c.exception))

    def test_stop_bad_calls_left_min_refused(self):
        bad = VALID + '\n[stop]\ncalls_left_min = 0\n'
        with self.assertRaises(B.ManifestError):
            B.parse(self.write(bad))

    def test_smoke_section_parses(self):
        ok = VALID + '\n[smoke]\ntasks = ["t1"]\nrounds = 1\n'
        m = B.parse(self.write(ok))
        self.assertEqual(m.smoke, {"tasks": ("t1",), "rounds": 1})

    def test_the_docs_example_manifest_parses(self):
        m = B.parse(os.path.join(HERE, "examples", "example-loose.toml"))
        self.assertEqual(m.name, "example-loose")
        self.assertEqual(len(m.arms), 2)
        self.assertEqual(m.stop, {"calls_left_min": 200, "deadline": "07:30"})
        self.assertEqual(m.smoke, {"tasks": ("hello-go", "split-module-python", "duration-python"), "rounds": 1})


class DryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "m.toml")
        with open(path, "w") as f:
            f.write(VALID)
        self.manifest = B.parse(path)

    def test_contains_sha256_sequence_and_not_checked(self):
        lines = B.dry_run(self.manifest, ["run"], bench=None)
        joined = "\n".join(lines)
        self.assertIn(self.manifest.sha256, joined)
        self.assertIn("NOT CHECKED: envelope file", joined)
        self.assertIn("NOT CHECKED: bench checkout", joined)
        self.assertIn("round 1 pipeline:", joined)
        self.assertIn("round 2 session:", joined)
        self.assertIn(B.shift_command(self.manifest, self.manifest.arms[0]), joined)

    def test_other_phases_print_not_built(self):
        lines = B.dry_run(self.manifest, ["bootstrap", "run", "report"], bench=None)
        self.assertIn("bootstrap: NOT BUILT", lines)
        self.assertIn("report: NOT BUILT", lines)
        self.assertFalse(any(l == "run: NOT BUILT" for l in lines))

    def test_prints_the_four_orgs(self):
        lines = B.dry_run(self.manifest, ["run"])
        self.assertTrue(any(l.startswith("orgs:") and "work=w" in l and "results=res" in l for l in lines))

    def test_bench_checkout_found_when_tasks_present(self):
        bench_dir = os.path.join(self.tmp, "bench")
        os.makedirs(os.path.join(bench_dir, "tasks", "t1"))
        os.makedirs(os.path.join(bench_dir, "tasks", "t2"))
        lines = B.dry_run(self.manifest, ["run"], bench=bench_dir)
        self.assertTrue(any("has all 2 task(s)" in l for l in lines))
        self.assertFalse(any("NOT CHECKED: bench checkout" in l for l in lines))


def make_manifest(**kw):
    base = dict(path="/tmp/m.toml", sha256="abc123", name="exp1", question="q",
                tasks=("t1", "t2"), judge="acceptance-v4", envelope="frozen/e.toml",
                rounds=2, order="alternate",
                arms=(B.Arm(name="pipeline", executor="agent", tier="tier-a", think="low"),
                      B.Arm(name="session", executor="session", tier="tier-a", think="low")),
                orgs={"work": "w", "archive": "a", "records": "r", "results": "res"},
                smoke=None, stop=None)
    base.update(kw)
    return B.Manifest(**base)


class FakeRunShift:
    def __init__(self):
        self.calls = []

    def __call__(self, round_no, arm):
        self.calls.append((round_no, arm.name))
        return f"shift-{round_no}-{arm.name}"


def make_verify(mismatch_at=None, not_derivable_at=None):
    def _verify(shift_id):
        lines = [f"== shift {shift_id}"]
        mismatch = shift_id == mismatch_at
        not_derivable = shift_id == not_derivable_at
        if mismatch:
            lines.append("MISMATCH        something disagrees")
        if not_derivable:
            lines.append("NOT DERIVABLE   nothing to read")
        return lines, mismatch, not_derivable
    return _verify


class RunPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "ledger.jsonl"), clock=Clock())
        self.manifest = make_manifest()

    def test_rows_written_in_order_every_round_verified(self):
        run_shift = FakeRunShift()
        logs = []
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [(1, "pipeline"), (1, "session"), (2, "pipeline"), (2, "session")])
        events = self.led.events()
        self.assertEqual(events[0]["kind"], "bench.start")
        self.assertEqual(events[0]["manifest_sha256"], "abc123")
        self.assertEqual(events[0]["tests_version"], "tv1")
        self.assertEqual(events[0]["envelope_sha256"], "env1")
        rounds = self.led.events("bench.round")
        self.assertEqual(len(rounds), 4)
        self.assertTrue(all(r["verified"] for r in rounds))
        self.assertEqual([(r["round"], r["arm"]) for r in rounds],
                         [(1, "pipeline"), (1, "session"), (2, "pipeline"), (2, "session")])
        self.assertTrue(any(l.startswith("PASS round 1 pipeline: shift shift-1-pipeline") for l in logs))

    def test_bench_start_written_once(self):
        run_shift = FakeRunShift()
        B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=lambda *_: None)
        self.assertEqual(len(self.led.events("bench.start")), 1)

    def test_mismatch_ends_the_phase_with_fail_and_stops_launching(self):
        run_shift = FakeRunShift()
        verify = make_verify(mismatch_at="shift-1-session")
        logs = []
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, verify, log=logs.append)
        self.assertFalse(ok)
        self.assertEqual(run_shift.calls, [(1, "pipeline"), (1, "session")])
        rounds = self.led.events("bench.round")
        self.assertEqual(len(rounds), 2)
        self.assertFalse(rounds[-1]["verified"])
        self.assertTrue(any(l.startswith("FAIL round 1 session") for l in logs))

    def test_not_derivable_counts_the_round_and_continues(self):
        run_shift = FakeRunShift()
        verify = make_verify(not_derivable_at="shift-1-pipeline")
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, verify, log=lambda *_: None)
        self.assertTrue(ok)
        rounds = self.led.events("bench.round")
        self.assertEqual(len(rounds), 4)
        self.assertTrue(rounds[0]["verified"])

    def test_calls_left_min_stops_before_the_first_round(self):
        m = make_manifest(stop={"calls_left_min": 100})
        run_shift = FakeRunShift()
        logs = []
        ok = B.run_phase(m, self.led, "tv1", "env1", run_shift, make_verify(),
                         calls_left=lambda arm: 50, log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [])
        self.assertTrue(any("stopping before round 1 arm pipeline" in l for l in logs))

    def test_calls_left_above_the_floor_does_not_stop(self):
        m = make_manifest(stop={"calls_left_min": 100})
        run_shift = FakeRunShift()
        ok = B.run_phase(m, self.led, "tv1", "env1", run_shift, make_verify(),
                         calls_left=lambda arm: 500, log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(len(run_shift.calls), 4)

    def test_deadline_stops_before_the_first_round(self):
        m = make_manifest(stop={"deadline": "07:30"})
        run_shift = FakeRunShift()
        logs = []
        ok = B.run_phase(m, self.led, "tv1", "env1", run_shift, make_verify(),
                         now_hhmm=lambda: "08:00", log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [])
        self.assertTrue(any("past deadline 07:30" in l for l in logs))

    def test_deadline_not_yet_reached_does_not_stop(self):
        m = make_manifest(stop={"deadline": "23:59"})
        run_shift = FakeRunShift()
        ok = B.run_phase(m, self.led, "tv1", "env1", run_shift, make_verify(),
                         now_hhmm=lambda: "06:00", log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(len(run_shift.calls), 4)


class PauseResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "ledger.jsonl"), clock=Clock())
        self.manifest = make_manifest()

    def start(self):
        self.led.emit("bench.start", name="exp1", manifest_sha256="abc123",
                      tests_version="tv1", envelope_sha256="env1")

    def test_not_paused_with_no_pause_event(self):
        self.start()
        self.assertFalse(B.is_paused(self.led, "exp1"))

    def test_paused_after_a_pause_following_start(self):
        self.start()
        self.led.emit("bench.pause", name="exp1")
        self.assertTrue(B.is_paused(self.led, "exp1"))

    def test_a_pause_before_start_does_not_count(self):
        self.led.emit("bench.pause", name="exp1")
        self.start()
        self.assertFalse(B.is_paused(self.led, "exp1"))

    def test_resume_after_pause_unpauses(self):
        self.start()
        self.led.emit("bench.pause", name="exp1")
        self.led.emit("bench.resume", name="exp1")
        self.assertFalse(B.is_paused(self.led, "exp1"))

    def test_run_phase_stops_cleanly_when_paused(self):
        self.start()
        self.led.emit("bench.pause", name="exp1")
        run_shift = FakeRunShift()
        logs = []
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [])
        self.assertTrue(any(l == "PAUSED before round 1 arm pipeline" for l in logs))

    def test_resume_skips_rounds_already_verified(self):
        self.start()
        self.led.emit("bench.round", name="exp1", manifest_sha256="abc123", round=1, arm="pipeline",
                      shift="shift-1-pipeline", verified=True)
        run_shift = FakeRunShift()
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [(1, "session"), (2, "pipeline"), (2, "session")])

    def test_a_verified_false_round_is_not_skipped(self):
        self.start()
        self.led.emit("bench.round", name="exp1", manifest_sha256="abc123", round=1, arm="pipeline",
                      shift="shift-1-pipeline", verified=False)
        run_shift = FakeRunShift()
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(run_shift.calls, [(1, "pipeline"), (1, "session"), (2, "pipeline"), (2, "session")])

    def test_a_verified_round_of_a_different_manifest_sha_is_not_skipped(self):
        self.start()
        self.led.emit("bench.round", name="exp1", manifest_sha256="different-sha", round=1, arm="pipeline",
                      shift="shift-1-pipeline", verified=True)
        run_shift = FakeRunShift()
        ok = B.run_phase(self.manifest, self.led, "tv1", "env1", run_shift, make_verify(), log=lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(len(run_shift.calls), 4)


if __name__ == "__main__":
    unittest.main()

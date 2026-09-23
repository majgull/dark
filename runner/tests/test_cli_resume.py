"""dark done / dark envelope: what a session arm needs to resume.

A session arm is one chat session solving one task outside the runner; it
has no ledger of its own, so it asks the runner two questions before it
starts a step: does this shift already hold a verdict for the task (then
the step is not run again, and the branch it delivered goes to the next
step), and what wall cap would the pipeline have given this run.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from dark import __main__ as M
from dark import ledger as L
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_ledger import run_end
from tests.test_tasks import make_task


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = write_conf(self.tmp, MODELS, BUDGETS)
        self.state = os.path.join(self.tmp, "state")
        self.bench = os.path.join(self.tmp, "bench")
        make_task(self.bench, "obs-01")
        make_task(self.bench, "obs-02", extra='after = "obs-01"\n')
        tok = os.path.join(self.tmp, "tok")
        with open(tok, "w") as f:
            f.write("t\n")
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{self.state}"\nbench_dir = "{self.bench}"\n'
                    f'templates_dir = "{self.tmp}/templates"\n'
                    f'admin_token_file = "{tok}"\nagent_token_file = "{tok}"\n')
        self.led = L.Ledger(os.path.join(self.state, "ledger.jsonl"))

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["--conf", self.conf, *argv])
        out = buf.getvalue().strip().splitlines()
        return rc, json.loads(out[-1]) if out else None

    def record(self, task, outcome, shift, branch="run/b1", arm=None, run="r1"):
        self.led.emit("run.end", **run_end(task=task, run=run, outcome=outcome, shift=shift,
                                           branch=branch, arm=arm))


class Done(Base):
    def test_a_pass_in_this_shift_is_resumable_and_names_its_branch(self):
        self.record("obs-01", "pass", "s1", branch="run/session-dsf-1")
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual(rc, 0)
        self.assertTrue(out["resumable"])
        self.assertEqual(out["branch"], "run/session-dsf-1")

    def test_a_failure_is_not_resumable(self):
        self.record("obs-01", "fail:capability", "s1")
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual((rc, out["resumable"], out["outcome"]), (1, False, "fail:capability"))

    def test_nothing_recorded_is_not_resumable(self):
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual((rc, out["resumable"], out["run"]), (1, False, None))

    def test_a_pass_of_another_shift_does_not_count(self):
        self.record("obs-01", "pass", "other")
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual((rc, out["resumable"]), (1, False))

    def test_a_voided_pass_does_not_count(self):
        self.record("obs-01", "pass", "s1", run="r9")
        self.led.emit("run.void", task="obs-01", run="r9", reason="harness fault")
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual((rc, out["resumable"]), (1, False))

    def test_the_arm_filter_keeps_two_arms_of_one_shift_apart(self):
        self.record("obs-01", "pass", "s1", arm="session-dsf", run="r1")
        self.record("obs-01", "fail:capability", "s1", arm="session-sonnet", run="r2")
        self.assertEqual(self.run_cli("done", "--shift", "s1", "--task", "obs-01",
                                      "--arm", "session-dsf")[0], 0)
        self.assertEqual(self.run_cli("done", "--shift", "s1", "--task", "obs-01",
                                      "--arm", "session-sonnet")[0], 1)

    def test_the_last_record_wins(self):
        self.record("obs-01", "fail:capability", "s1", run="r1")
        self.record("obs-01", "pass", "s1", run="r2", branch="run/second")
        rc, out = self.run_cli("done", "--shift", "s1", "--task", "obs-01")
        self.assertEqual((rc, out["run"], out["branch"]), (0, "r2", "run/second"))


class EnvelopeCmd(Base):
    def test_the_wall_cap_is_the_runs_seconds_plus_the_judging_time(self):
        rc, out = self.run_cli("envelope", "--task", "obs-01", "--tier", "local-a")
        self.assertEqual(rc, 0)
        self.assertEqual(out["wall_seconds"], out["seconds"] + out["stage_timeout"])
        self.assertGreater(out["seconds"], 0)
        self.assertEqual((out["task"], out["tier"]), ("obs-01", "local-a"))

    def test_an_unknown_task_is_a_message_not_a_traceback(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["--conf", self.conf, "envelope", "--task", "nope", "--tier", "local-a"])
        self.assertEqual(rc, 2)
        self.assertIn("tasks:", buf.getvalue())


class LaunchRecord(Base):
    """--resume repeats the launch it continues (the arguments recorded in
    shift.start), so a resumed round cannot change tier or thinking level
    halfway: the first resume smoke re-ran its tasks at think=low because
    the resuming command line simply did not say --think."""

    REC = {"tasks": ["obs-01"], "task_dir": None, "tier": "local-a", "think": "none",
           "arm": "smoke-local", "slot": 0, "work_org": "dark-runs"}

    def launch(self, shift="s1", **kw):
        rec = dict(self.REC, **kw)
        self.led.emit("shift.start", shift=shift, host="h", launch=rec)
        return rec

    def args(self, **kw):
        return SimpleNamespace(**dict({"resume": "s1", "tasks": None, "task_dir": None, "tier": None,
                                       "think": None, "arm": None, "slot": None}, **kw))

    def merge(self, args, work_org="dark-runs"):
        return M._resume_launch(args, self.led, SimpleNamespace(work_org=work_org))

    def test_resume_without_arguments_reproduces_the_launch(self):
        self.launch()
        a = self.args()
        self.assertIsNone(self.merge(a))
        self.assertEqual((a.tasks, a.tier, a.think, a.arm, a.slot),
                         ("obs-01", "local-a", "none", "smoke-local", 0))

    def test_a_differing_think_is_refused_with_both_values(self):
        self.launch()
        why = self.merge(self.args(think="low"))
        self.assertIn("think", why)
        self.assertIn("'low'", why)
        self.assertIn("'none'", why)

    def test_a_differing_tier_is_refused(self):
        self.launch()
        self.assertIn("tier", self.merge(self.args(tier="cloud-b")) or "")

    def test_repeating_the_launchs_own_values_is_allowed(self):
        self.launch()
        a = self.args(tier="local-a", think="none", arm="smoke-local", slot=0, tasks="obs-01")
        self.assertIsNone(self.merge(a))
        self.assertEqual(a.think, "none")

    def test_a_differing_task_list_is_refused(self):
        self.launch()
        self.assertIn("tasks", self.merge(self.args(tasks="obs-02")) or "")

    def test_a_differing_work_org_is_refused(self):
        self.launch()
        why = self.merge(self.args(), work_org="dark-elsewhere")
        self.assertIn("work_org", why)
        self.assertIn("dark-runs", why)

    def test_a_shift_that_recorded_no_launch_is_refused(self):
        self.led.emit("shift.start", shift="s1", host="h")
        self.assertIn("no launch arguments", self.merge(self.args()) or "")


if __name__ == "__main__":
    unittest.main()

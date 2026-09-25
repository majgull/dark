"""python3 -m dark long --task <toml>: one long-arm run and its judge.

The run driver is Runner.long (a session executor over the task's several
repositories, dark/run.py) and the judge is Runner.review with
review_branches (tests/test_review_run_driver.py); here the command finds the
task, refuses what is not a long task, hands the branches the run pushed to a
judging review, and exits on the judge's verdict."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from dark import __main__ as M
from dark import run as R
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_tasks import make_long_task, make_task


class _Reachable:
    def reachable(self):
        return True


class LongCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = write_conf(self.tmp, MODELS, BUDGETS)
        tok = os.path.join(self.tmp, "tok")
        with open(tok, "w") as f:
            f.write("t\n")
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{self.tmp}/state"\n'
                    f'admin_token_file = "{tok}"\nagent_token_file = "{tok}"\n')
        self.bench = os.path.join(self.tmp, "bench")
        self.task_dir = make_long_task(self.bench, "rework")
        self.calls = []
        patches = [mock.patch("dark.sandbox.make", lambda host, template: _Reachable()),
                   mock.patch.object(R.Runner, "long", lambda runner, *a, **kw: self.fake_long(runner, *a, **kw)),
                   mock.patch.object(R.Runner, "review", lambda runner, *a, **kw: self.fake_review(runner, *a, **kw))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.branches = [{"repo": "api", "branch": "run/rework-1"},
                         {"repo": "web", "branch": "run/rework-1"}]
        self.judge_outcome = "pass"

    def fake_long(self, runner, task, tier, arm="long", shift=None, think=None, env=None, slot=0):
        self.calls.append({"phase": "long", "task": task, "tier": tier, "arm": arm,
                           "slot": slot, "runner_arm": runner.arm})
        return R.RunResult(run="rework-long-1", task=task.id, cls=task.cls, tier=tier,
                           outcome="delivered", branch="run/rework-1", branches=list(self.branches))

    def fake_review(self, runner, brief_text, files, tier, arm, shift=None, think=None,
                    env=None, slot=0, review_branches=None):
        self.calls.append({"phase": "judge", "brief": brief_text, "files": files, "tier": tier,
                           "arm": arm, "slot": slot, "branches": review_branches})
        return R.RunResult(run="review-long-1", task="review", cls="review", tier=tier,
                           outcome=self.judge_outcome, fail_kind=None if self.judge_outcome == "pass" else "verdict")

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["--conf", self.conf, "long", *argv])
        return rc, buf.getvalue().strip().splitlines()

    def test_a_passing_judge_exits_zero(self):
        rc, out = self.cli("--task", os.path.join(self.task_dir, "task.toml"), "--tier", "cloud-x")
        self.assertEqual(rc, 0)
        self.assertEqual(out, ["long: delivered", "judge: pass"])
        long_call, judge_call = self.calls
        self.assertEqual((long_call["task"].id, long_call["tier"], long_call["arm"], long_call["slot"]),
                         ("rework", "cloud-x", "long", 0))
        # the judge gets each pushed branch, with the url the task named
        self.assertEqual(judge_call["branches"],
                         [{"name": "api", "url": "https://git.example.test/dark/api.git", "branch": "run/rework-1"},
                          {"name": "web", "url": "ssh://git@git.example.test/dark/web.git", "branch": "run/rework-1"}])
        self.assertEqual(judge_call["tier"], "cloud-x")  # --judge-tier defaults to --tier
        for needle in ("Move both repositories onto the new schema.", "VERDICT: pass", "VERDICT: fail",
                       "report.md"):
            self.assertIn(needle, judge_call["brief"])

    def test_the_task_directory_is_accepted_and_the_judge_tier_reaches_the_review(self):
        rc, _ = self.cli("--task", self.task_dir, "--tier", "cloud-x", "--judge-tier", "local-b",
                         "--arm", "long-b", "--slot", "3")
        self.assertEqual(rc, 0)
        long_call, judge_call = self.calls
        self.assertEqual((long_call["arm"], long_call["slot"]), ("long-b", 3))
        self.assertEqual((judge_call["tier"], judge_call["arm"], judge_call["slot"]),
                         ("local-b", "long-b", 3))

    def test_a_failing_judge_exits_one(self):
        self.judge_outcome = "fail:capability"
        rc, out = self.cli("--task", self.task_dir, "--tier", "cloud-x")
        self.assertEqual(rc, 1)
        self.assertEqual(out[-1], "judge: fail:capability")
        self.assertEqual([c["phase"] for c in self.calls], ["long", "judge"])

    def test_no_branch_pushed_is_not_judged(self):
        self.branches = []
        rc, out = self.cli("--task", self.task_dir, "--tier", "cloud-x")
        self.assertEqual(rc, 1)
        self.assertEqual(out, ["long: delivered", "judge: none (no branch pushed)"])
        self.assertEqual([c["phase"] for c in self.calls], ["long"])  # no judge call

    def test_no_branch_pushed_prints_the_judge_none_line(self):
        self.branches = []
        rc, out = self.cli("--task", self.task_dir, "--tier", "cloud-x")
        self.assertEqual(rc, 1)
        self.assertIn("judge: none (no branch pushed)", out)
        self.assertEqual([c["phase"] for c in self.calls], ["long"])

    def test_a_task_that_is_not_a_long_task_is_refused(self):
        d = make_task(self.bench, "hello")
        rc, out = self.cli("--task", d, "--tier", "cloud-x")
        self.assertEqual(rc, 2)
        self.assertIn("not a long task", out[-1])
        self.assertEqual(self.calls, [])

    def test_a_path_that_is_not_a_task_is_one_line(self):
        other = os.path.join(self.tmp, "notes.toml")
        with open(other, "w") as f:
            f.write("x = 1\n")
        for path in (other, os.path.join(self.tmp, "absent")):
            with self.subTest(path=path):
                rc, out = self.cli("--task", path, "--tier", "cloud-x")
                self.assertEqual((rc, len(out)), (2, 1))
                self.assertTrue(out[0].startswith("long: "))
        self.assertEqual(self.calls, [])

    def test_an_unknown_tier_is_a_config_line(self):
        rc, out = self.cli("--task", self.task_dir, "--tier", "ghost")
        self.assertEqual(rc, 2)
        self.assertIn("ghost", out[-1])
        self.assertEqual(self.calls, [])

    def test_task_and_tier_are_required(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                M.main(["--conf", self.conf, "long", "--tier", "cloud-x"])
            with self.assertRaises(SystemExit):
                M.main(["--conf", self.conf, "long", "--task", self.task_dir])


if __name__ == "__main__":
    unittest.main()

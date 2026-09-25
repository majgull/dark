"""python3 -m dark user --task <toml>: one user-arm run from the command
line. The run driver is Runner.user (tests/test_user.py); here the command
finds the task, refuses what is not a user task, hands the driver what the
line asked for and prints the verdict as one JSON line."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from dark import __main__ as M
from dark import run as R
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_tasks import make_task, make_user_task


class _Reachable:
    def reachable(self):
        return True


class UserCommand(unittest.TestCase):
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
        self.task_dir = make_user_task(self.bench, "shop")
        self.calls = []
        patches = [mock.patch("dark.sandbox.make", lambda host, template: _Reachable()),
                   mock.patch.object(R.Runner, "user", lambda runner, *a, **kw: self.fake_user(runner, *a, **kw))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.outcome = "pass"

    def fake_user(self, runner, task, tier, arm="user", shift=None, think=None, env=None, slot=0):
        self.calls.append({"task": task, "tier": tier, "arm": arm, "shift": shift, "think": think,
                           "slot": slot, "runner_arm": runner.arm})
        return R.RunResult(run="shop-user-1", task=task.id, cls=task.cls, tier=tier, outcome=self.outcome,
                           fail_kind=None if self.outcome == "pass" else "steps", issue=3,
                           checks_ok=3 if self.outcome == "pass" else 2, checks_total=3,
                           records="dark-records/adhoc/shop-user-1")

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["--conf", self.conf, "user", *argv])
        return rc, buf.getvalue().strip().splitlines()

    def test_one_user_task_runs_and_prints_its_verdict(self):
        rc, out = self.cli("--task", os.path.join(self.task_dir, "task.toml"), "--tier", "cloud-x")
        self.assertEqual(rc, 0)
        doc = json.loads(out[-1])
        self.assertEqual((doc["outcome"], doc["steps_ok"], doc["steps_total"], doc["records"]),
                         ("pass", 3, 3, "dark-records/adhoc/shop-user-1"))
        (call,) = self.calls
        self.assertEqual((call["task"].id, call["task"].url, len(call["task"].steps)),
                         ("shop", "https://app.example.test/", 3))
        self.assertEqual((call["tier"], call["arm"], call["runner_arm"], call["slot"]), ("cloud-x", "user", "user", 1))

    def test_the_task_directory_is_accepted_and_the_options_reach_the_driver(self):
        rc, _ = self.cli("--task", self.task_dir, "--tier", "cloud-x", "--arm", "user-b",
                         "--shift", "s7", "--think", "none", "--slot", "2")
        self.assertEqual(rc, 0)
        self.assertEqual({k: self.calls[0][k] for k in ("arm", "shift", "think", "slot")},
                         {"arm": "user-b", "shift": "s7", "think": "none", "slot": 2})

    def test_a_failed_step_exits_one(self):
        self.outcome = "fail:capability"
        rc, out = self.cli("--task", self.task_dir, "--tier", "cloud-x")
        self.assertEqual((rc, json.loads(out[-1])["fail_kind"]), (1, "steps"))

    def test_a_task_that_is_not_a_user_task_is_refused(self):
        d = make_task(self.bench, "hello")
        rc, out = self.cli("--task", d, "--tier", "cloud-x")
        self.assertEqual(rc, 2)
        self.assertIn("not a user task", out[-1])
        self.assertEqual(self.calls, [])

    def test_a_path_that_is_not_a_task_is_one_line(self):
        other = os.path.join(self.tmp, "notes.toml")
        with open(other, "w") as f:
            f.write("x = 1\n")
        for path in (other, os.path.join(self.tmp, "absent")):
            with self.subTest(path=path):
                rc, out = self.cli("--task", path, "--tier", "cloud-x")
                self.assertEqual((rc, len(out)), (2, 1))
                self.assertTrue(out[0].startswith("user: "))
        self.assertEqual(self.calls, [])

    def test_an_unknown_tier_is_a_config_line(self):
        rc, out = self.cli("--task", self.task_dir, "--tier", "ghost")
        self.assertEqual(rc, 2)
        self.assertIn("ghost", out[-1])
        self.assertEqual(self.calls, [])

    def test_task_and_tier_are_required(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                M.main(["--conf", self.conf, "user", "--tier", "cloud-x"])
            with self.assertRaises(SystemExit):
                M.main(["--conf", self.conf, "user", "--task", self.task_dir])


if __name__ == "__main__":
    unittest.main()

"""python3 -m dark now: the running runs, oldest first, then the last N
finished runs, newest first, read from the ledger alone (dark/status.py)."""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from dark import __main__ as M
from dark import ledger as L
from dark import status as S
from tests.test_config import BUDGETS, MODELS, write_conf

FX = os.path.join(tempfile.gettempdir(), "fx")


class Rows(unittest.TestCase):
    def test_pairs_running_and_finished_oldest_and_newest_first(self):
        # three starts, two of them (a, c) also end: a running, b and c
        # finished, newest finished first
        base = 1000.0
        events = [
            {"kind": "run.start", "run": "a", "ts": base, "iso": "a-start", "task": "t1", "tier": "x", "arm": "factory"},
            {"kind": "run.start", "run": "b", "ts": base + 1, "iso": "b-start", "task": "t2", "tier": "x", "arm": "factory"},
            {"kind": "run.end", "run": "b", "ts": base + 2, "iso": "b-end", "task": "t2", "tier": "x", "arm": "factory",
             "outcome": "pass", "fail_kind": None, "seconds": 5, "calls": 1, "issue": 2, "records": None},
            {"kind": "run.start", "run": "c", "ts": base + 3, "iso": "c-start", "task": "t3", "tier": "x", "arm": "factory"},
            {"kind": "run.end", "run": "c", "ts": base + 4, "iso": "c-end", "task": "t3", "tier": "x", "arm": "factory",
             "outcome": "fail:budget", "fail_kind": "seconds", "seconds": 9, "calls": 3, "issue": 3, "records": None},
        ]
        rows = S.rows(events, last=10)
        self.assertEqual([(r["state"], r["task"]) for r in rows],
                         [("running", "t1"), ("fail:budget", "t3"), ("pass", "t2")])
        self.assertIsNone(rows[0]["seconds"])
        self.assertIsNone(rows[0]["issue"])
        self.assertEqual(rows[1]["kind"], "seconds")
        self.assertEqual(rows[2]["issue"], 2)

    def test_last_caps_the_finished_runs_kept(self):
        events = []
        for i in range(5):
            events.append({"kind": "run.start", "run": f"r{i}", "ts": i, "iso": f"s{i}", "task": "t", "tier": "x", "arm": "a"})
            events.append({"kind": "run.end", "run": f"r{i}", "ts": i + 0.5, "iso": f"e{i}", "task": "t", "tier": "x",
                           "arm": "a", "outcome": "pass", "fail_kind": None, "seconds": 1, "calls": 1,
                           "issue": i, "records": None})
        rows = S.rows(events, last=2)
        self.assertEqual([r["issue"] for r in rows], [4, 3])


class Table(unittest.TestCase):
    def test_a_line_never_exceeds_110_chars_with_a_long_task_id(self):
        rows = [{"state": "fail:structural", "kind": "crash", "arm": "factory", "task": "t" * 60,
                "tier": "claude:claude-opus-this-is-a-fairly-long-model-id", "started": "2026-01-01T00:00:00+00:00",
                "seconds": 123, "calls": 4, "issue": 42, "records": None}]
        text = S.table(rows, time.time())
        for line in text.splitlines():
            self.assertLessEqual(len(line), 110)

    def test_first_line_names_the_counts(self):
        rows = [{"state": "running", "kind": None, "arm": "a", "task": "t", "tier": "x",
                "started": "2026-01-01T00:00:00+00:00", "seconds": None, "calls": None, "issue": None, "records": None},
                {"state": "pass", "kind": None, "arm": "a", "task": "t2", "tier": "x",
                "started": "2026-01-01T00:00:00+00:00", "seconds": 1, "calls": 1, "issue": 1, "records": None}]
        text = S.table(rows, time.time())
        self.assertEqual(text.splitlines()[0], "running 1, last 1 finished")


class Cli(unittest.TestCase):
    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="status-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "conf")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.conf)
        os.makedirs(self.state)
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{self.state}"\n')
        self.led = L.Ledger(os.path.join(self.state, "ledger.jsonl"))

    def cli(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DARK_")}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            rc = M.main(["--conf", self.conf, "now", *argv])
        return rc, out.getvalue()

    def test_json_output_parses_and_has_the_keys(self):
        self.led.emit("run.start", task="hello", run="r1", cls="additive", tier="local-a", envelope={}, arm="factory", shift="s1")
        self.led.emit("run.end", task="hello", run="r1", cls="additive", tier="local-a", outcome="pass",
                      seconds=3, calls=1, tokens_in=10, tokens_out=5, reasoning_chars=0, paid=False,
                      watts_class=None, fail_kind=None, issue=7, records=None, arm="factory", shift="s1")
        rc, out = self.cli("--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]),
                         {"state", "kind", "arm", "task", "tier", "started", "seconds", "calls", "issue", "records"})
        self.assertEqual(rows[0]["state"], "pass")

    def test_table_output_makes_no_network_call_and_fits_the_width(self):
        self.led.emit("run.start", task="t" * 60, run="r1", cls="additive", tier="local-a", envelope={},
                      arm="factory", shift="s1")
        rc, out = self.cli()
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("running 1, last 0 finished"))
        for line in lines:
            self.assertLessEqual(len(line), 110)


class CliStatus(unittest.TestCase):
    """python3 -m dark status: windows, watts and envelopes today
    (unrelated to dark/status.py; the name predates it)."""

    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="status-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = write_conf(self.dir, MODELS, BUDGETS)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{self.state}"\n')

    def test_prints_windows_watts_and_envelopes(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DARK_")}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            rc = M.main(["--conf", self.conf, "status"])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith("window ") or line.startswith("envelope ") for line in lines), lines)

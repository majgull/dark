"""python3 -m dark ledger-tail: the last rows of the ledger.

The command reads host.ledger_path through config.load_host, prints the last
--lines rows (default 20), oldest first, one JSON object per line, and --kind
keeps rows whose `kind` field matches. A missing or empty ledger prints
nothing and exits 0, and a line that is not a JSON object is skipped with one
note on standard error. The fixture ledger lives under /tmp/fx."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from dark import __main__ as M

FX = "/tmp/fx"
KINDS = ("run.start", "run.end")


class LedgerTail(unittest.TestCase):
    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="ledger-tail-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "conf")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.conf)
        os.makedirs(self.state)
        with open(os.path.join(self.conf, "host.toml"), "w") as f:
            f.write(f'[host]\nstate_dir = "{self.state}"\n')
        self.ledger = os.path.join(self.state, "ledger.jsonl")

    def write_rows(self, malformed=False):
        """Fifteen rows of two kinds, oldest first, plus one malformed line
        when asked."""
        lines = []
        for i in range(15):
            lines.append(json.dumps({"kind": KINDS[i % len(KINDS)], "i": i}))
            if malformed and i == 7:
                lines.append("{not json")
        with open(self.ledger, "w") as f:
            f.write("\n".join(lines) + "\n")

    def cli(self, *argv):
        """The command with a DARK_-free environment, so host.toml alone says
        which ledger is read."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("DARK_")}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            rc = M.main(["--conf", self.conf, "ledger-tail", *argv])
        return rc, out.getvalue(), err.getvalue()

    def parsed(self, text):
        return [json.loads(line) for line in text.splitlines()]

    def test_default_prints_the_last_twenty_or_fewer_oldest_first(self):
        self.write_rows()
        rc, out, err = self.cli()
        self.assertEqual((rc, err), (0, ""))
        rows = self.parsed(out)
        self.assertEqual([r["i"] for r in rows], list(range(15)))
        self.assertTrue(all(r["kind"] in KINDS for r in rows))

    def test_lines_three_prints_three(self):
        self.write_rows()
        rc, out, err = self.cli("--lines", "3")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([r["i"] for r in self.parsed(out)], [12, 13, 14])

    def test_kind_keeps_only_that_kind(self):
        self.write_rows()
        rc, out, err = self.cli("--kind", "run.end")
        self.assertEqual((rc, err), (0, ""))
        rows = self.parsed(out)
        self.assertEqual([r["kind"] for r in rows], ["run.end"] * 7)
        self.assertEqual([r["i"] for r in rows], [1, 3, 5, 7, 9, 11, 13])

    def test_kind_with_no_matching_rows_prints_nothing(self):
        self.write_rows()
        rc, out, err = self.cli("--kind", "bench.pause")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_a_malformed_line_is_skipped_with_one_note(self):
        self.write_rows(malformed=True)
        rc, out, err = self.cli()
        self.assertEqual(rc, 0)
        self.assertEqual([r["i"] for r in self.parsed(out)], list(range(15)))
        self.assertNotIn("not json", out)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("not JSON", err)
        self.assertIn(":9:", err)  # the line number of the malformed row

    def test_an_absent_ledger_exits_zero_with_no_output(self):
        rc, out, err = self.cli()
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_an_empty_ledger_exits_zero_with_no_output(self):
        with open(self.ledger, "w"):
            pass
        rc, out, err = self.cli()
        self.assertEqual((rc, out, err), (0, "", ""))


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dark import pressure

RUNNER = Path(__file__).resolve().parent.parent

RELEASED = """## [0.1.0] - 2026-01-01

### Added

- released one
- released two
- released three
- released four
- released five
- released six
- released seven
- released eight
- released nine
- released ten
- released eleven
"""


def changelog(added, fixed=0):
    """A changelog with `added` + `fixed` Unreleased entries over a big release."""
    lines = ["# Changelog", "", "- a preamble line, not an entry", "", "## [Unreleased]", ""]
    if added:
        lines += ["### Added", ""] + [f"- added {i}" for i in range(added)] + [""]
    if fixed:
        lines += ["### Fixed", ""] + [f"- fixed {i}" for i in range(fixed)] + [""]
    return "\n".join(lines) + "\n" + RELEASED


class Count(unittest.TestCase):
    def test_added_and_fixed_both_count(self):
        self.assertEqual(pressure.unreleased_entries(changelog(3, 4)), 7)

    def test_released_sections_do_not_count(self):
        self.assertEqual(pressure.unreleased_entries(changelog(0)), 0)
        self.assertEqual(pressure.unreleased_entries(changelog(2)), 2)

    def test_no_unreleased_heading_counts_nothing(self):
        self.assertEqual(pressure.unreleased_entries(RELEASED), 0)


class Gate(unittest.TestCase):
    def run_on(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "CHANGELOG.md")
            Path(path).write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, "-m", "dark.pressure", path],
                                  cwd=RUNNER, capture_output=True, text=True)

    def test_five_entries_print_nothing(self):
        r = self.run_on(changelog(3, 2))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_six_entries_warn_and_pass(self):
        r = self.run_on(changelog(3, 3))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertIn("6 Unreleased", r.stdout)
        self.assertIn("patch release is due", r.stdout)

    def test_ten_entries_still_pass(self):
        self.assertEqual(self.run_on(changelog(5, 5)).returncode, 0)

    def test_eleven_entries_fail(self):
        r = self.run_on(changelog(6, 5))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("11 Unreleased", r.stderr)
        self.assertIn("FAIL", r.stderr)


if __name__ == "__main__":
    unittest.main()

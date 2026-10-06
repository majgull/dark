"""dark/critic.py: a run's records checked before a person sees its demo.

One clean fixture gives no findings; one fixture per rule gives exactly that
finding. The fixtures are written into a temp directory: the screenshots are
built by `tests/pngfix.py`, because the tree keeps no binary fixture.
"""

import json
import os
import shutil
import tempfile
import unittest

from dark import critic
from tests import pngfix

STEPS = ["Open the start page.", "Open the printers.", "Say what is odd."]
BADGE = {"pass": "🟢 PASS", "fail": "🔴 FAIL", "inconclusive": "🟡 INCONCLUSIVE"}


def shot(n):
    """A screenshot that is not one colour and differs from the other steps'."""
    return pngfix.png(6, 4, lambda x, y: ((x * 40 + n * 17) % 256, (y * 50 + n * 3) % 256, (n * 20) % 256))


def write_records(d, verdicts, *, steps=STEPS, shots="all", flat=(), bad=(), readme="auto"):
    """One records directory. `verdicts` is the steps.jsonl rows; `shots` is
    "all", "none" or the step numbers to write a screenshot for; `flat` and
    `bad` name steps whose PNG is one colour or not a PNG; `readme` is the
    outcome badge to claim, None for no README.md, "auto" for the verdicts'."""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump({"task": "t-critic", "steps": list(steps)}, f)
    with open(os.path.join(d, "steps.jsonl"), "w") as f:
        f.write("".join(json.dumps(v) + "\n" for v in verdicts))
    sdir = os.path.join(d, "steps")
    os.makedirs(sdir, exist_ok=True)
    for v in verdicts:
        n = v.get("step")
        if not isinstance(n, int) or shots == "none" or (shots != "all" and n not in shots):
            continue
        name = os.path.join(sdir, f"{n:02d}.png")
        with open(name, "wb") as f:
            f.write(b"not a png" if n in bad else
                    pngfix.png(6, 4, (10, 20, 30)) if n in flat else shot(n))
    if readme == "auto":
        readme = critic.verdict_outcome(verdicts)
    if readme is not None:
        with open(os.path.join(d, "README.md"), "w") as f:
            f.write(f"# run\n\n**{BADGE[readme]}**: the tally\n")
    return d


def passes(*notes):
    return [{"step": i, "verdict": "pass", "note": note} for i, note in enumerate(notes, 1)]


class Checks(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def assertOnly(self, findings, rule, step):
        self.assertEqual([(f["rule"], f["step"]) for f in findings], [(rule, step)])
        self.assertTrue(findings[0]["detail"], "a finding carries a sentence")

    def test_a_clean_run_has_no_findings(self):
        write_records(self.d, passes("start page opened", "one printer listed") +
                      [{"step": 3, "verdict": "inconclusive", "note": "the page cannot tell"}],
                      readme="inconclusive")
        self.assertEqual(critic.check(self.d), [])

    def test_a_missing_verdict(self):
        write_records(self.d, passes("opened") + [{"step": 3, "verdict": "pass", "note": "odd"}],
                      readme="pass")
        self.assertOnly(critic.check(self.d), "steps", 2)

    def test_a_duplicate_verdict(self):
        write_records(self.d, [{"step": 1, "verdict": "pass", "note": "a"},
                               {"step": 1, "verdict": "pass", "note": "b"},
                               {"step": 2, "verdict": "pass", "note": "c"}],
                      steps=STEPS[:2], readme="pass")
        self.assertOnly(critic.check(self.d), "steps", 1)

    def test_a_verdict_for_a_step_the_task_does_not_number(self):
        write_records(self.d, passes("a") + [{"step": 4, "verdict": "pass", "note": "d"}],
                      steps=STEPS[:1], readme="pass")
        self.assertOnly(critic.check(self.d), "steps", 4)

    def test_a_missing_screenshot(self):
        write_records(self.d, passes("a", "b", "c"), shots={1, 2}, readme="pass")
        self.assertOnly(critic.check(self.d), "screenshot", 3)

    def test_a_flat_screenshot(self):
        write_records(self.d, passes("a", "b"), steps=STEPS[:2], flat={1}, readme="pass")
        self.assertOnly(critic.check(self.d), "screenshot", 1)

    def test_a_screenshot_that_is_not_a_png(self):
        write_records(self.d, passes("a", "b"), steps=STEPS[:2], bad={2}, readme="pass")
        self.assertOnly(critic.check(self.d), "screenshot", 2)

    def test_a_pass_whose_note_says_it_failed(self):
        write_records(self.d, [{"step": 1, "verdict": "pass", "note": "the add link is broken"},
                               {"step": 2, "verdict": "inconclusive", "note": ""}],
                      steps=STEPS[:2], readme="inconclusive")
        self.assertOnly(critic.check(self.d), "note", 1)

    def test_a_fail_whose_note_says_it_passed(self):
        write_records(self.d, [{"step": 1, "verdict": "fail", "note": "it works"},
                               {"step": 2, "verdict": "pass", "note": "fine"}],
                      steps=STEPS[:2], readme="fail")
        self.assertOnly(critic.check(self.d), "note", 1)

    def test_a_claim_of_pass_when_a_step_failed(self):
        write_records(self.d, [{"step": 1, "verdict": "fail", "note": "no basket"},
                               {"step": 2, "verdict": "pass", "note": "one printer"}],
                      steps=STEPS[:2], readme="pass")
        self.assertOnly(critic.check(self.d), "outcome", None)

    def test_a_claim_of_fail_when_every_step_passed(self):
        write_records(self.d, passes("a", "b"), steps=STEPS[:2], readme="fail")
        self.assertOnly(critic.check(self.d), "outcome", None)

    def test_no_readme_makes_no_claim(self):
        write_records(self.d, passes("a", "b"), steps=STEPS[:2], readme=None)
        self.assertEqual(critic.check(self.d), [])

    def test_a_task_with_no_steps(self):
        write_records(self.d, [], steps=[], readme="inconclusive")
        self.assertOnly(critic.check(self.d), "steps", None)

    def test_a_missing_task_json(self):
        os.makedirs(self.d, exist_ok=True)
        self.assertOnly(critic.check(self.d), "steps", None)

    def test_findings_read_as_dicts_and_attributes(self):
        write_records(self.d, passes("a") + [{"step": 2, "verdict": "pass", "note": "b"},
                                             {"step": 3, "verdict": "pass", "note": "broken"}],
                      readme="pass")
        f = critic.check(self.d)[0]
        self.assertEqual(f["rule"], "note")
        self.assertEqual(f.rule, "note")
        self.assertIn("step 3: ", critic.line(f))
        self.assertIn("note: ", critic.line(f))


class Png(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def path(self, data):
        p = os.path.join(self.d, "x.png")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_one_colour_is_flat(self):
        self.assertTrue(critic.png_is_flat(self.path(pngfix.png(5, 4, (10, 20, 30)))))

    def test_a_gradient_is_not_flat(self):
        self.assertFalse(critic.png_is_flat(self.path(pngfix.png(5, 4, lambda x, y: (x * 7, y * 9, 3)))))

    def test_a_sub_filtered_png_is_still_read(self):
        # filter 1 (Sub) on a flat frame: the raw bytes differ, the pixels do not
        import struct
        import zlib
        width, height, colour = 4, 2, (200, 100, 50)
        raw = bytearray()
        for _ in range(height):
            raw.append(1)
            prev = [0, 0, 0]
            for _x in range(width):
                raw += bytes((c - p) & 0xFF for c, p in zip(colour, prev))
                prev = list(colour)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

        def chunk(typ, data):
            body = typ + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
        self.assertTrue(critic.png_is_flat(self.path(data)))

    def test_a_file_that_is_not_a_png_is_a_value_error(self):
        with self.assertRaises(ValueError):
            critic.png_is_flat(self.path(b"not a png"))


if __name__ == "__main__":
    unittest.main()

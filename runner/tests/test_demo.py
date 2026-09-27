"""python3 -m dark demo: a user-arm run's records cut into a captioned video.

The timeline is read from the Playwright trace: step k ends at the k-th
screenshot, and the step's actions from stream.jsonl are laid in order on the
trace's action events between that screenshot and the one before, with the
click point from the trace's input event. Without a usable trace the actions
are spread evenly. The subtitle script puts the step text above the page, the
actions and verdicts below it, a ring at each click, and every verdict on the
end card. The render test needs ffmpeg with libass and libx264, and is skipped
without them."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

from dark import __main__ as M
from dark import demo

TASK = {"task": "t-demo", "spec": "Find the {printers}.", "url": "http://x:631/", "llm_model": "m1",
        "steps": ["Open the start page.", "Open the printers.", "Say what is odd."]}


def _before(call, method, t):
    return {"type": "before", "callId": call, "method": method, "startTime": t}


def trace_events():
    return [
        {"type": "context-options", "wallTime": 1000, "monotonicTime": 500},
        _before("c1", "newPage", 1000), {"type": "after", "callId": "c1", "endTime": 1100},
        _before("c2", "goto", 1200),  # the arm opening the URL, never a model action
        _before("c3", "ariaSnapshot", 1300),
        _before("c4", "screenshot", 3000),  # step 1's verdict
        _before("c5", "click", 5000), {"type": "input", "callId": "c5", "point": {"x": 100, "y": 20}},
        _before("c6", "selectOption", 6000), {"type": "after", "callId": "c6", "error": {"message": "timeout"}},
        _before("c7", "selectOption", 8000),
        _before("c8", "screenshot", 9000),  # step 2's verdict
        _before("c9", "screenshot", 12000),  # step 3, recorded inconclusive by the arm itself
    ]


STREAM = [
    {"step": 1, "action": {"do": "verdict", "verdict": "pass", "note": "start"}},
    {"step": 2, "action": {"do": "click", "role": "link", "name": "Printers"}},
    {"step": 2, "action": {"do": "select", "role": "combobox", "name": "Maintenance", "value": "Test"},
     "result": {"error": "TimeoutError"}},
    {"step": 2, "action": {"do": "select", "role": "combobox", "name": "", "value": "Test"}},
    {"step": 2, "action": {"do": "verdict", "verdict": "pass", "note": "one printer"}},
]
VERDICTS = [{"step": 1, "verdict": "pass", "note": "start"},
            {"step": 2, "verdict": "pass", "note": "one printer"},
            {"step": 3, "verdict": "inconclusive", "note": "stuck: no progress"}]


def write_records(d, trace=True):
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump(TASK, f)
    for name, rows in (("stream.jsonl", STREAM), ("steps.jsonl", VERDICTS)):
        with open(os.path.join(d, name), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
    if trace:
        with zipfile.ZipFile(os.path.join(d, "trace.zip"), "w") as z:
            z.writestr("trace.trace", "".join(json.dumps(e) + "\n" for e in trace_events()))


class Timeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def test_times_come_from_the_trace(self):
        write_records(self.d)
        steps, exact = demo.timeline(demo.load(self.d), 12.0)
        self.assertTrue(exact)
        self.assertEqual([s["n"] for s in steps], [1, 2, 3])
        # each step ends 0.2 s before its verdict's screenshot, the next starts at the screenshot
        self.assertEqual([(s["start"], s["end"]) for s in steps], [(0.2, 1.8), (2.0, 7.8), (8.0, 10.8)])
        acts = steps[1]["actions"]
        self.assertEqual([a["t"] for a in acts], [4.0, 5.0, 7.0])
        self.assertEqual(acts[0]["text"], "click link 'Printers'")
        self.assertEqual(acts[0]["point"], {"x": 100, "y": 20})
        self.assertEqual([a["failed"] for a in acts], [False, True, False])
        self.assertEqual(steps[2]["actions"], [])
        self.assertEqual(steps[2]["verdict"], "inconclusive")

    def test_without_a_trace_the_actions_are_spread_evenly(self):
        write_records(self.d, trace=False)
        steps, exact = demo.timeline(demo.load(self.d), 30.0)
        self.assertFalse(exact)
        self.assertEqual([(s["start"], s["end"]) for s in steps], [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)])
        self.assertEqual(len(steps[1]["actions"]), 3)
        self.assertTrue(all(10.0 < a["t"] < 20.0 for a in steps[1]["actions"]))

    def test_no_verdicts_is_an_error(self):
        with open(os.path.join(self.d, "steps.jsonl"), "w") as f:
            f.write("not json\n")
        with self.assertRaises(demo.DemoError):
            demo.load(self.d)

    def test_outcome(self):
        self.assertEqual(demo.outcome([{"verdict": "pass"}, {"verdict": "pass"}]), "pass")
        self.assertEqual(demo.outcome([{"verdict": "pass"}, {"verdict": "inconclusive"}]), "inconclusive")
        self.assertEqual(demo.outcome([{"verdict": "fail"}, {"verdict": "inconclusive"}]), "fail")


class Subtitles(unittest.TestCase):
    def test_the_script(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        write_records(d)
        rec = demo.load(d)
        steps, _ = demo.timeline(rec, 12.0)
        holds = demo.plan_holds(steps, {1: 6.0})
        self.assertEqual(holds, [demo.HOLD_S, 6.6, demo.HOLD_S])
        ass, total = demo.subtitles(rec, steps, holds, 12.0)
        self.assertAlmostEqual(total, demo.TITLE_S + 12.0 + sum(holds) + demo.TAIL_S + demo.END_S)
        self.assertIn(f"PlayResY: {demo.CH}", ass)
        self.assertIn("Find the (printers).", ass)  # braces would start an override block
        self.assertIn("Step 2 of 3:  Open the printers.", ass)
        self.assertIn("did not work, trying again", ass)
        self.assertIn(f"\\pos(100,{20 + demo.TOP})", ass)  # the ring sits on the page, below the top band
        self.assertIn("INCONCLUSIVE", ass)
        self.assertIn("2 of 3 steps pass", ass)
        # step 2's click at 4.0 s on the run's clock comes after the title card and step 1's hold
        self.assertIn(f"Dialogue: 0,{demo._ts(demo.TITLE_S + 4.0 + demo.HOLD_S)},", ass)


class Speech(unittest.TestCase):
    def test_the_title_speaks_one_sentence_and_long_banners_are_clipped(self):
        rec = {"task": {"task": "t", "spec": "First sentence here. Second one is never read."}}
        title, verdicts, end = demo.speech(rec, [{"n": 1, "verdict": "pass", "note": "ok"}])
        self.assertEqual(title, "t. First sentence here.")
        self.assertEqual(verdicts, ["Step 1, pass. ok"])
        self.assertEqual(end, "Result: pass. 1 of 1 steps pass.")
        self.assertEqual(demo._clip("word " * 100, 20), "word word word …")  # never past the limit


def _can_render():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    q = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return " ass " in p.stdout and "libx264" in q.stdout


@unittest.skipUnless(_can_render(), "needs ffmpeg with libass and libx264")
class Render(unittest.TestCase):
    def test_a_video_is_cut(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        write_records(d)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=25:d=12",
                        "-c:v", "libvpx", "-b:v", "200k", "-deadline", "realtime", "-cpu-used", "8", os.path.join(d, "video.webm")], check=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["demo", d])
        self.assertEqual(rc, 0, buf.getvalue())
        out = json.loads(buf.getvalue())
        self.assertTrue(out["timed_from_trace"])
        path = os.path.join(d, "demo.mp4")
        self.assertEqual(out["demo"], path)
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                            "-of", "json", path], capture_output=True, text=True, check=True)
        info = json.loads(p.stdout)
        self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]), (1280, demo.CH))
        self.assertAlmostEqual(float(info["format"]["duration"]), out["seconds"], delta=0.5)

    def test_no_video_is_refused(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        write_records(d)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = M.main(["demo", d])
        self.assertEqual(rc, 2)
        self.assertIn("no video.webm", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

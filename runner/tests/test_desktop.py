"""dark/desktop.py, the desktop driver, against a fake exec channel that
records every command and replays one output per program: one test per action,
a refused click, a desktop snapshot of windows and OCR lines, and the driver
driven through the user arm's `run_steps` (a quoted window title accepted, an
invented one refused, three steps with three verdicts and three screenshots).
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from dark import desktop as D
from dark import run as R
from dark import session as S
from dark import spec
from dark import user as U
from tests import fakes
from tests.test_user import reset_session, verdict

PNG = b"\x89PNG\r\n\x1a\n"

# mango's `mmsg get all-clients` reply, and niri's `niri msg --json windows`
MANGO_WINDOWS = {"clients": [
    {"id": 4, "appid": "foot", "title": "shell", "tags": ["1"], "is_focused": False},
    {"id": 7, "appid": "firefox", "title": "Dark search", "tags": ["2"], "is_focused": True},
]}
NIRI_WINDOWS = [
    {"id": 7, "app_id": "firefox", "title": "Dark search", "workspace_id": 2, "is_focused": True},
    {"id": 4, "app_id": "foot", "title": "shell", "workspace_id": 1, "is_focused": False},
]
OCR = "hello world\nsecond line\n\n"


class FakeChannel:
    """A channel that records every command list and replays one canned
    `(rc, output)` per program (the command's argv[0]). With `frames`, a `grim`
    command writes a stand-in PNG to the file it names, as the real one does."""

    def __init__(self, outputs=None, frames=False):
        self.outputs = dict(outputs or {})
        self.frames = frames
        self.calls = []

    def __call__(self, argv):
        self.calls.append([str(a) for a in argv])
        if self.frames and argv[0] == "grim" and len(argv) > 1 and argv[1] != "-":
            with open(argv[1], "wb") as f:
                f.write(PNG)
        return self.outputs.get(self.program(argv), (0, ""))

    @staticmethod
    def program(argv):
        """The program a command runs: argv[0], or the one an `sh -c` wraps
        (the driver wraps tesseract to drop its stderr)."""
        if argv[0] == "sh" and len(argv) > 2:
            return str(argv[2]).split()[0]
        return argv[0]

    def programs(self):
        return [self.program(c) for c in self.calls]


def mango_channel(**kw):
    return FakeChannel({"mmsg": (0, json.dumps(MANGO_WINDOWS)), "tesseract": (0, OCR)}, **kw)


class Actions(unittest.TestCase):
    """One test per action of the desktop action set."""

    def setUp(self):
        # a clock that jumps past the launch wait, so no test sleeps for real
        ticks = iter(range(0, 1000, 5))
        self.page = D.DesktopPage(mango_channel(), "mango", clock=lambda: next(ticks), sleep=lambda s: None)

    def test_launch_runs_the_desktop_entry(self):
        self.page.act({"do": "launch", "app": "foot"})
        # detached through sh, because gtk-launch waits for the program
        self.assertIn(["sh", "-c", 'gtk-launch "$1" >/dev/null 2>&1 &', "launch", "foot"], self.page.channel.calls)

    def test_key_runs_wtype_for_a_chord(self):
        self.page.act({"do": "key", "key": "ctrl+c"})
        self.assertIn(["wtype", "-M", "ctrl", "-k", "c"], self.page.channel.calls)

    def test_type_runs_wtype_with_the_text(self):
        self.page.act({"do": "type", "text": "hello world"})
        self.assertIn(["wtype", "--", "hello world"], self.page.channel.calls)

    def test_click_moves_the_pointer_and_clicks(self):
        self.page.which = lambda name: "/usr/bin/ydotool"
        self.page.act({"do": "click", "x": 10, "y": 20})
        self.assertIn(["ydotool", "mousemove", "--absolute", "10", "20"], self.page.channel.calls)
        self.assertIn(["ydotool", "click", "0xC0"], self.page.channel.calls)

    def test_focus_dispatches_the_window_id(self):
        self.page.act({"do": "focus", "name": "Dark search"})
        self.assertIn(["mmsg", "dispatch", "focusid", "client,7"], self.page.channel.calls)

    def test_wait_only_sleeps(self):
        with mock.patch.object(D.time, "sleep") as sleep:
            self.page.act({"do": "wait", "seconds": 2})
        sleep.assert_called_once_with(2)
        self.assertEqual(self.page.channel.calls, [])  # a wait runs no command

    def test_wtype_key_names_modifiers_and_common_keys(self):
        self.assertEqual(D.wtype_key("Return"), ["wtype", "-k", "Return"])
        self.assertEqual(D.wtype_key("ctrl+shift+t"), ["wtype", "-M", "ctrl", "-M", "shift", "-k", "t"])


class Refusals(unittest.TestCase):
    """An action the desktop cannot do is refused with an error the model
    sees, never done silently."""

    def test_a_click_without_ydotool_is_refused(self):
        page = D.DesktopPage(mango_channel(), "mango", which=lambda name: None)
        with self.assertRaises(D.DesktopError) as cm:
            page.act({"do": "click", "x": 1, "y": 2})
        self.assertIn("ydotool", str(cm.exception))
        self.assertNotIn("ydotool", page.channel.programs())

    def test_an_unknown_compositor_is_refused(self):
        with self.assertRaises(D.DesktopError):
            D.DesktopPage(mango_channel(), "sway")

    def test_a_failed_command_carries_its_output_to_the_model(self):
        channel = FakeChannel({"wtype": (1, "compositor refused the keyboard")})
        with self.assertRaises(D.DesktopError) as cm:
            D.DesktopPage(channel, "mango").act({"do": "type", "text": "hi"})
        self.assertIn("compositor refused the keyboard", str(cm.exception))


class Snapshot(unittest.TestCase):
    """The desktop snapshot: one line per window, then one `ocr:` line per line
    tesseract reads off a grim frame."""

    def test_windows_then_ocr_lines(self):
        page = D.DesktopPage(mango_channel(frames=True), "mango")
        snap = page.snapshot()
        self.assertEqual(snap.splitlines(), [
            'window id=4 appid="foot" title="shell" workspace=1 unfocused',
            'window id=7 appid="firefox" title="Dark search" workspace=2 focused',
            "ocr: hello world",
            "ocr: second line",
        ])
        self.assertIn("grim", page.channel.programs())
        self.assertIn("tesseract", page.channel.programs())

    def test_niri_windows_are_read_from_their_own_fields(self):
        page = D.DesktopPage(FakeChannel({"niri": (0, json.dumps(NIRI_WINDOWS))}), "niri")
        snap = page.snapshot()
        self.assertIn('window id=7 appid="firefox" title="Dark search" workspace=2 focused', snap)

    def test_url_names_the_focused_window(self):
        self.assertEqual(D.DesktopPage(mango_channel(), "mango").url(), 'firefox "Dark search"')

    def test_a_window_list_that_cannot_be_read_is_a_desktop_error(self):
        channel = FakeChannel({"mmsg": (0, "not json")})
        with self.assertRaises(D.DesktopError):
            D.DesktopPage(channel, "mango").snapshot()


def mango_page(**kw):
    kw.setdefault("sleep", lambda s: None)
    return D.DesktopPage(mango_channel(frames=True), "mango", **kw)


class StepLoop(unittest.TestCase):
    """The driver through the user arm's run_steps: the quote rule on a window
    title, and three steps taking three verdicts and three screenshots."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        reset_session(self.tmp, {"token": "fake_agent"})
        self.records = S.RECORDS_DIR

    def run_steps(self, model, page, steps):
        return U.run_steps(model, page, "foot", steps, self.records, 20, 1e12)

    def test_a_pass_quoting_a_window_title_is_accepted(self):
        model = ScriptedModel([verdict(evidence="Dark search")])
        results, stop = self.run_steps(model, mango_page(), ["Read the window"])
        self.assertIsNone(stop)
        self.assertEqual(results[0]["verdict"], "pass")
        self.assertEqual(results[0]["evidence"], "Dark search")

    def test_a_pass_quoting_an_invented_title_is_refused(self):
        model = ScriptedModel([verdict(evidence="A Window That Is Not There", note="invented"),
                               verdict("fail", "it is not there")])
        results, stop = self.run_steps(model, mango_page(), ["Read the window"])
        self.assertIsNone(stop)
        self.assertEqual(results[0]["verdict"], "fail")  # the invented pass never counted
        self.assertIn("evidence not found", model.asked[1]["history"][0]["error"])

    def test_three_steps_three_verdicts_and_three_screenshots(self):
        model = ScriptedModel([verdict(evidence="Dark search", note="shown"),
                               verdict("fail", "nothing happened"),
                               verdict("inconclusive", "cannot tell")])
        results, stop = self.run_steps(model, mango_page(), ["one", "two", "three"])
        self.assertIsNone(stop)
        self.assertEqual([r["verdict"] for r in results], ["pass", "fail", "inconclusive"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.records, "steps"))),
                         ["01.png", "01.trail.jsonl", "02.png", "02.trail.jsonl",
                          "03.png", "03.trail.jsonl"])

    def test_the_trail_names_a_desktop_target_and_the_page_prompt_is_the_desktop_one(self):
        model = ScriptedModel([{"do": "focus", "name": "Dark search"}, verdict(evidence="Dark search")])
        self.run_steps(model, mango_page(), ["Look at the window"])
        with open(os.path.join(self.records, "steps", "01.trail.jsonl")) as f:
            line = json.loads(f.read())
        self.assertEqual(line["target"], 'focus "Dark search"')
        self.assertEqual([c["system"] for c in model.asked], [D.SYSTEM, D.SYSTEM])


class ScriptedModel:
    """next_action from a list, in order; each question is kept, including the
    system prompt run_steps took from the desktop driver's SYSTEM."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.asked = []

    def next_action(self, n, text, url, snapshot, history, earlier=None, timeout=None, system=None):
        self.asked.append({"step": n, "text": text, "snapshot": snapshot,
                           "history": list(history), "system": system})
        return self.actions.pop(0)


class EnvFile(unittest.TestCase):
    """The session's environment file: `KEY=VALUE`, quotes and comments."""

    def test_values_comments_and_quotes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "dark-desktop-session")
        with open(path, "w") as f:
            f.write('# the session\nWAYLAND_DISPLAY="wayland-0"\nDARK_UID=1000\n\nBADLINE\n')
        env = D.read_env_file(path)
        self.assertEqual(env, {"WAYLAND_DISPLAY": "wayland-0", "DARK_UID": "1000"})

    def test_a_missing_file_is_an_empty_environment(self):
        self.assertEqual(D.read_env_file("/nonexistent/dark-desktop-session"), {})


class EdgeBands(unittest.TestCase):
    """The snapshot also reads each output's top and bottom band, adding the
    lines the whole-frame read missed (a bar above an open window)."""

    def test_a_bar_line_only_the_top_band_shows_is_added_once(self):
        calls = []

        def channel(argv):
            argv = [str(a) for a in argv]
            calls.append(argv)
            if argv[:3] == ["mmsg", "get", "all-clients"]:
                return 0, json.dumps({"clients": []})
            if argv[:3] == ["mmsg", "get", "all-monitors"]:
                return 0, json.dumps({"monitors": [{"x": 0, "y": 0, "width": 1280, "height": 800, "scale": 1}]})
            if argv[0] == "grim":
                self.last_grab = argv
                return 0, ""
            if argv[0] == "sh":  # tesseract
                if self.last_grab[1:3] == ["-g", "0,0 1280x66"]:
                    return 0, "Nothing Playing\nshell prompt\n"
                if "-g" in self.last_grab:
                    return 0, ""
                return 0, "shell prompt\n"
            return 0, ""

        self.last_grab = []
        snap = D.DesktopPage(channel, "mango").snapshot()
        self.assertEqual(snap.splitlines(), ["ocr: shell prompt", "ocr: Nothing Playing"])
        self.assertIn(["grim", "-g", "0,734 1280x66", calls[-2][-1]], calls)


class Checks(unittest.TestCase):
    """run_checks: one checks.jsonl line per check, its own timeout, the output
    cut, and the tally of the passing checks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def lines(self):
        with open(os.path.join(self.tmp, D.CHECKS_FILE)) as f:
            return [json.loads(line) for line in f]

    def test_a_passing_and_a_failing_check_give_the_right_lines_and_tally(self):
        seen = []

        def run(argv, **kw):
            seen.append((list(argv), dict(kw)))
            if argv[-1] == "true":
                return subprocess.CompletedProcess(argv, 0, "ok\n", "")
            return subprocess.CompletedProcess(argv, 3, "x" * 20, "boom\n")

        ok, total = D.run_checks([{"id": "a", "command": "true", "timeout": 5},
                                  {"id": "b", "command": "false"}], self.tmp, run=run)
        self.assertEqual((ok, total), (1, 2))
        self.assertEqual(self.lines()[0], {"id": "a", "command": "true", "rc": 0,
                                           "output": "ok\n", "verdict": "pass"})
        self.assertEqual((self.lines()[1]["rc"], self.lines()[1]["verdict"]), (3, "fail"))
        self.assertEqual(self.lines()[1]["output"], "x" * 20 + "boom\n")  # stdout then stderr
        self.assertEqual(seen[0], (["sh", "-c", "true"],
                                   {"capture_output": True, "text": True, "timeout": 5}))
        self.assertEqual(seen[1][1]["timeout"], D.CHECK_TIMEOUT)  # the task named none

    def test_a_check_not_for_this_target_is_skipped_and_not_counted(self):
        ran = []

        def run(argv, **kw):
            ran.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        checks = [{"id": "reads", "command": "true", "targets": ("lab", "vm", "live")},
                  {"id": "turns-the-screen-off", "command": "tv-off", "targets": ("lab", "vm")}]
        self.assertEqual(D.run_checks(checks, self.tmp, run=run, target="live"), (1, 1))
        self.assertEqual(ran, ["true"])  # the screen-off check never ran on the live machine
        self.assertEqual([(x["id"], x["verdict"]) for x in self.lines()],
                         [("reads", "pass"), ("turns-the-screen-off", "skipped")])
        self.assertEqual(D.run_checks(checks, self.tmp, run=run, target="lab"), (2, 2))

    def test_a_check_that_times_out_is_rc_124_and_fails(self):
        def run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        ok, total = D.run_checks([{"id": "a", "command": "sleep 999", "timeout": 7}],
                                 self.tmp, run=run)
        self.assertEqual((ok, total), (0, 1))
        self.assertEqual((self.lines()[0]["rc"], self.lines()[0]["verdict"]), (124, "fail"))

    def test_the_output_is_cut_to_two_thousand_characters(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "y" * 5000, "")

        D.run_checks([{"id": "big", "command": "c"}], self.tmp, run=run)
        self.assertEqual(len(self.lines()[0]["output"]), D.CHECK_OUTPUT_CHARS)

    def test_records_paths_lists_the_checks_file(self):
        self.assertEqual(D.records_paths("/r")[D.CHECKS_FILE], "/r/checks.jsonl")


class Ending(unittest.TestCase):
    """A run whose steps all pass but one check fails ends `checks`: the hidden
    checks are a verdict of their own, also when every step was green."""

    def test_all_steps_pass_and_one_check_fails_ends_checks(self):
        results = [{"step": 1, "verdict": "pass", "note": "shown"},
                   {"step": 2, "verdict": "pass", "note": "shown"}]
        self.assertEqual(D.verdict(results, None, 1, 2),
                         ("fail:capability", "checks",
                          "1 of 2 checks failed (2/2 steps pass, 1/2 checks pass)"))
        self.assertEqual(spec.FAIL_KIND_OUTCOME["checks"], "fail:capability")

    def test_a_failed_step_comes_before_a_failing_check(self):
        results = [{"step": 1, "verdict": "fail", "note": "nothing happened"}]
        self.assertEqual(D.verdict(results, None, 0, 1)[:2], ("fail:capability", "steps"))

    def test_every_step_and_check_pass_is_a_pass(self):
        results = [{"step": 1, "verdict": "pass", "note": "shown"}]
        self.assertEqual(D.verdict(results, None, 2, 2),
                         ("pass", None, "1/1 steps pass, 2/2 checks pass"))


class FakeRun:
    """subprocess.run for the start script and the checks: records every command
    and returns rc 1 for a check whose command holds one of `fail`, or for the
    start script when `fail_start`."""

    def __init__(self, fail=(), fail_start=False):
        self.fail = set(fail)
        self.fail_start = fail_start
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(([str(a) for a in argv], dict(kw)))
        if len(argv) == 2:  # sh <start script>
            rc = 1 if self.fail_start else 0
        else:  # sh -c <check>
            rc = 1 if any(f in " ".join(str(a) for a in argv) for f in self.fail) else 0
        return subprocess.CompletedProcess(argv, rc, "ok\n" if rc == 0 else "boom\n", "")


class DesktopExecutor(unittest.TestCase):
    """desktop.main(): the start script first, as root, then the step loop over a
    fake desktop, then the hidden checks; the AGENT-DONE tag carries both tallies,
    end to end against the fake Gitea and a file:// records repository."""

    @classmethod
    def setUpClass(cls):
        cls.gitea = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.gitea.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repos = os.path.join(self.tmp, "repos")
        git_url = fakes.make_origin(self.repos, "dark-records/s1", {"README.md": "records\n"})
        self.gitea.issues["dark-records/s1"] = {1: {"title": "desktop", "body": "", "state": "open",
                                                 "comments": []}}
        reset_session(self.tmp, {
            "gitea": self.gitea.url, "git_url": git_url, "repo": "dark-records/s1",
            "records_repo": "dark-records/s1", "issue": 1, "token": "agent-token", "mode": "desktop",
            "image": "example.test/tvbox:next", "start": "#!/bin/sh\necho up\n",
            "steps": ["Open a terminal", "Read the top bar"],
            "checks": [{"id": "c0", "command": "true", "timeout": 5}],
            "spec": "Check the desktop.", "llm_model": "local-a", "max_calls": 20,
            "max_seconds": 600, "run": "tvbox-desktop-1", "heartbeat_seconds": 3600})
        S.API = f"{self.gitea.url}/api/v1"
        S.REPO = "dark-records/s1"
        self.start = os.path.join(self.tmp, "start.sh")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def done_tag(self):
        body = [b for b in self.gitea.bodies("dark-records/s1", 1) if b.startswith("AGENT-DONE")][-1]
        tag = [t for t in R.parse_tags(body) if t.get("ev") == "done"][-1]
        self.assertTrue(spec.tag_ok("done", tag), tag)
        return body, tag

    def two_passes(self):
        return ScriptedModel([verdict("pass", "shown", evidence="Dark search"),
                               verdict("pass", "shown", evidence="Dark search")])

    def test_desktop_main_runs_the_start_script_then_the_steps_and_checks(self):
        run = FakeRun()
        page = mango_page()
        rc = D.main(self.two_passes(), page, run=run, start_path=self.start)
        self.assertEqual(rc, 0)
        body, tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["steps_ok"], tag["steps_total"],
                          tag["checks_ok"], tag["checks_total"]), ("ok", 2, 2, 1, 1))
        # the start script ran first, as root, before any step
        self.assertEqual(run.calls[0][0], ["sh", self.start])
        self.assertEqual(page.channel.calls[0][0], "mmsg")  # the first snapshot is now
        self.assertEqual([c[0][0] for c in run.calls], ["sh", "sh"])  # start, then the check
        with open(os.path.join(S.RECORDS_DIR, "checks.jsonl")) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(rows, [{"id": "c0", "command": "true", "rc": 0,
                                  "output": "ok\n", "verdict": "pass"}])

    def test_desktop_main_runs_the_start_script_for_a_vm_target(self):
        # in a VM the image starts the session itself and the task's start
        # script only waits for it; dark runs the start script either way
        S.TASK["target"] = "vm"
        run = FakeRun()
        rc = D.main(self.two_passes(), mango_page(), run=run, start_path=self.start)
        self.assertEqual(rc, 0)
        self.assertEqual(run.calls[0][0], ["sh", self.start])

    def test_desktop_main_runs_the_start_script_for_a_live_target(self):
        # on a live machine the start script comes from the copied task.json
        # and runs as root the same way it does in a sandbox or a VM, so it
        # writes the host's own /run/dark-desktop-session
        S.TASK["target"] = "live"
        run = FakeRun()
        rc = D.main(self.two_passes(), mango_page(), run=run, start_path=self.start)
        self.assertEqual(rc, 0)
        self.assertEqual(run.calls[0][0], ["sh", self.start])

    def test_desktop_main_a_start_script_that_fails_ends_env(self):
        rc = D.main(self.two_passes(), mango_page(), run=FakeRun(fail_start=True), start_path=self.start)
        self.assertEqual(rc, 1)
        body, tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"]), ("fail", "env"))
        self.assertIn("start script failed", body)

    def test_desktop_main_a_failing_check_ends_checks(self):
        S.TASK["checks"] = [{"id": "c0", "command": "true"}, {"id": "c1", "command": "false"}]
        rc = D.main(self.two_passes(), mango_page(), run=FakeRun(fail=("false",)), start_path=self.start)
        self.assertEqual(rc, 1)
        body, tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"]), ("fail", "checks"))
        self.assertEqual((tag["steps_ok"], tag["checks_ok"], tag["checks_total"]), (2, 1, 2))
        self.assertIn("1 of 2 checks failed", body)


if __name__ == "__main__":
    unittest.main()

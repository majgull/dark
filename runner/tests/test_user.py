"""dark/user.py, the user arm's executor, against a scripted model and a fake
page: three steps taken in order, one screenshot and one verdict line per
step, pass only when every verdict is pass, and the records pushed with the
session arm's own push, with no token in them. Then the run driver
(Runner.user) turning the executor's report into an outcome."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from dark import budget, config, spec, tasks
from dark import gitea as G
from dark import ledger as L
from dark import run as R
from dark import session as S
from dark import user as U
from tests import fakes
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_tasks import make_user_task

TOKEN = "agent-secret-token"
STEPS = ["Open the sign-in page", "Sign in as the demo user", "Add one item to the cart"]
PNG = b"\x89PNG\r\n\x1a\n"


class ScriptedModel:
    """next_action from a list, in order; every question is kept."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.asked = []

    def next_action(self, n, text, url, snapshot, history, earlier=None, timeout=None):
        self.asked.append({"step": n, "text": text, "url": url, "snapshot": snapshot,
                           "history": list(history), "earlier": list(earlier or [])})
        return self.actions.pop(0)


class FakePage:
    """The driver interface: records what was done, writes a stand-in PNG."""

    def __init__(self, snapshot="- heading \"Shop\"", fail_on=()):
        self.current = ""
        self.done = []
        self.shots = []
        self.closed = False
        self._snapshot = snapshot
        self.fail_on = set(fail_on)

    def goto(self, url):
        self.current = url
        self.done.append(("goto", url))

    def url(self):
        return self.current

    def snapshot(self):
        return self._snapshot

    def act(self, action):
        if action.get("name") in self.fail_on:
            raise TimeoutError(f"no {action.get('role')} named {action.get('name')!r}")
        self.done.append(("act", action))

    def screenshot(self, path):
        self.shots.append(os.path.basename(path))
        with open(path, "wb") as f:
            f.write(PNG + os.path.basename(path).encode())

    def close(self):
        self.closed = True


class ChangingPage(FakePage):
    """A page whose snapshot changes after an action, as a real one does:
    the first is shown, then the rest in order, the last kept."""

    def __init__(self, snapshots):
        super().__init__(snapshot=snapshots[0])
        self.rest = list(snapshots[1:])

    def act(self, action):
        super().act(action)
        if self.rest:
            self._snapshot = self.rest.pop(0)


class ServerPage(FakePage):
    """A page that, like a browser, issues the page's own request when the
    Like button is clicked: a real GET to the local server, recorded for the
    trail the way PlaywrightPage records its network events."""

    def __init__(self, base):
        super().__init__(snapshot='- heading "Shop"\n- button "Like"')
        self.base = base
        self.pending = []

    def act(self, action):
        if action.get("do") == "click" and action.get("name") == "Like":
            url = self.base + "/api/liked"
            try:
                urllib.request.urlopen(url, timeout=5)
            except urllib.error.HTTPError as e:
                self.pending.append({"method": "GET", "url": url, "status": e.code})
        super().act(action)

    def drain_requests(self):
        out, self.pending = self.pending, []
        return out


class _LikeHandler(BaseHTTPRequestHandler):
    """A page whose /api/liked answers 502; every other path answers the page
    with the button."""

    def do_GET(self):
        if self.path == "/api/liked":
            body, code = b"bad gateway", 502
        else:
            body, code = b'<html><body><button>Like</button></body></html>', 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RecordedPage(FakePage):
    """A page that, like PlaywrightPage, leaves its recordings in the records
    directory when it is closed."""

    def __init__(self, records_dir, trace=True, video=True, **kw):
        super().__init__(**kw)
        self.records_dir, self.trace, self.video = records_dir, trace, video

    def close(self):
        for wanted, name, data in ((self.trace, "trace.zip", b"PK trace"), (self.video, "video.webm", b"webm")):
            if wanted:
                with open(os.path.join(self.records_dir, name), "wb") as f:
                    f.write(data)
        super().close()


class FakePlaywright:
    """playwright.sync_api as PlaywrightPage uses it, keeping every call in
    `calls` as (name, kwargs). no_video: a context that records a video
    refuses its page, as Playwright does without its ffmpeg. no_trace:
    tracing.start refuses. dies: tracing.stop, closing the context, the
    browser and Playwright all raise, as they do once the browser crashed."""

    def __init__(self, no_video=False, no_trace=False, dies=False):
        self.no_video, self.no_trace, self.dies = no_video, no_trace, dies
        self.calls = []
        self.chromium = self  # sync_playwright().start().chromium.launch(...) -> a browser, also this object
        self.contexts = []

    def install(self, case):
        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: types.SimpleNamespace(start=lambda: self)
        patch = mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"),
                                              "playwright.sync_api": module})
        patch.start()
        case.addCleanup(patch.stop)
        return self

    def _call(self, name, **kw):
        self.calls.append((name, kw))
        if self.dies and name in ("tracing.stop", "context.close", "browser.close", "stop"):
            raise RuntimeError("Target closed")

    def launch(self, **kw):
        self._call("launch", **kw)
        return self

    def new_context(self, **kw):
        self._call("new_context", **kw)
        ctx = FakeContext(self, kw)
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self._call("browser.close")

    def stop(self):
        self._call("stop")

    def names(self):
        return [name for name, _ in self.calls]

    def kwargs(self, name):
        return [kw for n, kw in self.calls if n == name]


class FakeEventPage:
    """Playwright's Page as PlaywrightPage uses it: the video handle plus the
    request/response streams the trail listens to. `emit` feeds one event to
    the handlers registered for it."""

    def __init__(self, pw, kw):
        self.pw, self.kw = pw, kw
        self.video = None
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, obj):
        for handler in self.handlers.get(event, []):
            handler(obj)

    def goto(self, url, **kw):
        self.url = url


class FakeContext:
    def __init__(self, pw, kw):
        self.pw, self.kw = pw, kw
        self.tracing = self

    def start(self, **kw):  # tracing.start
        self.pw._call("tracing.start", **kw)
        if self.pw.no_trace:
            raise RuntimeError("tracing is not available")

    def stop(self, **kw):  # tracing.stop
        self.pw._call("tracing.stop", **kw)
        with open(kw["path"], "wb") as f:
            f.write(b"PK trace")

    def new_page(self):
        self.pw._call("new_page")
        page = FakeEventPage(self.pw, self.kw)
        self.page = page
        if self.kw.get("record_video_dir"):
            if self.pw.no_video:
                raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/ffmpeg-1011/ffmpeg-linux")
            path = os.path.join(self.kw["record_video_dir"], "0a1b2c.webm")
            with open(path, "wb") as f:
                f.write(b"webm")
            page.video = types.SimpleNamespace(path=lambda: path)
        return page

    def close(self):
        self.pw._call("context.close")


def verdict(v="pass", note="as asked", evidence=None):
    """One verdict action. A pass gets an evidence quote because the arm
    accepts one only when the quote is on the page; the fake pages show
    "Shop", so that is the default."""
    a = {"do": "verdict", "verdict": v, "note": note}
    if v == "pass":
        a["evidence"] = "Shop" if evidence is None else evidence
    elif evidence is not None:
        a["evidence"] = evidence
    return a


def reset_session(tmp, task):
    """dark/session.py's module state, pointed at this test's directories."""
    for k in S.STATS:
        S.STATS[k] = 0
    S.TASK.clear()
    S.TASK.update(task)
    S.PROGRESS = S.Progress()
    S.RECORDS_DIR = os.path.join(tmp, "records")
    S.STREAM_PATH = os.path.join(S.RECORDS_DIR, "stream.jsonl")
    S.RECORDS_WORK = os.path.join(tmp, "records-work")
    S.RUN_ID = task.get("run", "run")
    S.SPEC_TEXT = task.get("spec", "")
    S.T0 = time.time()


class Steps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_session(self.tmp, {"token": TOKEN})
        self.records = S.RECORDS_DIR

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_steps(self, model, page, max_calls=20, deadline=1e12):
        return U.run_steps(model, page, "https://app.example.test/", STEPS, self.records, max_calls, deadline)

    def jsonl(self):
        with open(os.path.join(self.records, "steps.jsonl")) as f:
            return [json.loads(l) for l in f]

    def test_three_steps_three_screenshots_in_order_and_three_verdicts(self):
        model = ScriptedModel([
            {"do": "click", "role": "link", "name": "Sign in"}, verdict(note="the form is shown"),
            {"do": "fill", "role": "textbox", "name": "User", "text": "demo"},
            {"do": "click", "role": "button", "name": "Sign in"}, verdict(note="signed in as demo"),
            verdict(note="the cart holds one item")])
        page = FakePage()
        results, stop = self.run_steps(model, page)
        self.assertIsNone(stop)
        self.assertEqual(page.shots, ["01.png", "02.png", "03.png"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.records, "steps"))),
                         ["01.png", "01.trail.jsonl", "02.png", "02.trail.jsonl",
                          "03.png", "03.trail.jsonl"])
        want = [{"step": 1, "verdict": "pass", "note": "the form is shown", "evidence": "Shop"},
                {"step": 2, "verdict": "pass", "note": "signed in as demo", "evidence": "Shop"},
                {"step": 3, "verdict": "pass", "note": "the cart holds one item", "evidence": "Shop"}]
        self.assertEqual(results, want)
        self.assertEqual(self.jsonl(), want)
        # the URL was opened first, then the actions, in the order given
        self.assertEqual(page.done[0], ("goto", "https://app.example.test/"))
        self.assertEqual([a["name"] for _, a in page.done[1:]], ["Sign in", "User", "Sign in"])
        # every question named its step and carried the page's snapshot
        self.assertEqual([q["step"] for q in model.asked], [1, 1, 2, 2, 2, 3])
        self.assertEqual(model.asked[2]["text"], STEPS[1])
        self.assertTrue(all(q["snapshot"] == page.snapshot() for q in model.asked))
        self.assertEqual((S.STATS["calls"], S.STATS["tool_calls"]), (6, 3))

    def test_the_prompt_carries_each_finished_steps_note_and_none_for_the_first(self):
        llm = fakes.FakeLLM([
            {"content": '{"do": "click", "role": "link", "name": "Sign in"}'},
            {"content": json.dumps({"do": "verdict", "verdict": "pass", "note": "the form is shown",
                                     "evidence": "Shop"})},
            {"content": '{"do": "click", "role": "button", "name": "Buy"}'},
            {"content": json.dumps({"do": "verdict", "verdict": "pass", "note": "the cart holds one item",
                                     "evidence": "Shop"})}])
        self.addCleanup(llm.close)
        model = U.ChatModel({"llm_url": f"{llm.url}/v1", "llm_model": "local-a",
                             "spec": "Check that a visitor can buy.", "steps": STEPS})
        results, stop = U.run_steps(model, FakePage(), "https://app.example.test/", STEPS[:2],
                                    self.records, 20, 1e12)
        self.assertIsNone(stop)
        self.assertEqual([r["verdict"] for r in results], ["pass", "pass"])
        prompts = [r["messages"][1]["content"] for r in llm.requests]
        self.assertIn("EARLIER STEPS:\nnone", prompts[0])  # step 1: nothing has finished
        self.assertIn("EARLIER STEPS:\nstep 1 (pass): the form is shown", prompts[2])  # step 2's first call

    def test_a_pass_keeps_the_quote_it_was_accepted_on_and_a_fail_carries_none(self):
        model = ScriptedModel([verdict(evidence='heading "Shop"', note="the shop is shown"),
                               verdict("fail", "no cart button"), verdict()])
        results, stop = self.run_steps(model, FakePage(snapshot='- heading "Shop"\n- text "cart: 1"'))
        self.assertIsNone(stop)
        self.assertEqual(results[0], {"step": 1, "verdict": "pass", "note": "the shop is shown",
                                      "evidence": 'heading "Shop"'})
        self.assertEqual(results[1], {"step": 2, "verdict": "fail", "note": "no cart button"})
        self.assertEqual(self.jsonl(), results)

    def test_a_quote_copied_across_a_line_break_still_matches(self):
        page = FakePage(snapshot='- heading\n  "Shop"\n- text "cart: 1"')
        results, _ = self.run_steps(ScriptedModel([verdict(evidence='heading "Shop"'), verdict(), verdict()]),
                                    page)
        self.assertEqual(results[0]["evidence"], 'heading "Shop"')

    def test_a_quote_spanning_two_snapshot_lines_matches(self):
        page = FakePage(snapshot='Yesterday — 19:40\nSunday Morning: 18 songs')
        quote = 'Yesterday — 19:40 Sunday Morning: 18 songs'
        results, _ = self.run_steps(ScriptedModel([verdict(evidence=quote, note="yesterday's history"),
                                                   verdict(evidence="Sunday Morning: 18 songs"),
                                                   verdict(evidence="Sunday Morning: 18 songs")]), page)
        self.assertEqual(results[0]["verdict"], "pass")
        self.assertEqual(results[0]["evidence"], quote)

    def test_a_line_join_ignores_case_and_edge_punctuation(self):
        # the snapshot's bullets and quotes, and the em dash between the day
        # and the clock, are not parts of the words on the page
        page = FakePage(snapshot='- "Yesterday — 19:40"\n- "Sunday Morning: 18 songs"')
        results, _ = self.run_steps(ScriptedModel([verdict(evidence="yesterday 19:40 sunday morning: 18 songs",
                                                           note="the history is there"),
                                                   verdict(evidence="Sunday Morning: 18 songs"),
                                                   verdict(evidence="Sunday Morning: 18 songs")]), page)
        self.assertEqual(results[0]["verdict"], "pass")

    def test_evidence_spanning_lines_but_not_on_the_page_is_still_refused(self):
        page = FakePage(snapshot='- "Yesterday — 19:40"\n- "Sunday Morning: 18 songs"')
        model = ScriptedModel([verdict(evidence="Yesterday — 20:40 Sunday Morning: 18 songs",
                                       note="wrong time"),
                               verdict(evidence="Sunday Morning: 18 songs"),
                               verdict(evidence="Sunday Morning: 18 songs"),
                               verdict(evidence="Sunday Morning: 18 songs")])
        results, _ = self.run_steps(model, page)
        self.assertEqual(model.asked[1]["history"][0]["error"],
                         'evidence not found on the page: "20:40 Sunday Morning: 18 songs"')
        self.assertEqual(results[0]["verdict"], "pass")

    def test_a_quote_joining_two_elements_with_a_dash_is_accepted(self):
        # live: the model joined two list items with " — ", and a summary line
        # sits between them, so no single string of the quote is on the page
        page = FakePage(snapshot='- heading "Shop"\n'
                                 '- listitem "Lofi Jazzy": Lofi Jazzy: 7 songs, 2 saved, 5 not saved\n'
                                 '- text: 6 total\n'
                                 '- listitem "Nujabes - Feather": Nujabes - Feather')
        quote = "Lofi Jazzy: 7 songs, 2 saved, 5 not saved — Nujabes - Feather"
        results, stop = self.run_steps(ScriptedModel([verdict(evidence=quote, note="both albums"),
                                                      verdict(), verdict()]), page)
        self.assertIsNone(stop)
        self.assertEqual(results[0]["verdict"], "pass")
        self.assertEqual(results[0]["evidence"], quote)

    def test_a_quote_of_two_adjacent_paragraphs_is_accepted(self):
        page = FakePage(snapshot='- heading "Shop"\n'
                                 '- text: Nothing is being saved right now.\n'
                                 '- text: Next: Sunday Morning, Sat 20:32.')
        quote = "Nothing is being saved right now. Next: Sunday Morning, Sat 20:32."
        results, _ = self.run_steps(ScriptedModel([verdict(evidence=quote, note="the scheduler is idle"),
                                                   verdict(), verdict()]), page)
        self.assertEqual(results[0]["verdict"], "pass")
        self.assertEqual(results[0]["evidence"], quote)

    def test_a_quote_of_the_snapshot_format_with_role_prefixes_is_accepted(self):
        page = FakePage(snapshot='- heading "Shop"\n'
                                 '- button "Open Road Trip": Road Trip\n- text: 20 of 20 saved')
        quote = 'button "Open Road Trip": Road Trip — text: 20 of 20 saved'
        results, _ = self.run_steps(ScriptedModel([verdict(evidence=quote, note="20 of 20 saved"),
                                                   verdict(), verdict()]), page)
        self.assertEqual(results[0]["verdict"], "pass")
        self.assertEqual(results[0]["evidence"], quote)

    def test_a_pass_about_another_job_than_the_earlier_step_named_is_refused(self):
        page = FakePage(snapshot='- text "job HL-L2400DWE-17 completed"\n- text "job HL-L2400DWE-18 completed"')
        model = ScriptedModel([
            verdict(note="Test page sent; job ID HL-L2400DWE-18 confirmed.", evidence="HL-L2400DWE-18"),
            verdict(note="job HL-L2400DWE-17 is completed", evidence="job HL-L2400DWE-17 completed"),
            verdict(note="job HL-L2400DWE-18 is completed", evidence="job HL-L2400DWE-18 completed"),
            verdict(evidence="HL-L2400DWE-18")])
        results, _ = self.run_steps(model, page)
        self.assertEqual([r["verdict"] for r in results], ["pass", "pass", "pass"])
        self.assertEqual(results[1]["note"], "job HL-L2400DWE-18 is completed")
        refusal = model.asked[2]["history"][-1]["error"]
        self.assertEqual(refusal, "step 1 named HL-L2400DWE-18, and this pass names HL-L2400DWE-17: "
                                  "find HL-L2400DWE-18 on the page, or give fail or inconclusive")

    def test_the_same_wrong_job_three_times_is_inconclusive(self):
        page = FakePage(snapshot='- text "ORD-7 shipped"')
        wrong = verdict(note="ORD-7 shipped", evidence="ORD-7 shipped")
        model = ScriptedModel([verdict(note="placed order ORD-9", evidence="ORD-7"), wrong, wrong, wrong,
                               verdict(evidence="shipped")])
        results, _ = self.run_steps(model, page)
        self.assertEqual(results[1]["verdict"], "inconclusive")
        self.assertTrue(results[1]["note"].startswith("refused three times: step 1 named ORD-9"))

    def test_ids_that_agree_or_were_never_named_pass(self):
        self.assertIsNone(U.id_drift({"note": "job HL-18 done"}, [{"step": 1, "note": "sent HL-18"}]))
        self.assertIsNone(U.id_drift({"note": "job HL-17 done"}, [{"step": 1, "note": "no job named"}]))
        self.assertIsNone(U.id_drift({"note": "jobs HL-17 and HL-18"}, [{"step": 1, "note": "sent HL-18"}]))
        self.assertIsNone(U.id_drift({"note": "HL-17"}, [{"step": 1, "note": "saw HL-16 and HL-15"}]))
        self.assertEqual(U.named_ids("run 1041-user-print-clone-user-20260925-200450, on 2026-09-24"),
                         {"user-print-clone-user-20260925": {"200450"}})

    def test_a_refused_quote_names_the_fragment_the_page_does_not_show(self):
        page = FakePage(snapshot='- heading "Shop"\n'
                                 '- listitem "Lofi Jazzy": Lofi Jazzy: 7 songs, 2 saved, 5 not saved\n'
                                 '- text: 6 total')
        quote = "Lofi Jazzy: 7 songs, 2 saved, 5 not saved — Nujabes - Feather"
        model = ScriptedModel([verdict(evidence=quote, note="both albums"),
                               verdict(evidence="6 total", note="the one album on the page"),
                               verdict(), verdict()])
        results, _ = self.run_steps(model, page)
        self.assertEqual(model.asked[1]["history"][0]["error"],
                         'evidence not found on the page: "Nujabes - Feather"')
        self.assertEqual(results[0], {"step": 1, "verdict": "pass",
                                      "note": "the one album on the page", "evidence": "6 total"})

    def test_a_pass_whose_quote_is_not_on_the_page_is_refused_and_asked_again(self):
        page = FakePage(snapshot='- heading "Shop"\n- text "job 17: printing"')
        model = ScriptedModel([verdict(evidence="job 18", note="job 18 shipped"),
                               verdict(evidence="job 17", note="job 17 shipped"),
                               verdict(), verdict()])
        results, stop = self.run_steps(model, page)
        self.assertIsNone(stop)
        self.assertEqual(S.STATS["calls"], 4)  # the refused answer cost a call like any other
        self.assertEqual(model.asked[1]["history"][0]["error"],
                         'evidence not found on the page: "job 18"')
        self.assertEqual(results[0], {"step": 1, "verdict": "pass", "note": "job 17 shipped",
                                      "evidence": "job 17"})

    def test_a_pass_without_an_evidence_field_is_refused(self):
        model = ScriptedModel([{"do": "verdict", "verdict": "pass", "note": "looks right"},
                               verdict(), verdict(), verdict()])
        results, _ = self.run_steps(model, FakePage())
        self.assertEqual(model.asked[1]["history"][0]["error"],
                         "evidence not found on the page")  # no fragment to name
        self.assertEqual(results[0]["verdict"], "pass")

    def test_a_note_naming_an_earlier_job_cannot_pass_a_page_showing_another(self):
        page = FakePage(snapshot='- heading "Shop"\n- text "job 17: queued"')
        model = ScriptedModel([verdict(evidence="Shop", note="queued print job 18"),
                               verdict(evidence="job 18: done", note="job 18 is done"),
                               verdict("fail", "the page shows job 17: queued, not job 18"),
                               verdict()])
        results, _ = self.run_steps(model, page)
        self.assertEqual([r["verdict"] for r in results], ["pass", "fail", "pass"])
        self.assertEqual(model.asked[2]["history"][0]["error"],
                         'evidence not found on the page: "job 18: done"')

    def test_a_failed_action_is_fed_back_to_the_model_not_fatal(self):
        model = ScriptedModel([{"do": "click", "role": "button", "name": "Gone"}, verdict("fail", "no such button"),
                               verdict(), verdict()])
        results, _ = self.run_steps(model, FakePage(fail_on={"Gone"}))
        self.assertIn("error", model.asked[1]["history"][0])
        self.assertEqual([r["verdict"] for r in results], ["fail", "pass", "pass"])

    def test_a_reply_that_is_not_an_action_costs_a_call_and_is_said_so(self):
        model = ScriptedModel([{"do": "invalid", "reply": "I think I will click"}, verdict(),
                               {"do": "verdict", "verdict": "maybe"}, verdict(), verdict()])
        results, _ = self.run_steps(model, FakePage())
        self.assertEqual([r["verdict"] for r in results], ["pass", "pass", "pass"])
        self.assertIn("JSON", model.asked[1]["history"][0]["error"])
        self.assertIn("inconclusive", model.asked[3]["history"][0]["error"])
        self.assertEqual(S.STATS["calls"], 5)

    def test_a_model_inconclusive_verdict_is_recorded_and_never_fail(self):
        model = ScriptedModel([verdict("inconclusive", "the page showed nothing to judge"),
                               verdict(), verdict()])
        results, stop = self.run_steps(model, FakePage())
        self.assertIsNone(stop)
        self.assertEqual([r["verdict"] for r in results], ["inconclusive", "pass", "pass"])
        self.assertEqual(results[0]["note"], "the page showed nothing to judge")
        self.assertNotIn("evidence", results[0])
        self.assertEqual(self.jsonl(), results)

    def test_calls_exhausted_records_inconclusive_on_the_step_and_after(self):
        model = ScriptedModel([{"do": "wait", "seconds": 1}, {"do": "wait", "seconds": 1}])
        results, stop = self.run_steps(model, FakePage(), max_calls=2)
        self.assertEqual(stop, "calls")
        self.assertEqual([r["verdict"] for r in results], ["inconclusive"] * 3)
        self.assertEqual(results[0]["note"], "calls exhausted")
        self.assertEqual(self.jsonl(), results)

    def test_a_repeated_action_on_an_unchanged_page_is_stuck(self):
        model = ScriptedModel([{"do": "click", "role": "button", "name": "Buy"}] * 3
                              + [verdict(), verdict(), verdict()])
        results, stop = self.run_steps(model, FakePage())
        self.assertIsNone(stop)
        self.assertEqual(results[0], {"step": 1, "verdict": "inconclusive", "note": 'stuck: button "Buy"'})
        self.assertEqual([r["verdict"] for r in results[1:]], ["pass", "pass"])
        self.assertEqual(S.STATS["calls"], 5)

    def test_a_wait_never_counts_as_stuck_and_a_quote_still_passes(self):
        # live: a step said "wait 30 seconds and look again" and ended
        # "stuck: 5s" because the wait repeated on an unchanged page
        model = ScriptedModel([{"do": "wait", "seconds": 5}] * 5
                              + [verdict(evidence="Shop", note="the shop is there"),
                                 verdict(), verdict()])
        results, stop = self.run_steps(model, FakePage(snapshot='- heading "Shop"'))
        self.assertIsNone(stop)
        self.assertEqual(results[0], {"step": 1, "verdict": "pass", "note": "the shop is there",
                                      "evidence": "Shop"})
        self.assertEqual([r["verdict"] for r in results], ["pass", "pass", "pass"])
        self.assertEqual(S.STATS["calls"], 8)

    def test_the_stuck_count_starts_again_when_the_snapshot_changes(self):
        # two identical clicks on an unchanged page, then the page changes and
        # two more: the count started again, so no third repeat is a stuck one
        click = {"do": "click", "role": "button", "name": "Buy"}
        page = ChangingPage(['- heading "Shop"', '- heading "Shop"',
                             '- heading "Shop"\n- text "cart: 1"'])
        model = ScriptedModel([click] * 4 + [verdict(), verdict(), verdict()])
        results, stop = self.run_steps(model, page)
        self.assertIsNone(stop)
        self.assertEqual([r["verdict"] for r in results], ["pass", "pass", "pass"])
        self.assertNotIn("stuck", results[0]["note"])

    def test_a_refused_verdict_never_counts_as_stuck_and_names_the_quote(self):
        quote = ("job 18: shipped to the customer in the north warehouse and signed "
                 "for by the receiver on Tuesday")
        page = FakePage(snapshot='- heading "Shop"\n- text "job 17: queued"')
        model = ScriptedModel([verdict(evidence=quote, note="job 18 shipped")] * 3
                              + [verdict(), verdict()])
        results, stop = self.run_steps(model, page)
        self.assertIsNone(stop)
        self.assertEqual(results[0]["verdict"], "inconclusive")
        self.assertEqual(results[0]["note"], "evidence not found: " + quote[:80])
        self.assertNotIn("stuck", results[0]["note"])
        self.assertEqual([r["verdict"] for r in results[1:]], ["pass", "pass"])
        self.assertEqual(S.STATS["calls"], 5)

    def test_a_verdict_between_identical_clicks_does_not_reset_the_stuck_count(self):
        # a verdict resets nothing: the same click on an unchanged page is
        # still stuck after three of them, however many verdicts came between
        click = {"do": "click", "role": "button", "name": "Buy"}
        refused = verdict(evidence="job 18", note="job 18 shipped")
        model = ScriptedModel([click, refused, click, refused, click,
                               verdict(), verdict()])
        page = FakePage(snapshot='- heading "Shop"\n- text "job 17: queued"')
        results, stop = self.run_steps(model, page)
        self.assertIsNone(stop)
        self.assertEqual(results[0], {"step": 1, "verdict": "inconclusive", "note": 'stuck: button "Buy"'})
        self.assertEqual([r["verdict"] for r in results[1:]], ["pass", "pass"])

    def test_the_call_envelope_leaves_the_step_it_ran_out_in_inconclusive(self):
        model = ScriptedModel([verdict(), {"do": "wait", "seconds": 1}, {"do": "wait", "seconds": 1}])
        page = FakePage()
        results, stop = self.run_steps(model, page, max_calls=3)
        self.assertEqual(stop, "calls")
        self.assertEqual([r["verdict"] for r in results], ["pass", "inconclusive", "inconclusive"])
        self.assertEqual(results[1]["note"], "calls exhausted")
        self.assertIn("not reached", results[2]["note"])
        self.assertEqual(page.shots, ["01.png", "02.png"])  # step 3 was never taken
        self.assertEqual(len(self.jsonl()), 3)

    def test_the_wall_envelope(self):
        results, stop = self.run_steps(ScriptedModel([]), FakePage(), deadline=0)
        self.assertEqual(stop, "seconds")
        self.assertEqual([r["verdict"] for r in results], ["fail"] * 3)

    def test_parse_action(self):
        self.assertEqual(U.parse_action('Sure.\n{"do": "press", "key": "Enter"}'), {"do": "press", "key": "Enter"})
        self.assertEqual(U.parse_action("click it")["do"], "invalid")
        self.assertEqual(U.parse_action('{"verdict": "pass"}')["do"], "invalid")


class ScriptedJudge:
    """expected_target from a list of reply texts, in order; each question is
    kept, and when a `log` is given the call is written to it, so a test can
    see it came before the step's first action."""

    def __init__(self, replies, log=None):
        self.replies = list(replies)
        self.asked = []
        self.log = log

    def expected_target(self, text, snapshot, timeout=None):
        self.asked.append({"text": text, "snapshot": snapshot, "timeout": timeout})
        if self.log is not None:
            self.log.append(("judge", text))
        return self.replies.pop(0)


class LoggedPage(FakePage):
    """A FakePage writing each action to a shared log."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def act(self, action):
        self.log.append(("act", action.get("name")))
        super().act(action)


class Judge(unittest.TestCase):
    """The judge: a separate call before each step's first action names where
    the result should appear; its answer, or null and why, lands in
    steps.jsonl and changes nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_session(self.tmp, {"token": TOKEN})
        self.records = S.RECORDS_DIR

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_steps(self, model, page, judge, max_calls=20, deadline=1e12):
        return U.run_steps(model, page, "https://app.example.test/", STEPS, self.records, max_calls, deadline,
                           judge=judge)

    def jsonl(self):
        with open(os.path.join(self.records, "steps.jsonl")) as f:
            return [json.loads(l) for l in f]

    def test_the_judge_is_asked_before_the_first_action_and_its_answer_lands_in_steps_jsonl(self):
        log = []
        model = ScriptedModel([{"do": "click", "role": "link", "name": "Sign in"}, verdict(note="the form is shown"),
                               verdict(), verdict()])
        judge = ScriptedJudge([json.dumps({"expected": 'heading "Sign in"', "why": "the form opens under it."}),
                               '{"expected": "Signed in as demo", "why": "the header greets the user."}',
                               'Sure: {"expected": "1 item", "why": "the cart badge counts it."}'], log)
        results, stop = self.run_steps(model, LoggedPage(log), judge)
        self.assertIsNone(stop)
        self.assertEqual(log[:2], [("judge", STEPS[0]), ("act", "Sign in")])
        self.assertEqual([q["text"] for q in judge.asked], STEPS)
        self.assertEqual(judge.asked[0]["snapshot"], '- heading "Shop"')
        rows = self.jsonl()
        self.assertEqual(rows, results)
        self.assertEqual(rows[0], {"step": 1, "verdict": "pass", "note": "the form is shown", "evidence": "Shop",
                                   "expected": 'heading "Sign in"', "expected_why": "the form opens under it."})
        self.assertEqual([r["expected"] for r in rows], ['heading "Sign in"', "Signed in as demo", "1 item"])
        # the judge's three calls count against the envelope beside the acting model's four
        self.assertEqual(S.STATS["calls"], 7)
        with open(os.path.join(self.records, "stream.jsonl")) as f:
            stream = [json.loads(l) for l in f]
        self.assertEqual(stream[0]["judge"], {"expected": 'heading "Sign in"',
                                              "expected_why": "the form opens under it."})
        self.assertNotIn("action", stream[0])

    def test_a_judge_answer_that_is_not_json_records_null_and_the_step_runs_as_before(self):
        model = ScriptedModel([{"do": "click", "role": "link", "name": "Sign in"}, verdict(note="the form is shown"),
                               verdict(), verdict("fail", note="no cart button")])
        judge = ScriptedJudge(["the sign-in form, I think", '{"why": "no target"}', '{"expected": "Cart"}'])
        page = FakePage()
        results, stop = self.run_steps(model, page, judge)
        self.assertIsNone(stop)
        rows = self.jsonl()
        self.assertIsNone(rows[0]["expected"])
        self.assertEqual(rows[0]["expected_why"], "judge reply is not JSON: the sign-in form, I think")
        self.assertIsNone(rows[1]["expected"])
        self.assertIn("names no expected target", rows[1]["expected_why"])
        self.assertEqual((rows[2]["expected"], rows[2]["expected_why"]), ("Cart", ""))
        # everything else is what a run without a judge records
        self.assertEqual([{k: v for k, v in r.items() if not k.startswith("expected")} for r in rows],
                         [{"step": 1, "verdict": "pass", "note": "the form is shown", "evidence": "Shop"},
                          {"step": 2, "verdict": "pass", "note": "as asked", "evidence": "Shop"},
                          {"step": 3, "verdict": "fail", "note": "no cart button"}])
        self.assertEqual([a["name"] for _, a in page.done[1:]], ["Sign in"])

    def test_a_failed_judge_call_records_null_with_the_reason(self):
        class Down:
            def expected_target(self, text, snapshot, timeout=None):
                raise U.ModelError("HTTP 503: down")
        results, stop = self.run_steps(ScriptedModel([verdict(), verdict(), verdict()]), FakePage(), Down())
        self.assertEqual([r["verdict"] for r in results], ["pass"] * 3)
        self.assertEqual(results[0]["expected_why"], "judge call failed: HTTP 503: down")
        self.assertIsNone(results[0]["expected"])

    def test_the_judge_counts_against_the_calls_envelope(self):
        judge = ScriptedJudge(['{"expected": "Shop", "why": "it is the page."}'] * 3)
        results, stop = self.run_steps(ScriptedModel([verdict(), verdict()]), FakePage(), judge, max_calls=3)
        self.assertEqual(stop, "calls")
        self.assertEqual([r["verdict"] for r in results], ["pass", "inconclusive", "inconclusive"])
        self.assertEqual(results[1]["expected"], "Shop")  # step 2's judge took the third call
        self.assertEqual(results[2]["expected_why"], "not asked: the calls envelope was spent")

    def test_the_judges_prompt_carries_the_step_and_page_and_none_of_the_acting_models_messages(self):
        llm = fakes.FakeLLM([{"content": '{"expected": "Order ORD-7", "why": "the confirmation shows it."}'}])
        self.addCleanup(llm.close)
        m = U.ChatModel({"llm_url": f"{llm.url}/v1", "llm_model": "local-a", "spec": "Buy one item.",
                         "steps": STEPS})
        self.assertEqual(U.parse_expected(m.expected_target(STEPS[2], '- button "Add to cart"')),
                         ("Order ORD-7", "the confirmation shows it."))
        system, user = (msg["content"] for msg in llm.requests[0]["messages"])
        self.assertEqual(system, U.JUDGE_SYSTEM)
        self.assertIn(STEPS[2], user)
        self.assertIn('button "Add to cart"', user)
        for acting in ("Buy one item.", "EARLIER STEPS", "ACTIONS SO FAR", U.SYSTEM):
            self.assertNotIn(acting, system + user)


class FakeClock:
    """time.time for a test: it moves only when a fake model spends time."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class SlowModel:
    """A model that takes `seconds` of the fake clock per reply, a wait. A call
    given a timeout shorter than that, or than `llm_timeout` when it is given
    none, uses up that timeout and fails the way urlopen's timeout does. Every
    call's start and timeout are kept."""

    def __init__(self, clock, seconds, llm_timeout=300):
        self.clock, self.seconds, self.llm_timeout = clock, seconds, llm_timeout
        self.calls = []

    def next_action(self, n, text, url, snapshot, history, earlier=None, timeout=None):
        self.calls.append({"start": self.clock.now, "timeout": timeout})
        limit = self.llm_timeout if timeout is None else min(self.llm_timeout, timeout)
        if self.seconds > limit:
            self.clock.now += limit
            raise U.ModelError("TimeoutError: timed out")
        self.clock.now += self.seconds
        return {"do": "wait", "seconds": 1}


class WallEnvelope(unittest.TestCase):
    """Issue 1072: the runner kills the sandbox when the wall envelope is
    spent, so the arm must end the run itself before that, with room left to
    write and push its records: no call starts inside the reserve, and no call
    may run past it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        reset_session(self.tmp, {"token": TOKEN})
        self.records = S.RECORDS_DIR
        self.clock = FakeClock(1000.0)

    def run_steps(self, model, max_seconds, judge=None):
        self.deadline = self.clock.now + max_seconds
        return U.run_steps(model, FakePage(), "https://app.example.test/", STEPS, self.records, 20,
                           self.deadline, clock=self.clock, judge=judge)

    def test_the_judge_is_held_to_the_same_reserve_as_the_acting_model(self):
        # The judge (PR #9) and the reserve (PR #10) meet here: a judge that
        # takes 250 s a reply against a 400 s envelope must be given only the
        # 280 s before the reserve, and no call may start or run inside it.
        clock = self.clock

        class SlowJudge:
            def __init__(self):
                self.calls = []

            def expected_target(self, text, snapshot, timeout=None):
                self.calls.append({"start": clock.now, "timeout": timeout})
                clock.now += min(250, timeout if timeout is not None else 250)
                return '{"expected": "the cart", "why": "it lists items"}'

        judge, model = SlowJudge(), SlowModel(clock, 250)
        results, stop = self.run_steps(model, max_seconds=400, judge=judge)
        self.assertEqual(stop, "seconds")
        spend_by = self.deadline - U.RECORDS_RESERVE_SECONDS
        self.assertEqual(judge.calls[0]["timeout"], spend_by - 1000.0)
        for c in judge.calls + model.calls:
            self.assertLess(c["start"], spend_by)
            self.assertLessEqual(c["start"] + c["timeout"], spend_by)
        self.assertLessEqual(clock.now, spend_by)

    def jsonl(self):
        with open(os.path.join(self.records, "steps.jsonl")) as f:
            return [json.loads(l) for l in f]

    def test_a_slow_model_ends_the_run_on_seconds_with_its_records_written_in_time(self):
        # 250 s a reply against a 600 s envelope: the third call would start at
        # 500 s and end at 750 s, long after the runner killed the sandbox
        model = SlowModel(self.clock, seconds=250)
        results, stop = self.run_steps(model, max_seconds=600)
        self.assertEqual(stop, "seconds")
        self.assertLessEqual(self.clock.now, self.deadline)  # steps.jsonl was written before max_seconds
        self.assertLessEqual(self.clock.now, self.deadline - U.RECORDS_RESERVE_SECONDS)
        self.assertEqual([r["verdict"] for r in self.jsonl()], ["fail"] * 3)
        self.assertEqual(results[0]["note"], "the wall envelope was spent before a verdict")

    def test_no_call_starts_inside_the_reserve(self):
        model = SlowModel(self.clock, seconds=100)
        _, stop = self.run_steps(model, max_seconds=1000)
        self.assertEqual(stop, "seconds")
        self.assertLessEqual(self.clock.now, self.deadline)
        self.assertTrue(model.calls)
        for call in model.calls:
            self.assertGreater(self.deadline - call["start"], U.RECORDS_RESERVE_SECONDS, call)

    def test_no_call_is_given_longer_than_the_time_left_before_the_reserve(self):
        model = SlowModel(self.clock, seconds=100)
        self.run_steps(model, max_seconds=1000)
        for call in model.calls:
            self.assertIsNotNone(call["timeout"], call)
            self.assertLessEqual(call["timeout"], self.deadline - U.RECORDS_RESERVE_SECONDS - call["start"], call)

    def test_a_model_error_with_time_left_is_still_a_model_error(self):
        class Down:
            def next_action(self, *a, **kw):
                raise U.ModelError("HTTP 503: down")

        with self.assertRaises(U.ModelError):
            self.run_steps(Down(), max_seconds=1000)


class Trail(unittest.TestCase):
    """Item 2: every step leaves a trail.jsonl beside its screenshot, one line
    per action with the requests the page made before the next snapshot, and
    a 4xx/5xx is named in the step's note in steps.jsonl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        reset_session(self.tmp, {"token": TOKEN})
        self.records = S.RECORDS_DIR
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LikeHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def trail(self, n):
        with open(os.path.join(self.records, "steps", f"{n:02d}.trail.jsonl")) as f:
            return [json.loads(l) for l in f]

    def test_a_502_fetch_lands_in_the_trail_and_names_it_in_the_step_note(self):
        model = ScriptedModel([{"do": "click", "role": "button", "name": "Like"},
                               verdict("fail", "the like came back an error"),
                               verdict(), verdict()])
        results, stop = U.run_steps(model, ServerPage(self.base), "https://app.example.test/",
                                    STEPS, self.records, 20, 1e12)
        self.assertIsNone(stop)
        lines = self.trail(1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["action"], {"do": "click", "role": "button", "name": "Like"})
        self.assertEqual(lines[0]["target"], 'button "Like"')
        self.assertIsInstance(lines[0]["t"], (int, float))
        self.assertEqual(lines[0]["requests"],
                         [{"method": "GET", "url": self.base + "/api/liked", "status": 502}])
        self.assertIn("saw 502 GET /api/liked", results[0]["note"])
        self.assertIn("the like came back an error", results[0]["note"])
        self.assertEqual([r["verdict"] for r in results], ["fail", "pass", "pass"])

    def test_every_step_writes_a_trail_beside_its_screenshot_even_when_empty(self):
        U.run_steps(ScriptedModel([verdict(), verdict(), verdict()]), FakePage(),
                    "https://app.example.test/", STEPS, self.records, 20, 1e12)
        self.assertEqual(sorted(os.listdir(os.path.join(self.records, "steps"))),
                         ["01.png", "01.trail.jsonl", "02.png", "02.trail.jsonl",
                          "03.png", "03.trail.jsonl"])
        self.assertEqual(self.trail(1), [])


class Recording(unittest.TestCase):
    """PlaywrightPage, over a fake playwright.sync_api, records a trace and a
    video into the records directory, and never lets the recording change the
    run: what goes wrong with it is skipped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.records = os.path.join(self.tmp, "records")
        os.makedirs(self.records)

    def page(self, **kw):
        self.pw = FakePlaywright(**kw).install(self)
        with contextlib.redirect_stderr(io.StringIO()) as self.err:
            return U.PlaywrightPage(self.records)

    def test_the_trace_starts_before_the_page_and_both_files_land_beside_steps_jsonl(self):
        page = self.page()
        video_dir = self.pw.kwargs("new_context")[0]["record_video_dir"]
        self.assertEqual(self.pw.names(), ["launch", "new_context", "tracing.start", "new_page"])
        self.assertEqual(self.pw.kwargs("tracing.start"), [{"screenshots": True, "snapshots": True}])
        # the video is as large as the viewport the screenshots are taken in
        self.assertEqual(self.pw.kwargs("new_context")[0],
                         {"viewport": U.VIEWPORT, "record_video_dir": video_dir, "record_video_size": U.VIEWPORT})
        page.close()
        self.assertEqual(self.pw.names()[4:], ["tracing.stop", "context.close", "browser.close", "stop"])
        self.assertEqual(self.pw.kwargs("tracing.stop"), [{"path": os.path.join(self.records, "trace.zip")}])
        self.assertEqual(sorted(os.listdir(self.records)), ["trace.zip", "video.webm"])
        with open(os.path.join(self.records, "video.webm"), "rb") as f:
            self.assertEqual(f.read(), b"webm")
        self.assertFalse(os.path.exists(video_dir))  # the scratch directory the video was written in

    def test_the_trail_keeps_the_targets_requests_and_pairs_their_status(self):
        page = self.page()
        page.goto("https://app.example.test/")
        fake = self.pw.contexts[0].page
        liked = types.SimpleNamespace(method="GET", url="https://app.example.test/api/liked")
        pixel = types.SimpleNamespace(method="GET", url="https://cdn.other.test/pixel.gif")
        fake.emit("request", liked)
        fake.emit("request", pixel)  # another origin: not the trail's business
        fake.emit("response", types.SimpleNamespace(status=502, url=liked.url, request=liked))
        self.assertEqual(page.drain_requests(),
                         [{"method": "GET", "url": liked.url, "status": 502}])
        self.assertEqual(page.drain_requests(), [])

    def test_the_names_are_the_records_index(self):
        self.assertEqual((U.TRACE_FILE, U.VIDEO_FILE), ("trace.zip", "video.webm"))
        paths = U.records_paths("/r")
        self.assertEqual(sorted(paths), ["README.md", "steps", "steps.jsonl", "trace.zip", "video.webm"])
        self.assertEqual((paths["trace.zip"], paths["video.webm"]), ("/r/trace.zip", "/r/video.webm"))

    def test_without_a_records_directory_nothing_is_recorded(self):
        FakePlaywright().install(self)
        page = U.PlaywrightPage()
        page.close()
        self.assertEqual(os.listdir(self.records), [])

    def test_a_trace_that_cannot_start_is_skipped_and_the_video_is_kept(self):
        self.page(no_trace=True).close()
        self.assertIn("trace not started", self.err.getvalue())
        self.assertNotIn("tracing.stop", self.pw.names())
        self.assertEqual(sorted(os.listdir(self.records)), ["video.webm"])

    def test_a_video_that_cannot_be_recorded_is_skipped_and_the_trace_is_kept(self):
        page = self.page(no_video=True)  # an image without Playwright's ffmpeg
        self.assertIn("video recording unavailable", self.err.getvalue())
        self.assertEqual(self.pw.names().count("new_context"), 2)
        self.assertNotIn("record_video_dir", self.pw.kwargs("new_context")[1])
        page.close()
        self.assertEqual(sorted(os.listdir(self.records)), ["trace.zip"])

    def test_a_browser_that_died_closes_without_raising_and_leaves_no_trace(self):
        page = self.page(dies=True)
        video_dir = self.pw.kwargs("new_context")[0]["record_video_dir"]
        page.close()
        self.assertNotIn("trace.zip", os.listdir(self.records))
        self.assertEqual(self.pw.names()[-4:], ["tracing.stop", "context.close", "browser.close", "stop"])
        self.assertFalse(os.path.exists(video_dir))

    def test_a_browser_that_will_not_start_is_still_the_environment(self):
        FakePlaywright().install(self)
        with mock.patch.object(FakePlaywright, "launch", side_effect=RuntimeError("no display")):
            with self.assertRaises(U.BrowserError):
                U.PlaywrightPage(self.records)


class Model(unittest.TestCase):
    """ChatModel asks the provider entry the session arm uses."""

    def setUp(self):
        for k in S.STATS:
            S.STATS[k] = 0

    def test_one_question_one_action_and_the_providers_token_counts(self):
        llm = fakes.FakeLLM([{"content": '{"do": "click", "role": "button", "name": "Buy"}',
                              "tokens_in": 70, "tokens_out": 9}])
        self.addCleanup(llm.close)
        m = U.ChatModel({"llm_url": f"{llm.url}/v1", "llm_model": "local-a", "max_tokens": 512,
                         "think": "low", "think_chars": 300, "think_api": "reasoning_effort",
                         "spec": "Buy one item.", "steps": STEPS})
        a = m.next_action(2, STEPS[1], "https://app.example.test/", "- button \"Buy\"", [])
        self.assertEqual(a, {"do": "click", "role": "button", "name": "Buy"})
        body = llm.requests[0]
        self.assertEqual((body["model"], body["reasoning_effort"], body["max_tokens"]), ("local-a", "low", 612))
        self.assertIn("STEP 2 of 3: Sign in as the demo user", body["messages"][1]["content"])
        self.assertIn('button "Buy"', body["messages"][1]["content"])
        self.assertEqual((S.STATS["tokens_in"], S.STATS["tokens_out"]), (70, 9))

    def test_a_call_is_given_no_longer_than_the_time_it_is_left(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(timeout)
            raise urllib.error.URLError("timed out")

        m = U.ChatModel({"llm_url": "http://llm.example.test/v1", "llm_model": "m", "llm_timeout": 300})
        with mock.patch.object(U.urllib.request, "urlopen", urlopen):
            for left in (42.5, None, 900):
                with self.assertRaises(U.ModelError):
                    m.next_action(1, "x", "u", "", [], timeout=left)
        self.assertEqual(seen, [42.5, 300, 300])

    def test_an_endpoint_failure_is_a_model_error(self):
        llm = fakes.FakeLLM([{"status": 503, "content": "down"}])
        self.addCleanup(llm.close)
        with self.assertRaises(U.ModelError):
            U.ChatModel({"llm_url": f"{llm.url}/v1", "llm_model": "m"}).next_action(1, "x", "u", "", [])


class Executor(unittest.TestCase):
    """main(): the verdicts, the AGENT-DONE tag the runner reads, and the
    records push, end to end against the fake Gitea and a file:// records
    repository."""

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
        self.gitea.issues["dark-records/s1"] = {1: {"title": "user", "body": "", "state": "open", "comments": []}}
        reset_session(self.tmp, {
            "gitea": self.gitea.url, "git_url": git_url, "repo": "dark-records/s1",
            "records_repo": "dark-records/s1", "issue": 1, "token": TOKEN, "mode": "user",
            "url": "https://app.example.test/", "steps": STEPS, "spec": "Check that a visitor can buy.",
            "llm_model": "local-a", "max_calls": 20, "max_seconds": 600, "run": "shop-user-1",
            "heartbeat_seconds": 3600})
        S.API = f"{self.gitea.url}/api/v1"
        S.REPO = "dark-records/s1"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def done_tag(self):
        body = [b for b in self.gitea.bodies("dark-records/s1", 1) if b.startswith("AGENT-DONE")][-1]
        tag = [t for t in R.parse_tags(body) if t.get("ev") == "done"][-1]
        self.assertTrue(spec.tag_ok("done", tag))
        req, opt = spec.AGENT_TAGS["done"]
        self.assertEqual(sorted(set(tag) - {"v", "ev"} - set(req) - set(opt)), [])
        return body, tag

    def records(self):
        """Every file of the records repo, as bytes, from a fresh clone."""
        dest = os.path.join(self.tmp, "check")
        fakes.git("clone", "-q", os.path.join(self.repos, "dark-records/s1.git"), dest)
        out = {}
        for dp, dns, fns in os.walk(dest):
            dns[:] = [d for d in dns if d != ".git"]
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as f:
                    out[os.path.relpath(os.path.join(dp, fn), dest)] = f.read()
        return out

    def test_every_step_pass_is_a_pass_with_the_records_pushed(self):
        page = FakePage(snapshot=f"- heading \"Shop\"\n- text \"session {TOKEN}\"")  # a page that shows the token
        rc = U.main(ScriptedModel([verdict(), verdict(note=f"saw {TOKEN} on the page", evidence=TOKEN),
                                   verdict()]), page)
        self.assertEqual(rc, 0)
        self.assertTrue(page.closed)
        body, tag = self.done_tag()
        self.assertTrue(body.startswith("AGENT-DONE ok steps=3/3"))
        self.assertEqual((tag["outcome"], tag["steps_ok"], tag["steps_total"], tag["calls"]), ("ok", 3, 3, 3))
        self.assertEqual(tag["records"], "dark-records/s1/shop-user-1")
        self.assertIn("records_sha256", tag)
        files = self.records()
        run = "shop-user-1/"
        for name in ("stream.jsonl", "brief.md", "task.json", "steps.jsonl",
                     "steps/01.png", "steps/02.png", "steps/03.png"):
            self.assertIn(run + name, files)
        self.assertEqual([json.loads(l)["verdict"] for l in files[run + "steps.jsonl"].decode().splitlines()],
                         ["pass", "pass", "pass"])
        self.assertTrue(files[run + "steps/02.png"].startswith(PNG))

    def test_the_readme_says_the_outcome_and_every_step_without_the_token(self):
        page = FakePage(snapshot=f"- heading \"Shop\"\n- text \"session {TOKEN}\"")
        rc = U.main(ScriptedModel([verdict(), verdict(note=f"saw {TOKEN} | twice", evidence=TOKEN), verdict()]), page)
        self.assertEqual(rc, 0)
        readme = self.records()["shop-user-1/README.md"].decode()
        self.assertTrue(readme.startswith("# shop-user-1\n"))
        self.assertIn("**🟢 PASS**: 3/3 steps pass", readme)
        self.assertIn(f"| 1. {STEPS[0]} | 🟢 PASS |", readme)
        self.assertIn("\\| twice", readme)  # a pipe in a note never breaks the table
        self.assertNotIn(TOKEN, readme)
        self.assertIn("- `trace.zip`: the Playwright trace", readme)

    def test_a_failed_run_shows_the_failed_step(self):
        rc = U.main(ScriptedModel([verdict(), verdict("fail", note="no basket"), verdict()]), FakePage())
        self.assertEqual(rc, 1)
        readme = self.records()["shop-user-1/README.md"].decode()
        self.assertIn("**🔴 FAIL**: (", readme)
        self.assertIn("| 🔴 FAIL | no basket |", readme)

    def test_the_trace_and_the_video_are_pushed_beside_steps_jsonl(self):
        rc = U.main(ScriptedModel([verdict(), verdict(), verdict()]), RecordedPage(S.RECORDS_DIR))
        self.assertEqual(rc, 0)
        files = self.records()
        run = "shop-user-1/"
        self.assertEqual((files[run + "trace.zip"], files[run + "video.webm"]), (b"PK trace", b"webm"))
        # the recording added files; the steps, verdicts and screenshot names are the ones without it
        for name in ("steps.jsonl", "steps/01.png", "steps/02.png", "steps/03.png"):
            self.assertIn(run + name, files)
        self.assertEqual(sorted(k[len(run):] for k in files if k.startswith(run + "steps/")),
                         ["steps/01.png", "steps/01.trail.jsonl",
                          "steps/02.png", "steps/02.trail.jsonl",
                          "steps/03.png", "steps/03.trail.jsonl"])

    def test_a_missing_trace_does_not_fail_the_run(self):
        rc = U.main(ScriptedModel([verdict(), verdict(), verdict()]), RecordedPage(S.RECORDS_DIR, trace=False))
        self.assertEqual(rc, 0)
        self.assertEqual(self.done_tag()[1]["outcome"], "ok")
        files = self.records()
        self.assertIn("shop-user-1/video.webm", files)
        self.assertNotIn("shop-user-1/trace.zip", files)
        self.assertIn("shop-user-1/steps.jsonl", files)

    def test_a_browser_that_dies_mid_run_still_writes_steps_jsonl(self):
        class Dies(RecordedPage):
            def screenshot(self, path):
                if os.path.basename(path) == "02.png":
                    raise U.BrowserError("Target page, context or browser has been closed")
                super().screenshot(path)
        rc = U.main(ScriptedModel([verdict(), verdict()]), Dies(S.RECORDS_DIR, video=False))
        self.assertEqual(rc, 1)
        self.assertEqual(self.done_tag()[1]["kind"], "env")
        files = self.records()
        self.assertEqual([json.loads(l)["step"] for l in files["shop-user-1/steps.jsonl"].decode().splitlines()], [1])
        self.assertIn("shop-user-1/steps/01.png", files)
        self.assertIn("shop-user-1/trace.zip", files)  # what the browser managed to save before it died
        self.assertNotIn("shop-user-1/video.webm", files)

    def test_no_token_appears_in_the_records(self):
        page = FakePage(snapshot=f"- heading \"Shop\"\n- text \"session {TOKEN}\"")
        U.main(ScriptedModel([verdict(), verdict("fail", f"saw {TOKEN} on the page"), verdict()]), page)
        files = self.records()
        self.assertTrue(any(k.endswith("stream.jsonl") for k in files))
        for name, data in files.items():
            with self.subTest(file=name):
                self.assertNotIn(TOKEN.encode(), data)
        # and not in what the runner reads either
        for b in self.gitea.bodies("dark-records/s1", 1):
            self.assertNotIn(TOKEN, b)

    def test_a_task_with_one_inconclusive_step_reports_overall_inconclusive(self):
        rc = U.main(ScriptedModel([verdict(), verdict("inconclusive", "the cart was empty"), verdict()]),
                    FakePage())
        self.assertEqual(rc, 1)
        body, tag = self.done_tag()
        self.assertEqual(tag["outcome"], "inconclusive")
        self.assertEqual((tag["steps_ok"], tag["steps_total"]), (2, 3))
        self.assertIn("inconclusive", body)
        files = self.records()
        verdicts = [json.loads(l)["verdict"] for l in files["shop-user-1/steps.jsonl"].decode().splitlines()]
        self.assertEqual(verdicts, ["pass", "inconclusive", "pass"])

    def test_one_failed_step_fails_the_run_with_the_steps_kind(self):
        rc = U.main(ScriptedModel([verdict(), verdict("fail", "the sign-in button does nothing"), verdict()]),
                    FakePage())
        self.assertEqual(rc, 1)
        body, tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["steps_ok"], tag["steps_total"]), ("fail", "steps", 2, 3))
        self.assertIn("step 2: the sign-in button does nothing", body)
        self.assertEqual(spec.FAIL_KIND_OUTCOME["steps"], "fail:capability")
        self.assertIn("shop-user-1/steps/03.png", self.records())

    def test_a_browser_that_cannot_open_the_url_is_the_environment(self):
        class Unreachable(FakePage):
            def goto(self, url):
                raise U.BrowserError(f"{url} did not open: net::ERR_CONNECTION_REFUSED")
        self.assertEqual(U.main(ScriptedModel([]), Unreachable()), 1)
        _, tag = self.done_tag()
        self.assertEqual(tag["kind"], "env")

    def test_a_task_without_steps_is_refused(self):
        S.TASK["steps"] = []
        self.assertEqual(U.main(ScriptedModel([]), FakePage()), 1)
        self.assertEqual(self.done_tag()[1]["kind"], "env")


class ReportingRunner(R.Runner):
    """launch() does what the sandbox would: it answers on the issue with
    the AGENT-DONE the test scripts, instead of running a browser."""

    def __init__(self, *a, report=None, **kw):
        kw.setdefault("hold", lambda s: (True, ""))
        kw.setdefault("meter", _NoMeter)
        super().__init__(*a, **kw)
        self.report = report
        self.spawned = []

    def launch(self, vmid, name, files, runcmd, **spawn_kw):
        self.spawned.append({"files": files, "runcmd": runcmd, "kw": spawn_kw})
        task = json.loads(files["/opt/task.json"][0])
        self.gitea.comment(task["repo"], task["issue"], self.report)

    def net_ip(self, vmid):
        return "192.0.2.9"

    def reap(self, vmid, name):
        return True


class _NoMeter:
    def start(self):
        return self

    def stop(self):
        return {}


def report(outcome, kind=None, steps_ok=3, steps_total=3):
    kw = {"kind": kind} if kind else {}
    tag = {"v": 2, "ev": "done", "outcome": outcome, "calls": 5, "tokens_in": 100, "tokens_out": 20,
           "reasoning_chars": 0, "seconds": 30, "requests": 5, "tool_calls": 2,
           "steps_ok": steps_ok, "steps_total": steps_total, "records": "dark-records/s1/x", **kw}
    head = "AGENT-DONE ok" if outcome == "ok" else f"AGENT-DONE fail ({kind}): step 2: no button"
    return f"{head}\nDARK:{json.dumps(tag, sort_keys=True)}"


class Driver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        budgets = BUDGETS.replace("poll_seconds = 1", "poll_seconds = 0.05")
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, budgets))
        self.host = config.Host(gitea_url=self.fake.url, gitea_lan_url=self.fake.url,
                                state_dir=os.path.join(self.tmp, "state"), records_org="dark-records",
                                admin_token="good", agent_token=TOKEN)
        os.makedirs(self.host.abort_dir)
        self.led = L.Ledger(self.host.ledger_path)
        self.task = tasks.load_task(make_user_task(os.path.join(self.tmp, "bench")))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_user(self, body, **kw):
        r = ReportingRunner(self.cat, self.bud, self.host, self.led, G.Gitea(self.fake.url, "good"), None,
                            log=lambda *a: None, shift="s1", report=body)
        return r, r.user(self.task, "cloud-x", **kw)

    def test_every_step_pass_is_a_pass(self):
        r, res = self.run_user(report("ok"))
        self.assertEqual((res.outcome, res.checks_ok, res.checks_total), ("pass", 3, 3))
        end = self.led.last("run.end")
        self.assertEqual((end["outcome"], end["cls"], end["arm"], end["repo"]),
                         ("pass", "user", "user", "dark-records/s1"))
        self.assertEqual(res.transitions[-1], ("executing", "pass"))
        self.assertNotIn("verifying", [to for _, to in res.transitions])
        # the sandbox: the executor beside session.py, as a user sandbox
        sent = r.spawned[0]
        self.assertEqual(sorted(sent["files"]), ["/opt/session.py", "/opt/task.json", "/opt/user.py"])
        self.assertIn("python3 /opt/user.py", sent["runcmd"][0][-1])
        self.assertEqual(sent["kw"], {"cls": "user"})

    def test_a_failed_step_is_a_capability_failure(self):
        _, res = self.run_user(report("fail", "steps", steps_ok=2))
        self.assertEqual((res.outcome, res.fail_kind, res.checks_ok, res.checks_total),
                         ("fail:capability", "steps", 2, 3))
        self.assertIn("no button", res.detail)

    def test_a_browser_that_did_not_start_is_structural(self):
        _, res = self.run_user(report("fail", "env", steps_ok=0))
        self.assertEqual((res.outcome, self.led.last("run.end")["reason"]),
                         ("fail:structural", spec.STRUCTURAL_REASONS["env"]))

    def test_a_docker_user_sandbox_gets_the_browser_image(self):
        self.host.backend = "docker"
        r, _ = self.run_user(report("ok"))
        self.assertEqual(r.spawned[0]["kw"], {"cls": "user", "image": "dark-sandbox-browser"})

    def test_an_exec_task_is_refused(self):
        from tests.test_tasks import make_task
        t = tasks.load_task(make_task(os.path.join(self.tmp, "bench"), "hello"))
        r = ReportingRunner(self.cat, self.bud, self.host, self.led, G.Gitea(self.fake.url, "good"), None,
                            log=lambda *a: None, shift="s1", report="")
        self.assertEqual(r.user(t, "cloud-x").outcome, "refused")
        self.assertEqual(r.spawned, [])

    def test_the_envelope_is_the_user_class_one(self):
        self.run_user(report("ok"))
        start = self.led.last("run.start")
        env = budget.envelope(self.bud, self.led, "user", "cloud-x")
        self.assertEqual((start["cls"], start["envelope"]["calls"]), ("user", env.calls))


if __name__ == "__main__":
    unittest.main()

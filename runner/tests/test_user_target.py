"""dark/user.py's resolve_target: an action names one element. Playwright's
`name` matches a substring, so `.first` of it clicked the link
`records: 1032-x` (a commit message) when the model named the link `1032-x`
(the run's folder). The exact name now wins, and several partial matches with
no exact one are refused with their names. First over a fake page that
matches names the way Playwright documents it, then, where Playwright and its
Chromium are installed, over a real page."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dark import session as S
from dark import user as U
from tests.test_user import TOKEN, FakePage, ScriptedModel, reset_session, verdict

FIXTURE = [("link", "records: 1032-x"), ("link", "1032-x"), ("link", "records: 1032-xy"),
           ("button", "Play"), ("button", "Play next"), ("button", "Play next")]


def _norm(s):
    return " ".join(s.split())


class FakeLocator:
    """Playwright's Locator as resolve_target uses it, over a list of
    elements: count, first, nth, wait_for, aria_snapshot."""

    def __init__(self, els):
        self.els = els

    def count(self):
        return len(self.els)

    @property
    def first(self):
        return self.nth(0)

    def nth(self, i):
        return FakeLocator(self.els[i:i + 1])

    def wait_for(self, state=None, timeout=None):
        if not self.els:
            raise TimeoutError("Locator.wait_for: Timeout exceeded")

    def aria_snapshot(self):
        role, name = self.els[0]
        line = f'{role} "{name}"'
        return f"- '{line}':" if ": " in line else f"- {line}"


class FakeRolePage:
    """get_by_role the way Playwright documents it: `name` is a substring,
    case ignored, whitespace collapsed; `exact=True` is the whole name, case
    kept."""

    def __init__(self, els):
        self.els = els

    def get_by_role(self, role, name=None, exact=False):
        def hit(n):
            if name is None:
                return True
            return _norm(n) == _norm(name) if exact else _norm(name).lower() in _norm(n).lower()
        return FakeLocator([(r, n) for r, n in self.els if r == role and hit(n)])


class Resolve(unittest.TestCase):
    def setUp(self):
        self.page = FakeRolePage(FIXTURE)

    def pick(self, role, name):
        return U.resolve_target(self.page, role, name).els

    def test_the_exact_name_wins_over_an_earlier_partial_match(self):
        self.assertEqual(self.pick("link", "1032-x"), [("link", "1032-x")])
        self.assertEqual(self.pick("link", "records: 1032-x"), [("link", "records: 1032-x")])

    def test_one_partial_match_is_taken(self):
        self.assertEqual(self.pick("link", "1032-xy"), [("link", "records: 1032-xy")])
        self.assertEqual(self.pick("link", "RECORDS: 1032-XY"), [("link", "records: 1032-xy")])

    def test_several_partial_matches_and_no_exact_one_are_refused_with_their_names(self):
        with self.assertRaises(U.AmbiguousTarget) as cm:
            U.resolve_target(self.page, "link", "1032")
        msg = str(cm.exception)
        self.assertIn('refused: 3 link elements contain "1032"', msg)
        for name in ("records: 1032-x", "1032-x", "records: 1032-xy"):
            self.assertIn(json.dumps(name), msg)

    def test_several_exact_matches_take_the_first(self):
        self.assertEqual(self.pick("button", "Play next"), [("button", "Play next")])
        self.assertEqual(self.pick("button", "Play"), [("button", "Play")])

    def test_no_name_is_the_first_of_the_role_as_before(self):
        self.assertEqual(self.pick("link", None), [("link", "records: 1032-x")])

    def test_no_match_waits_and_times_out(self):
        with self.assertRaises(TimeoutError):
            U.resolve_target(self.page, "link", "nothing")

    def test_a_long_candidate_list_is_cut_and_counted(self):
        page = FakeRolePage([("link", f"run {i}") for i in range(12)])
        with self.assertRaises(U.AmbiguousTarget) as cm:
            U.resolve_target(page, "link", "run")
        self.assertIn('"run 7"', str(cm.exception))
        self.assertNotIn('"run 8"', str(cm.exception))
        self.assertIn("and 4 more", str(cm.exception))


class RefusingPage(FakePage):
    """A page whose first click is ambiguous, as PlaywrightPage raises it."""

    def act(self, action):
        if action.get("name") == "1032":
            raise U.AmbiguousTarget('refused: 2 link elements contain "1032" and none is named '
                                    'exactly that: "records: 1032-x", "1032-x"')
        super().act(action)


class RefusedAction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_session(self.tmp, {"token": TOKEN})
        self.records = S.RECORDS_DIR

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_refused_action_is_shown_to_the_model_and_marked_in_the_trail(self):
        model = ScriptedModel([{"do": "click", "role": "link", "name": "1032"},
                               {"do": "click", "role": "link", "name": "1032-x"},
                               verdict()])
        page = RefusingPage()
        U.run_steps(model, page, "https://app.example.test/", ["Open the run"], self.records, 10, 1e12)
        # nothing was clicked for the refused action; the precise one went through
        self.assertEqual([a for kind, a in page.done if kind == "act"],
                         [{"do": "click", "role": "link", "name": "1032-x"}])
        seen = model.asked[1]["history"][0]
        self.assertTrue(seen["refused"])
        self.assertIn('"records: 1032-x", "1032-x"', seen["error"])
        with open(os.path.join(self.records, "steps", "01.trail.jsonl")) as f:
            trail = [json.loads(line) for line in f]
        self.assertEqual([t.get("refused", False) for t in trail], [True, False])


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


PAGES = {
    "/": ('<a href="/commit/bd78">records: 1032-x</a> <a href="/src/1032-x">1032-x</a> '
          '<a href="/commit/aa11">records: 1032-xy</a>'),
    "/commit/bd78": "<h1>commit</h1>", "/src/1032-x": "<h1>folder</h1>", "/commit/aa11": "<h1>other</h1>",
}


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<!doctype html><title>t</title>{PAGES.get(self.path, 'missing')}".encode()
        self.send_response(200 if self.path in PAGES else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(_playwright(), "playwright not installed")
class RealBrowser(unittest.TestCase):
    """The fixture page in Playwright's own Chromium, through PlaywrightPage."""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.page = U.PlaywrightPage()
        self.page.goto(self.base + "/")

    def tearDown(self):
        self.page.close()
        self.srv.shutdown()

    def test_the_named_folder_opens_not_the_commit_listed_first(self):
        self.page.act({"do": "click", "role": "link", "name": "1032-x"})
        self.assertEqual(self.page.url(), self.base + "/src/1032-x")

    def test_an_ambiguous_name_clicks_nothing(self):
        with self.assertRaises(U.AmbiguousTarget) as cm:
            self.page.act({"do": "click", "role": "link", "name": "1032"})
        self.assertIn('"records: 1032-x", "1032-x", "records: 1032-xy"', str(cm.exception))
        self.assertEqual(self.page.url(), self.base + "/")


if __name__ == "__main__":
    unittest.main()

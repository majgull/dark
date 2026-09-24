"""dark/session.py: the numbers the session arm reports come from pi's own
stream, counted as it arrives.

The fixture is a recorded pi session asked to write a file and read it back:
three model calls, two tool calls, and the token counts the provider returned.
The counts come from the events themselves, so a refused call is not confused
with a lost record.
"""

import io
import json
import os
import shutil
import tempfile
import unittest

from dark import session
from dark import spec
from tests import fakes

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pi-stream-tools.jsonl")


class FakeProc:
    """Enough of Popen for read_stream: a line iterator and a kill flag."""

    def __init__(self, lines):
        self.stdout = io.StringIO("".join(lines))
        self.killed = False

    def kill(self):
        self.killed = True


def fixture_lines():
    with open(FIX) as f:
        return f.readlines()


class Counting(unittest.TestCase):
    def setUp(self):
        for k in session.STATS:
            session.STATS[k] = 0
        session.PROGRESS.cid = None          # no issue to PATCH in a test
        session.MAX_CALLS = 99
        session.TASK.setdefault("token", "")

    def run_fixture(self, lines=None, deadline=1e12):
        p = FakeProc(lines if lines is not None else fixture_lines())
        killed, tail = session.read_stream(p, deadline)
        return p, killed, tail

    def test_the_recorded_run_counts_three_model_calls_and_two_tool_calls(self):
        p, killed, _ = self.run_fixture()
        self.assertIsNone(killed)
        self.assertFalse(p.killed)
        self.assertEqual(session.STATS["calls"], 3)
        self.assertEqual(session.STATS["tool_calls"], 2)

    def test_tool_calls_asked_and_tool_calls_run_are_counted_apart(self):
        self.run_fixture()
        # nothing refuses a call in this arm, so they agree; they are counted
        # separately so that a run where they do not can say so
        self.assertEqual(session.STATS["asked"], session.STATS["tool_calls"])

    def test_tokens_and_thinking_are_the_providers_own_numbers(self):
        self.run_fixture()
        evs = [json.loads(l) for l in fixture_lines() if l.strip()]
        want_in = sum(int(((e.get("message") or {}).get("usage") or {}).get("input") or 0)
                      for e in evs if e["type"] == "message_end")
        want_out = sum(int(((e.get("message") or {}).get("usage") or {}).get("output") or 0)
                       for e in evs if e["type"] == "message_end")
        self.assertEqual(session.STATS["tokens_in"], want_in)
        self.assertEqual(session.STATS["tokens_out"], want_out)
        self.assertGreater(want_in, 0)
        self.assertEqual(session.STATS["reasoning_chars"], 39)

    def test_a_session_over_the_call_envelope_is_killed_on_the_call_that_passes_it(self):
        session.MAX_CALLS = 2
        p, killed, _ = self.run_fixture()
        self.assertEqual(killed, "calls")
        self.assertTrue(p.killed)
        self.assertEqual(session.STATS["calls"], 3)   # the one that passed the cap

    def test_a_session_over_the_wall_deadline_is_killed(self):
        p, killed, _ = self.run_fixture(deadline=0)
        self.assertEqual(killed, "seconds")
        self.assertTrue(p.killed)

    def test_a_line_that_is_not_json_is_skipped_not_fatal(self):
        lines = ["not json at all\n", *fixture_lines()]
        _, killed, _ = self.run_fixture(lines)
        self.assertIsNone(killed)
        self.assertEqual(session.STATS["calls"], 3)

    def test_the_tail_keeps_the_end_of_the_stream_for_a_failure_report(self):
        _, _, tail = self.run_fixture()
        self.assertTrue(tail)
        self.assertIn("agent_end", tail)

    def test_the_stream_file_matches_every_line_pi_emitted(self):
        path = os.path.join(tempfile.mkdtemp(), "stream.jsonl")
        p = FakeProc(fixture_lines())
        killed, _ = session.read_stream(p, 1e12, path)
        self.assertIsNone(killed)
        with open(path) as f:
            written = f.read()
        self.assertEqual(written, "".join(fixture_lines()))

    def test_a_kill_at_the_envelope_keeps_the_stream_up_to_the_kill(self):
        # a kill loses nothing already emitted: the file is a prefix of the
        # full stream, not empty and not the whole thing
        path = os.path.join(tempfile.mkdtemp(), "stream.jsonl")
        session.MAX_CALLS = 2
        p = FakeProc(fixture_lines())
        killed, _ = session.read_stream(p, 1e12, path)
        self.assertEqual(killed, "calls")
        with open(path) as f:
            written = f.read()
        self.assertTrue(written)
        self.assertTrue("".join(fixture_lines()).startswith(written))


class Config(unittest.TestCase):
    def test_models_json_names_the_endpoint_and_carries_no_credential(self):
        import tempfile
        home = tempfile.mkdtemp()
        session.TASK.update({"llm_model": "deepseek-v4-flash:cloud",
                             "llm_url": "http://localhost:11434/v1",
                             "think_api": "reasoning_effort", "ctx": 128000,
                             "max_tokens": 8192, "thinking_tokens": 1})
        session.write_models_json(home)
        with open(os.path.join(home, ".pi", "agent", "models.json")) as f:
            doc = json.load(f)
        prov = doc["providers"]["dark"]
        self.assertEqual(prov["baseUrl"], "http://localhost:11434/v1")
        self.assertEqual(prov["apiKey"], "unused")   # the server ignores it
        self.assertEqual(prov["models"][0]["id"], "deepseek-v4-flash:cloud")
        self.assertTrue(prov["compat"]["supportsReasoningEffort"])
        self.assertFalse(prov["compat"]["supportsDeveloperRole"])

    def test_a_provider_that_ignores_the_level_is_told_so(self):
        import tempfile
        home = tempfile.mkdtemp()
        session.TASK.update({"llm_model": "m", "llm_url": "u", "think_api": "none"})
        session.write_models_json(home)
        with open(os.path.join(home, ".pi", "agent", "models.json")) as f:
            doc = json.load(f)
        self.assertFalse(doc["providers"]["dark"]["compat"]["supportsReasoningEffort"])

    def test_darks_levels_map_onto_the_ones_pi_understands(self):
        self.assertEqual(session.THINK["none"], "off")
        self.assertEqual([session.THINK[k] for k in ("low", "medium", "high")],
                         ["low", "medium", "high"])

    def test_the_done_tag_carries_every_field_the_runner_requires(self):
        for k in session.STATS:
            session.STATS[k] = 1
        tag = json.loads(session.done("ok", branch="run/x")[len("DARK:"):])
        self.assertTrue(spec.tag_ok("done", tag))
        self.assertEqual(tag["tool_calls"], 1)
        req, opt = spec.AGENT_TAGS["done"]
        self.assertEqual(sorted(set(tag) - {"v", "ev"} - set(req) - set(opt)), [])

    def test_the_done_tag_carries_the_records_path_and_sha(self):
        tag = json.loads(session.done("ok", records="dark-records/s1/review-x-1",
                                      records_sha256="deadbeef")[len("DARK:"):])
        self.assertTrue(spec.tag_ok("done", tag))
        self.assertEqual(tag["records"], "dark-records/s1/review-x-1")
        self.assertEqual(tag["records_sha256"], "deadbeef")


class RecordsPush(unittest.TestCase):
    """dark/session.py's own push of the kept stream: a
    fail() or the ok path always calls records_kw(), and a push failure is
    reported, never raised: the run's outcome is decided before this runs
    and stays what it was."""

    def setUp(self):
        for k in session.STATS:
            session.STATS[k] = 0
        session.TASK.clear()
        session.TASK.update({"token": "tok", "spec": "review this"})

    def test_no_records_repo_fails_fast_without_touching_the_network(self):
        ok, info = session.push_records({})
        self.assertFalse(ok)
        self.assertIn("no records_repo", info)

    def test_records_kw_reports_push_failed_and_leaves_stats_untouched(self):
        before = dict(session.STATS)
        kw = session.records_kw()
        self.assertTrue(kw["records"].startswith("PUSH FAILED"))
        self.assertNotIn("records_sha256", kw)
        self.assertEqual(session.STATS, before)


class RecordsPushLive(unittest.TestCase):
    """push_records() against a real (local, file://) bare repo: the same
    git plumbing a records repo on Gitea gets, minus the network hop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = fakes.make_origin(self.tmp, "dark-records/s1", {"README.md": "seed\n"})
        session.TASK.clear()
        session.TASK.update({"git_url": self.base, "records_repo": "dark-records/s1",
                             "token": "tok", "spec": "brief text", "run": "run-1"})
        session.RUN_ID = "run-1"
        session.SPEC_TEXT = "brief text"
        session.RECORDS_WORK = os.path.join(self.tmp, "records-work")
        session.STREAM_PATH = os.path.join(self.tmp, "stream.jsonl")
        with open(session.STREAM_PATH, "w") as f:
            f.write('{"type": "message_start"}\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stream_brief_and_task_json_land_under_the_run_id(self):
        ok, path = session.push_records({"report.md": "the findings"})
        self.assertTrue(ok, path)
        self.assertEqual(path, "dark-records/s1/run-1")
        files = fakes.branch_files(self.tmp, "dark-records/s1", "main")
        self.assertEqual(files, {"README.md", "run-1/stream.jsonl", "run-1/brief.md",
                                 "run-1/task.json", "run-1/report.md"})
        self.assertEqual(fakes.branch_file(self.tmp, "dark-records/s1", "main", "run-1/brief.md"),
                         "brief text")
        self.assertNotIn("tok", fakes.branch_file(self.tmp, "dark-records/s1", "main", "run-1/task.json"))

    def test_a_second_runs_records_do_not_clobber_the_first(self):
        session.push_records({})
        session.RUN_ID = "run-2"
        ok, path = session.push_records({})
        self.assertTrue(ok, path)
        files = fakes.branch_files(self.tmp, "dark-records/s1", "main")
        self.assertTrue({"run-1/stream.jsonl", "run-2/stream.jsonl"} <= files)


class ReviewMode(unittest.TestCase):
    """No hidden acceptance, just report.md landed
    non-empty. read_report()/review_outcome() are the decision the runner
    reads back as "delivered" (dark/run.py's ReviewMode tests fake the rest
    of the executor around this)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        session.WORK = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_report_file_reads_as_empty(self):
        self.assertEqual(session.read_report(), "")
        self.assertEqual(session.review_outcome(session.read_report()), (False, "no_report", "report.md missing or empty"))

    def test_a_blank_report_is_not_delivered(self):
        with open(os.path.join(self.tmp, "report.md"), "w") as f:
            f.write("   \n")
        ok, kind, _ = session.review_outcome(session.read_report())
        self.assertEqual((ok, kind), (False, "no_report"))

    def test_a_nonempty_report_is_read_back_whole_and_delivered(self):
        with open(os.path.join(self.tmp, "report.md"), "w") as f:
            f.write("# findings\nsomething\n")
        report = session.read_report()
        self.assertEqual(report, "# findings\nsomething\n")
        self.assertEqual(session.review_outcome(report), (True, None, ""))

    def test_the_default_mode_is_task_not_review(self):
        session.TASK.clear()
        self.assertEqual(session.TASK.get("mode", "task"), "task")


if __name__ == "__main__":
    unittest.main()

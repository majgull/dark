"""dark/session.py: the numbers the session arm reports come from pi's own
stream, counted as it arrives.

The fixture is a recorded pi session asked to write a file and read it back:
three model calls, two tool calls, and the token counts the provider returned.
The counts come from the events themselves, so a refused call is not confused
with a lost record.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from dark import session
from dark import spec
from tests import fakes
from tests.test_run import Base, LocalRunner

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pi-stream-tools.jsonl")
CLAUDE_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "claude-stream-tools.jsonl")
FAKE_CLAUDE_BIN = os.path.join(os.path.dirname(__file__), "fixtures", "fake-claude-bin")


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


def claude_fixture_lines():
    with open(CLAUDE_FIX) as f:
        return f.readlines()


class CountingClaude(unittest.TestCase):
    """The same counting Counting proves for pi's stream, proven again for
    Claude Code's own stream-json shape: read_claude_stream() must produce
    identical STATS from a different wire format, so the runner cannot tell
    the two brains apart from the outside (dark/run.py's ledger row is the
    same either way)."""

    def setUp(self):
        for k in session.STATS:
            session.STATS[k] = 0
        session.PROGRESS.cid = None
        session.MAX_CALLS = 99
        session.TASK.setdefault("token", "")

    def run_fixture(self, lines=None, deadline=1e12):
        p = FakeProc(lines if lines is not None else claude_fixture_lines())
        killed, tail = session.read_claude_stream(p, deadline)
        return p, killed, tail

    def test_the_recorded_run_counts_three_calls_and_two_tool_results(self):
        p, killed, _ = self.run_fixture()
        self.assertIsNone(killed)
        self.assertFalse(p.killed)
        self.assertEqual(session.STATS["calls"], 3)
        self.assertEqual(session.STATS["tool_calls"], 2)

    def test_tool_calls_asked_and_tool_calls_run_are_counted_apart(self):
        self.run_fixture()
        # nothing refuses a call in this arm (--dangerously-skip-permissions),
        # so they agree; counted separately so a run where they do not can say so
        self.assertEqual(session.STATS["asked"], session.STATS["tool_calls"])

    def test_tokens_and_thinking_are_the_providers_own_numbers(self):
        self.run_fixture()
        evs = [json.loads(l) for l in claude_fixture_lines() if l.strip()]
        want_in, want_out, want_reasoning = 0, 0, 0
        for e in evs:
            if e["type"] != "assistant":
                continue
            u = (e["message"].get("usage") or {})
            want_in += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
                + (u.get("cache_read_input_tokens") or 0)
            want_out += u.get("output_tokens") or 0
            for c in e["message"].get("content") or []:
                if c.get("type") == "thinking":
                    want_reasoning += len(c.get("thinking") or "")
        self.assertEqual(session.STATS["tokens_in"], want_in)
        self.assertEqual(session.STATS["tokens_out"], want_out)
        self.assertGreater(want_in, 0)
        self.assertEqual(session.STATS["reasoning_chars"], want_reasoning)
        self.assertGreater(want_reasoning, 0)

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
        lines = ["not json at all\n", *claude_fixture_lines()]
        _, killed, _ = self.run_fixture(lines)
        self.assertIsNone(killed)
        self.assertEqual(session.STATS["calls"], 3)

    def test_the_stream_file_matches_every_line_claude_emitted(self):
        path = os.path.join(tempfile.mkdtemp(), "stream.jsonl")
        p = FakeProc(claude_fixture_lines())
        killed, _ = session.read_claude_stream(p, 1e12, path)
        self.assertIsNone(killed)
        with open(path) as f:
            written = f.read()
        self.assertEqual(written, "".join(claude_fixture_lines()))

    def test_a_kill_at_the_envelope_keeps_the_stream_up_to_the_kill(self):
        path = os.path.join(tempfile.mkdtemp(), "stream.jsonl")
        session.MAX_CALLS = 2
        p = FakeProc(claude_fixture_lines())
        killed, _ = session.read_claude_stream(p, 1e12, path)
        self.assertEqual(killed, "calls")
        with open(path) as f:
            written = f.read()
        self.assertTrue(written)
        self.assertTrue("".join(claude_fixture_lines()).startswith(written))


class ClaudeModelId(unittest.TestCase):
    def setUp(self):
        session.TASK.clear()

    def test_a_claude_prefixed_tier_names_the_model_after_the_colon(self):
        session.TASK["llm_model"] = "claude:claude-sonnet-5"
        self.assertEqual(session.claude_model_id(), "claude-sonnet-5")

    def test_any_other_tier_is_not_a_claude_tier(self):
        session.TASK["llm_model"] = "deepseek-v4-flash:cloud"
        self.assertIsNone(session.claude_model_id())

    def test_no_llm_model_at_all_is_not_a_claude_tier(self):
        self.assertIsNone(session.claude_model_id())


class ClaudeSubprocess(unittest.TestCase):
    """run_claude() against the fake `claude` script on PATH: a real
    subprocess and a real stream-json reply, counted into STATS."""

    def setUp(self):
        for k in session.STATS:
            session.STATS[k] = 0
        session.PROGRESS.cid = None
        session.MAX_CALLS = 99
        session.TASK.clear()
        session.TASK.update({"token": "", "llm_model": "claude:claude-sonnet-5"})
        self.tmp = tempfile.mkdtemp()
        self.saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = FAKE_CLAUDE_BIN + os.pathsep + self.saved_path
        os.environ["FAKE_CLAUDE_STREAM"] = CLAUDE_FIX

    def tearDown(self):
        os.environ["PATH"] = self.saved_path
        os.environ.pop("FAKE_CLAUDE_STREAM", None)
        session.PROGRESS.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_fake_claude_scripts_reply_is_counted_like_any_other(self):
        stream_path = os.path.join(self.tmp, "stream.jsonl")
        killed_for, tail, err, rc = session.run_claude("claude-sonnet-5", 1e12, stream_path, work=self.tmp)
        self.assertIsNone(killed_for, (tail, err))
        self.assertEqual(rc, 0, err)
        self.assertEqual(session.STATS["calls"], 3)
        self.assertEqual(session.STATS["tool_calls"], 2)

    def test_one_call_split_over_lines_counts_once_and_the_result_line_sets_the_totals(self):
        # the shape of a real run: one line per content block of one call,
        # each with the usage the call started with, totals only at the end
        start = {"input_tokens": 3, "cache_read_input_tokens": 100, "output_tokens": 2}
        lines = [
            {"type": "assistant", "message": {"id": "msg_a", "usage": start,
                                              "content": [{"type": "thinking", "thinking": "hm"}]}},
            {"type": "assistant", "message": {"id": "msg_a", "usage": start,
                                              "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
            {"type": "assistant", "message": {"id": "msg_b", "usage": start,
                                              "content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "usage": {"input_tokens": 6, "cache_creation_input_tokens": 50,
                                         "cache_read_input_tokens": 200, "output_tokens": 700}},
        ]
        fix = os.path.join(self.tmp, "split.jsonl")
        with open(fix, "w") as f:
            f.write("".join(json.dumps(l) + "\n" for l in lines))
        os.environ["FAKE_CLAUDE_STREAM"] = fix
        session.run_claude("claude-sonnet-5", 1e12, None, work=self.tmp)
        self.assertEqual(session.STATS["calls"], 2)
        self.assertEqual(session.STATS["tool_calls"], 1)
        self.assertEqual(session.STATS["tokens_in"], 256)
        self.assertEqual(session.STATS["tokens_out"], 700)


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

    def test_a_run_that_failed_before_pi_started_still_gets_its_records(self):
        # a clone that fails ends in fail("env"), which pushes records before
        # any stream exists; the sum must not raise and turn it into a crash
        os.unlink(session.STREAM_PATH)
        kw = session.records_kw()
        self.assertEqual(kw["records"], "dark-records/s1/run-1")
        self.assertEqual(kw["records_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(fakes.branch_file(self.tmp, "dark-records/s1", "main", "run-1/stream.jsonl"), "")

    def test_a_crash_names_the_missing_path(self):
        e = FileNotFoundError(2, "No such file or directory", "/opt/records/stream.jsonl")
        self.assertEqual(repr(e), "FileNotFoundError(2, 'No such file or directory')")
        self.assertIn("(file: /opt/records/stream.jsonl)", session.crash_text(e))
        self.assertNotIn("(file:", session.crash_text(ValueError("x")))

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


class SeveralRepositories(unittest.TestCase):
    """A task with `repos`: each is cloned to /work/<name> on its base, and at
    the end HEAD of each is pushed to the delivery branch in its own origin.
    git is real, against bare origins over file://; pi is stood in for by a
    session that commits one file in each tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = fakes.make_origin(self.tmp, "org/api", {"README.md": "api\n"})
        fakes.make_origin(self.tmp, "org/web", {"README.md": "web\n"}, branch="trunk")
        self.saved = {k: getattr(session, k) for k in
                      ("MULTI_WORK", "PIHOME", "RECORDS_DIR", "STREAM_PATH", "fetch_runtime",
                       "write_models_json", "run_session", "comment")}
        session.MULTI_WORK = os.path.join(self.tmp, "work")
        session.PIHOME = os.path.join(self.tmp, "pihome")
        session.RECORDS_DIR = os.path.join(self.tmp, "records")
        session.STREAM_PATH = os.path.join(session.RECORDS_DIR, "stream.jsonl")
        session.fetch_runtime = lambda: ("node", "cli")
        session.write_models_json = lambda home: None
        self.comments = []
        session.comment = lambda body: self.comments.append(body)
        session.PROGRESS.cid = None
        for k in session.STATS:
            session.STATS[k] = 0
        session.TASK.clear()
        session.TASK.update({
            "token": "tok", "llm_model": "m", "branch": "run/r1", "run": "r1", "task": "t",
            "repos": [{"name": "api", "url": f"{self.base}/org/api.git", "base": "main"},
                      {"name": "web", "url": f"{self.base}/org/web.git", "base": "trunk"}]})

        def fake_session(node, cli, home, deadline, stream_path, review=False, work=None):
            self.work = work
            for name in ("api", "web"):
                d = os.path.join(work, name)
                with open(os.path.join(d, "change.txt"), "w") as f:
                    f.write(f"{name} changed\n")
            session.STATS["calls"] = 1
            return None, "", "", 0
        session.run_session = fake_session

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(session, k, v)
        session.PROGRESS.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def done_tag(self):
        body = [b for b in self.comments if b.startswith("AGENT-DONE")][-1]
        line = [l for l in body.splitlines() if l.startswith("DARK:")][-1]
        return json.loads(line[len("DARK:"):])

    def test_each_repository_is_cloned_on_its_base_and_pushed_to_the_delivery_branch(self):
        self.assertEqual(session._main_task(), 0, self.comments)
        self.assertEqual(self.work, session.MULTI_WORK)
        self.assertEqual(fakes.branch_files(self.tmp, "org/api", "run/r1"), {"README.md", "change.txt"})
        self.assertEqual(fakes.branch_files(self.tmp, "org/web", "run/r1"), {"README.md", "change.txt"})
        self.assertEqual(fakes.branch_file(self.tmp, "org/web", "run/r1", "README.md"), "web\n")
        # two pushes, and the bases stay where they were
        self.assertEqual(fakes.branch_files(self.tmp, "org/api", "main"), {"README.md"})
        self.assertEqual(fakes.branch_files(self.tmp, "org/web", "trunk"), {"README.md"})

    def test_the_done_tag_lists_the_branches_pushed(self):
        session._main_task()
        tag = self.done_tag()
        self.assertTrue(spec.tag_ok("done", tag))
        self.assertEqual(tag["outcome"], "ok")
        self.assertEqual(tag["branches"], [{"repo": "api", "branch": "run/r1"},
                                           {"repo": "web", "branch": "run/r1"}])

    def test_a_base_that_does_not_exist_is_an_env_failure_before_the_session(self):
        session.TASK["repos"][1]["base"] = "nope"
        self.assertEqual(session._main_task(), 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"]), ("fail", "env"))
        self.assertIsNone(fakes.branch_files(self.tmp, "org/api", "run/r1"))


class ToolSet(unittest.TestCase):
    """tools = "full" launches pi as shipped and online; the default stays
    reduced: no extensions, skills, prompt templates or context files, and
    PI_OFFLINE=1."""

    class Popen:
        seen = []

        def __init__(self, cmd, cwd=None, env=None, **kw):
            ToolSet.Popen.seen.append((cmd, env))
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def setUp(self):
        self.saved = session.subprocess.Popen
        session.subprocess.Popen = ToolSet.Popen
        ToolSet.Popen.seen = []
        session.TASK.clear()
        session.TASK.update({"token": "", "llm_model": "m"})

    def tearDown(self):
        session.subprocess.Popen = self.saved

    def argv(self, tools=None):
        if tools:
            session.TASK["tools"] = tools
        session.run_session("node", "cli", "/tmp/h", 1e12, None, work="/tmp")
        return ToolSet.Popen.seen[-1]

    REDUCED = ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files"]

    def test_the_default_is_the_reduced_tool_set_offline(self):
        cmd, env = self.argv()
        for flag in self.REDUCED:
            self.assertIn(flag, cmd)
        self.assertEqual(env["PI_OFFLINE"], "1")
        self.assertEqual(cmd[-2:-1], ["-p"])

    def test_reduced_named_is_the_same_as_the_default(self):
        self.assertEqual(self.argv("reduced")[0], self.argv()[0])

    def test_full_drops_the_four_flags_and_the_offline_switch(self):
        os.environ["PI_OFFLINE"] = "1"   # not even inherited from the VM's own env
        try:
            cmd, env = self.argv("full")
        finally:
            del os.environ["PI_OFFLINE"]
        for flag in self.REDUCED:
            self.assertNotIn(flag, cmd)
        self.assertNotIn("PI_OFFLINE", env)
        self.assertEqual(cmd[:2], ["node", "cli"])
        self.assertIn("--approve", cmd)


class ClaudeInvocation(unittest.TestCase):
    """run_claude()'s own argv and environment, against a fake Popen (no
    real process): the flags code.claude.com/docs/en/cli-reference.md shows
    together (-p at the end, after --output-format/--verbose), a placeholder
    in place of a token, and the quiet-egress env vars always set."""

    def setUp(self):
        self.saved = session.subprocess.Popen
        session.subprocess.Popen = ToolSet.Popen
        ToolSet.Popen.seen = []
        session.TASK.clear()
        session.TASK.update({"token": ""})

    def tearDown(self):
        session.subprocess.Popen = self.saved

    def test_the_argv_matches_the_documented_flag_shape(self):
        session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        cmd, env = ToolSet.Popen.seen[-1]
        self.assertEqual(cmd, ["claude", "--model", "claude-sonnet-5", "--output-format", "stream-json",
                              "--verbose", "--dangerously-skip-permissions", "-p", cmd[-1]])

    def test_claude_code_is_given_a_placeholder_never_a_token(self):
        # whatever the sandbox's environment holds under that name, Claude
        # Code starts with the placeholder: the claude gate puts the real
        # token on each request, and it is the only one that can read it
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "something-left-in-the-environment"}):
            session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        cmd, env = ToolSet.Popen.seen[-1]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], session.CLAUDE_TOKEN_PLACEHOLDER)
        self.assertEqual(session.CLAUDE_TOKEN_PLACEHOLDER, "held-by-the-claude-gate")
        self.assertNotIn("something-left-in-the-environment", " ".join(cmd) + " ".join(env.values()))

    def test_the_quiet_egress_env_is_always_set(self):
        session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        _, env = ToolSet.Popen.seen[-1]
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")

    def test_root_in_the_sandbox_may_skip_permission_prompts(self):
        # a real run as root without it ended rc 1 with no model call:
        # Claude Code refuses the skip flag for root outside a sandbox
        session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        _, env = ToolSet.Popen.seen[-1]
        self.assertEqual(env["IS_SANDBOX"], "1")

    def test_a_claude_base_url_becomes_anthropic_base_url(self):
        session.TASK["claude_base_url"] = "http://claude-gate:8443"
        session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        _, env = ToolSet.Popen.seen[-1]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://claude-gate:8443")

    def test_no_claude_base_url_leaves_anthropic_base_url_unset(self):
        session.run_claude("claude-sonnet-5", 1e12, None, work="/tmp")
        _, env = ToolSet.Popen.seen[-1]
        self.assertNotIn("ANTHROPIC_BASE_URL", env)


class ToolSetOnTheLedger(Base):
    """The run.end row says which tool set a session-arm run had."""

    def session_runner(self, extra=""):
        r = self.runner([], task_kw={"extra": extra})
        r.executor = "session"
        self.sent = {}

        def launch(vmid, name, files, runcmd):
            task = json.loads(files["/opt/task.json"][0])
            self.sent = task
            r.gitea.comment(task["repo"], task["issue"], "AGENT-DONE\n" + spec.TAG_PREFIX + json.dumps(
                {"v": 2, "ev": "done", "outcome": "fail", "kind": "calls", "calls": 1, "tokens_in": 1,
                 "tokens_out": 1, "reasoning_chars": 0, "seconds": 1}))
        r.launch = launch
        return r

    def test_the_default_is_recorded_as_reduced(self):
        r = self.session_runner()
        r.run(self.task, "local-a", self.env())
        self.assertEqual(self.sent["tools"], "reduced")
        self.assertEqual(self.led.last("run.end")["tools"], "reduced")

    def test_a_full_task_is_recorded_as_full(self):
        r = self.session_runner('tools = "full"\n')
        r.run(self.task, "local-a", self.env())
        self.assertEqual(self.sent["tools"], "full")
        self.assertEqual(self.led.last("run.end")["tools"], "full")

    def test_the_pipeline_records_no_tool_set(self):
        r = self.runner([{"content": "no blocks"}] * 6)
        r.run(self.task, "local-a", self.env())
        self.assertIsNone(self.led.last("run.end")["tools"])


if __name__ == "__main__":
    unittest.main()

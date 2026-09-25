"""dark/agent.py and dark/stager.py: the executor contract.

The path guards, the reply-only staging rule and the acceptance tarball checks
are probed here; the reasoning cap and the continuation loop are probed against
the call envelope.
"""

import base64
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from dark import session, spec
from tests import fakes
from tests.test_agent import load_agent
from tests.test_run import Base

FILE_HELLO = "FILE: hello.txt\n```\nhello world\n```\n"


class PathGuards(unittest.TestCase):
    """No reply writes outside the work tree or to .git, .gitea or
    .dark/verify.sh."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent = load_agent(os.path.join(self.tmp, "none.json"), self.tmp,
                                os.path.join(self.tmp, "t.jsonl"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    ESCAPES = (
        ".git/config", "./.git/config", ".git/hooks/pre-commit", ".gitea/workflows/x.yml",
        "/etc/passwd", "//etc/passwd", "///etc/passwd", "../out.txt", "a/../../out.txt",
        "./../out.txt", ".dark/verify.sh", "./.dark/verify.sh",
        ".dark/./verify.sh", ".dark/sub/../verify.sh", ".git", ".gitea",
    )

    def test_no_escaping_path_survives_parse_files(self):
        for p in self.ESCAPES:
            with self.subTest(path=p):
                self.assertEqual(self.agent.parse_files(f"FILE: {p}\n```\nx\n```\n"), {})

    def test_no_escaping_path_survives_parse_deletes(self):
        for p in self.ESCAPES:
            with self.subTest(path=p):
                self.assertEqual(self.agent.parse_deletes(f"DELETE: {p}\n"), [])

    def test_symlinked_parent_is_refused_by_the_realpath_check(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        os.symlink("/etc", os.path.join(root, "out"))
        _, _, ref = self.agent.guard_writes({"out/passwd": "x"}, [], set(), None, root)
        self.assertEqual(ref, ["out/passwd"])
        _, _, ref = self.agent.guard_writes({}, ["out/passwd"], set(), None, root)
        self.assertEqual(ref, ["out/passwd"])


class AcceptanceTarball(unittest.TestCase):
    """The stager rejects traversal, absolute and link members in an
    acceptance tarball."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DARK_TASK"] = os.path.join(self.tmp, "none.json")
        os.environ["DARK_WORK"] = os.path.join(self.tmp, "stage")
        import importlib

        import dark.stager as stager
        self.stager = importlib.reload(stager)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _members(self, blob):
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode="r:gz") as tf:
            return tf.getmembers()

    def _bad(self, m):
        return (m.name.startswith("/") or ".." in m.name.split("/")
                or m.issym() or m.islnk())

    def test_traversal_absolute_and_link_members_are_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, kind in (("../escape.sh", tarfile.REGTYPE),
                               ("/abs.sh", tarfile.REGTYPE),
                               ("a/../../escape.sh", tarfile.REGTYPE),
                               ("link", tarfile.SYMTYPE),
                               ("hard", tarfile.LNKTYPE)):
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "/etc/passwd"
                tf.addfile(info, io.BytesIO(b""))
        blob = base64.b64encode(buf.getvalue()).decode()
        for m in self._members(blob):
            with self.subTest(name=m.name):
                self.assertTrue(self._bad(m), f"{m.name} would be extracted")


class ReasoningCap(Base):
    """"A budget failure never retries in the same shift" rests on the
    reasoning cap being an envelope, not a post-mortem: the agent's _account
    checks it once per turn, after the continuation chain has already run."""

    def test_the_cap_stops_the_agent_at_the_call_that_breaches_it(self):
        long_reasoning = [{"content": FILE_HELLO, "finish_reason": "length",
                           "reasoning": "x" * 100}] * 6
        r = self.runner(long_reasoning)
        res = r.run(self.task, "local-a", self.env(calls=6, max_reasoning_chars=50))
        self.assertEqual((res.outcome, res.fail_kind), ("fail:budget", "reasoning"))
        self.assertEqual(len(self.llm.requests), 1,
                         "the agent kept calling after the cumulative cap was already breached")
        self.assertLessEqual(res.reasoning_chars, 100)


class ContinuationCap(Base):
    """The continuation chain is counted against max_calls."""

    def test_continuations_never_exceed_the_call_envelope(self):
        r = self.runner([{"content": FILE_HELLO, "finish_reason": "length"}] * 3)
        res = r.run(self.task, "local-a", self.env(calls=3))
        self.assertEqual(len(self.llm.requests), 3)
        self.assertEqual(res.calls, 3)


class ReviewAsJudge(unittest.TestCase):
    """dark/session.py in review mode with review_branches: each branch is
    cloned into the sandbox before the session starts, and the outcome is
    read from report.md's last line. git is real, against bare origins over
    file://; pi is stood in for by a session that writes report.md."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = fakes.make_origin(self.tmp, "org/api", {"README.md": "api main\n"})
        fakes.make_origin(self.tmp, "org/web", {"README.md": "web main\n"})
        for full, text in (("org/api", "api delivered\n"), ("org/web", "web delivered\n")):
            work = os.path.join(self.tmp, "seed-" + full.replace("/", "-"))
            fakes.git("checkout", "-qb", "run/r1", cwd=work)
            with open(os.path.join(work, "README.md"), "w") as f:
                f.write(text)
            fakes.git("-c", "user.name=s", "-c", "user.email=s@x", "commit", "-qam", "work", cwd=work)
            fakes.git("push", "-q", "origin", "run/r1", cwd=work)
        self.saved = {k: getattr(session, k) for k in
                      ("WORK", "PIHOME", "RECORDS_DIR", "STREAM_PATH", "fetch_runtime",
                       "write_models_json", "run_session", "comment")}
        session.WORK = os.path.join(self.tmp, "work")
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
            "token": "tok", "llm_model": "m", "run": "r1", "mode": "review", "spec": "judge it",
            "review_files": [],
            "review_branches": [{"name": "api", "url": f"{self.base}/org/api.git", "branch": "run/r1"},
                                {"name": "web", "url": f"{self.base}/org/web.git", "branch": "run/r1"}]})
        self.report = None
        self.seen = {}

        def fake_session(node, cli, home, deadline, stream_path, review=False, work=None):
            for name in ("api", "web"):
                p = os.path.join(session.WORK, name, "README.md")
                if os.path.exists(p):
                    with open(p) as f:
                        self.seen[name] = f.read()
            if self.report is not None:
                with open(os.path.join(session.WORK, "report.md"), "w") as f:
                    f.write(self.report)
            session.STATS["calls"] = 1
            return None, "", "", 0
        session.run_session = fake_session

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(session, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def done_tag(self):
        body = [b for b in self.comments if b.startswith("AGENT-DONE")][-1]
        line = [l for l in body.splitlines() if l.startswith("DARK:")][-1]
        tag = json.loads(line[len("DARK:"):])
        self.assertTrue(spec.tag_ok("done", tag))
        return tag

    def test_each_branch_is_cloned_before_the_session_starts(self):
        self.report = "fine\nVERDICT: pass — both branches do what the brief asks\n"
        session._main()
        self.assertEqual(self.seen, {"api": "api delivered\n", "web": "web delivered\n"})

    def test_a_branch_that_cannot_be_cloned_is_an_env_failure_and_no_session(self):
        session.TASK["review_branches"][1]["branch"] = "run/missing"
        self.assertEqual(session._main(), 1)
        self.assertEqual(self.seen, {})
        self.assertEqual((self.done_tag()["outcome"], self.done_tag()["kind"]), ("fail", "env"))

    def test_verdict_pass(self):
        self.report = "# findings\nall good\nVERDICT: pass — both branches do what the brief asks\n"
        self.assertEqual(session._main(), 0)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["verdict"]), ("ok", "pass"))
        self.assertEqual(tag["verdict_reason"], "both branches do what the brief asks")

    def test_verdict_fail(self):
        self.report = "# findings\nVERDICT: fail — web drops the migration\n"
        self.assertEqual(session._main(), 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"], tag["verdict"]), ("fail", "verdict", "fail"))
        self.assertEqual(tag["verdict_reason"], "web drops the migration")

    def test_a_missing_report_is_no_verdict(self):
        self.assertEqual(session._main(), 1)
        tag = self.done_tag()
        self.assertEqual((tag["outcome"], tag["kind"]), ("fail", "no-verdict"))

    def test_a_verdict_that_is_not_the_last_line_is_no_verdict(self):
        self.report = "VERDICT: pass — fine\nand one more thought\n"
        session._main()
        self.assertEqual(self.done_tag()["kind"], "no-verdict")

    def test_malformed_verdict_lines(self):
        for line in ("VERDICT: pass", "VERDICT: maybe — unsure", "verdict: pass — lower case",
                     "VERDICT: passed — close", "**VERDICT: pass — bold**"):
            with self.subTest(line=line):
                self.assertEqual(session.review_verdict(f"text\n{line}\n")[0], None)

    def test_well_formed_verdict_lines(self):
        for line, want in (("VERDICT: pass — ok", ("pass", "ok")),
                           ("VERDICT: fail - tests missing", ("fail", "tests missing")),
                           ("VERDICT: fail: no tests", ("fail", "no tests"))):
            with self.subTest(line=line):
                self.assertEqual(session.review_verdict(f"notes\n{line}\n\n"), want)

    def test_without_branches_review_mode_is_unchanged(self):
        del session.TASK["review_branches"]
        self.report = "some findings, no verdict\n"
        self.assertEqual(session._main(), 0)
        tag = self.done_tag()
        self.assertEqual(tag["outcome"], "ok")
        self.assertNotIn("verdict", tag)


if __name__ == "__main__":
    unittest.main()

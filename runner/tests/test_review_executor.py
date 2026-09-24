"""Adversarial review, item 2 — the executor contract (dark/agent.py,
dark/stager.py).

The path guards, the reply-only staging rule and the acceptance tarball
checks are probed here as evidence for the report's NOT FOUND lines; the
reasoning cap and the continuation loop are probed for the envelope claim.
"""

import base64
import io
import os
import shutil
import tarfile
import tempfile
import unittest

from tests.test_agent import load_agent
from tests.test_run import Base

FILE_HELLO = "FILE: hello.txt\n```\nhello world\n```\n"


class PathGuards(unittest.TestCase):
    """Evidence for "no reply writes outside the work tree or to
    .git/.gitea/.dark/verify.sh"."""

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
    """Evidence for the stager's tarball path checks (dark/stager.py:107)."""

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
    """"a budget failure never retries in the same shift" rests on the
    reasoning cap being an envelope, not a post-mortem. dark/agent.py:263
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
    """Evidence: the continuation chain is counted against max_calls."""

    def test_continuations_never_exceed_the_call_envelope(self):
        r = self.runner([{"content": FILE_HELLO, "finish_reason": "length"}] * 3)
        res = r.run(self.task, "local-a", self.env(calls=3))
        self.assertEqual(len(self.llm.requests), 3)
        self.assertEqual(res.calls, 3)


if __name__ == "__main__":
    unittest.main()

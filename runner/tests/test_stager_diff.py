"""The stager's checks on what the branch adds to main: gitleaks (a
credential fails the work), diff-cover (a note, never a verdict) and the
protected-path check, which needs origin/main to exist in a
--single-branch clone."""
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import fakes, leakfix
from tests.test_stager import tar_b64

HAVE = shutil.which("gitleaks") is not None
PASS = {"run.sh": "#!/bin/bash\necho 'CHECK ok ok'\n"}


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args], cwd=cwd,
                   check=True, capture_output=True)


def origin_with_branch(base_dir, full, main_files, branch_files, branch="run/r1"):
    """A bare repo with `main` and a work branch one commit ahead of it."""
    bare = os.path.join(base_dir, f"{full}.git")
    os.makedirs(os.path.dirname(bare), exist_ok=True)
    git("init", "-q", "--bare", "-b", "main", bare, cwd=base_dir)
    work = os.path.join(base_dir, "seed")
    git("clone", "-q", bare, work, cwd=base_dir)
    for files in (main_files, branch_files):
        for path, content in files.items():
            os.makedirs(os.path.dirname(os.path.join(work, path)) or work, exist_ok=True)
            with open(os.path.join(work, path), "w") as f:
                f.write(content)
        git("add", "-A", cwd=work)
        git("commit", "-qm", "seed", cwd=work)
        if files is main_files:
            git("push", "-q", "origin", "HEAD:main", cwd=work)
    git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)
    return f"file://{base_dir}"


class StagerDiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gitea = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.gitea.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.full = "dark/t-diff"
        self.gitea.issues[self.full] = {1: {"title": "run", "body": "", "state": "open", "comments": []}}
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ["PATH"] = self.path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stage(self, branch_files, main_files=None, acceptance=PASS):
        main_files = main_files or {"hello.txt": "hello\n"}
        git_url = origin_with_branch(os.path.join(self.tmp, "repos"), self.full, main_files, branch_files)
        task = {"gitea": self.gitea.url, "git_url": git_url, "repo": self.full, "issue": 1,
                "branch": "run/r1", "token": "agent-tok", "timeout": 20, "run": "r1",
                "acceptance_tar_b64": tar_b64(acceptance)}
        tpath = os.path.join(self.tmp, "task.json")
        with open(tpath, "w") as f:
            json.dump(task, f)
        os.environ["DARK_TASK"] = tpath
        os.environ["DARK_WORK"] = os.path.join(self.tmp, "stage")
        import dark.stager as stager
        stager = importlib.reload(stager)
        rc = stager.main()
        bodies = [b for b in self.gitea.bodies(self.full, 1) if b.startswith("STAGE-DONE")]
        self.assertEqual(len(bodies), 1, bodies)
        line = [ln for ln in bodies[0].splitlines() if ln.startswith("DARK:")][-1]
        return rc, json.loads(line[len("DARK:"):]), bodies[0]

    def without_gitleaks(self):
        keep = [d for d in self.path.split(os.pathsep)
                if not os.path.exists(os.path.join(d, "gitleaks"))]
        os.environ["PATH"] = os.pathsep.join(keep)

    def fake_tool(self, name, script):
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(script)
        os.chmod(p, 0o755)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

    def test_protected_path_change_is_caught(self):
        # before the refspec fix this passed: origin/main never existed in the clone
        self.without_gitleaks()
        rc, tag, _ = self.stage({".dark/verify.sh": "#!/bin/bash\nexit 0\n"})
        self.assertEqual(rc, 1)
        self.assertTrue(tag["env"])
        self.assertIn("protected paths changed", tag["detail"])

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_credential_on_the_branch_fails_the_work(self):
        rc, tag, body = self.stage({"deploy.sh": leakfix.sidekiq_line()})
        self.assertEqual(rc, 1)
        self.assertFalse(tag["ok"])
        self.assertFalse(tag["env"])  # the work's fault: fail:capability, not STAGE-ENV
        self.assertIn("LEAK", tag["detail"])
        self.assertIn("deploy.sh", body)
        self.assertIn(leakfix.RULE, body)
        self.assertIn("gitleaks: credential found (origin/main..HEAD)", body)
        self.assertNotIn(leakfix.sidekiq_value_tail(), body)

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_credential_already_on_main_is_not_the_branch_s(self):
        rc, tag, body = self.stage({"more.txt": "more\n"},
                                   main_files={"hello.txt": "hello\n", "old.sh": leakfix.sidekiq_line()})
        self.assertEqual(rc, 0, body)
        self.assertIn("gitleaks: clean (origin/main..HEAD)", body)

    def test_missing_gitleaks_is_a_note_not_a_pass_claim(self):
        self.without_gitleaks()
        rc, tag, body = self.stage({"more.txt": "more\n"})
        self.assertEqual(rc, 0)
        self.assertIn("gitleaks: NOT SCANNED (not installed in the staging image)", body)

    def test_coverage_note_without_coverage_file(self):
        self.without_gitleaks()
        rc, _, body = self.stage({"more.txt": "more\n"})
        self.assertIn("diff-cover: not measured (no coverage.xml", body)

    def test_coverage_note_is_never_a_verdict(self):
        self.without_gitleaks()
        self.fake_tool("diff-cover", "#!/bin/bash\n"
                       "echo 'src/a.py (50.0%): Missing lines 3-4'\n"
                       "echo 'Total:   4 lines'\necho 'Missing: 2 lines'\necho 'Coverage: 50%'\n")
        verify = "#!/bin/bash\ncd \"$(dirname \"$0\")/..\"\necho '<coverage/>' > coverage.xml\n"
        rc, tag, body = self.stage({"more.txt": "more\n"},
                                   main_files={"hello.txt": "hello\n", ".dark/verify.sh": verify})
        self.assertEqual(rc, 0)
        self.assertTrue(tag["ok"])
        self.assertIn("diff-cover: src/a.py (50.0%): Missing lines 3-4; Total:   4 lines; "
                      "Missing: 2 lines; Coverage: 50%", body)


if __name__ == "__main__":
    unittest.main()

import base64
import importlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from tests import fakes

VERIFY = "#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/..\"\ntest -f hello.txt\necho verify OK\n"


def tar_b64(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


class Stager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gitea = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.gitea.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.full = "dark/t-hello"
        self.gitea.issues[self.full] = {1: {"title": "run", "body": "", "state": "open", "comments": []}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stage(self, acceptance, files=None, timeout=10):
        files = files or {".factory/verify.sh": VERIFY, "hello.txt": "hello\n"}
        git_url = fakes.make_origin(os.path.join(self.tmp, "repos"), self.full, files, branch="run/r1")
        task = {"gitea": self.gitea.url, "git_url": git_url, "repo": self.full, "issue": 1,
                "branch": "run/r1", "token": "agent-tok", "timeout": timeout, "run": "r1",
                "acceptance_tar_b64": tar_b64(acceptance) if isinstance(acceptance, dict) else acceptance}
        tpath = os.path.join(self.tmp, "task.json")
        with open(tpath, "w") as f:
            json.dump(task, f)
        os.environ["DARK_TASK"] = tpath
        os.environ["DARK_WORK"] = os.path.join(self.tmp, "stage")
        import dark.stager as stager
        stager = importlib.reload(stager)
        return stager.main()

    def tag(self):
        bodies = self.gitea.bodies(self.full, 1)
        done = [b for b in bodies if b.startswith("STAGE-DONE")]
        self.assertEqual(len(done), 1, bodies)
        line = [l for l in done[0].splitlines() if l.startswith("DARK:")][-1]
        return json.loads(line[len("DARK:"):]), done[0]

    def test_pass_with_partial_credit_lines(self):
        rc = self.stage({"run.sh": "#!/bin/bash\ngrep -q hello hello.txt && echo 'CHECK content ok' || echo 'CHECK content fail'\n"
                                   "echo 'CHECK exists ok'\ntest -e .acceptance/nothing && echo 'CHECK extra ok' || echo 'CHECK extra fail'\n"
                                   "grep -q hello hello.txt\n"})
        self.assertEqual(rc, 0)
        tag, body = self.tag()
        self.assertEqual((tag["ok"], tag["checks_ok"], tag["checks_total"]), (True, 2, 3))
        self.assertIn("seconds", tag)

    def test_fail_counts_checks(self):
        rc = self.stage({"run.sh": "echo 'CHECK a ok'\necho 'CHECK b fail'\nexit 1\n"})
        self.assertEqual(rc, 1)
        tag, _ = self.tag()
        self.assertEqual((tag["ok"], tag["checks_ok"], tag["checks_total"]), (False, 1, 2))

    def test_no_check_lines_is_one_check(self):
        rc = self.stage({"run.sh": "exit 0\n"})
        tag, _ = self.tag()
        self.assertEqual((tag["ok"], tag["checks_ok"], tag["checks_total"]), (True, 1, 1))

    def test_verify_red_in_staging_is_env(self):
        rc = self.stage({"run.sh": "exit 0\n"}, files={".factory/verify.sh": VERIFY, "other.txt": "x\n"})
        self.assertEqual(rc, 1)
        tag, body = self.tag()
        self.assertFalse(tag["ok"])
        self.assertIn("STAGE-ENV", body)
        self.assertEqual(tag["checks_total"], 0)

    def test_timeout(self):
        rc = self.stage({"run.sh": "sleep 5\n"}, timeout=1)
        self.assertEqual(rc, 1)
        tag, body = self.tag()
        self.assertIn("TIMEOUT", body)

    def test_bad_tar_path_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("../escape.sh")
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
        rc = self.stage(base64.b64encode(buf.getvalue()).decode())
        self.assertEqual(rc, 1)
        _, body = self.tag()
        self.assertIn("bad path", body)

    def test_missing_run_sh(self):
        rc = self.stage({"notes.txt": "x"})
        _, body = self.tag()
        self.assertIn("no run.sh", body)


if __name__ == "__main__":
    unittest.main()

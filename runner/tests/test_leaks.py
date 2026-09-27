"""ops/leaks.sh: gitleaks over a repository's history, the credential gate
verify.sh runs before a push."""
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import leakfix

HERE = os.path.dirname(os.path.abspath(__file__))
LEAKS = os.path.join(HERE, "..", "ops", "leaks.sh")
HAVE = shutil.which("gitleaks") is not None


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args], cwd=cwd,
                   check=True, capture_output=True)


class Leaks(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git("init", "-q", "-b", "main", cwd=self.repo)
        self.commit("README.md", "a clean repository\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def commit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        git("add", name, cwd=self.repo)
        git("commit", "-qm", f"add {name}", cwd=self.repo)

    def leaks(self, *args):
        return subprocess.run(["bash", LEAKS, *args], cwd=self.repo, capture_output=True, text=True)

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_clean_history_passes(self):
        r = self.leaks()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("leaks OK", r.stdout)

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_planted_credential_is_refused_and_named(self):
        self.commit("deploy.sh", leakfix.sidekiq_line())
        r = self.leaks()
        self.assertEqual(r.returncode, 1)
        self.assertIn("LEAKS FAIL", r.stderr)
        self.assertIn("deploy.sh", r.stderr)
        self.assertIn(leakfix.RULE, r.stderr)
        self.assertNotIn(leakfix.sidekiq_value_tail(), r.stderr + r.stdout)  # --redact

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_a_credential_removed_later_is_still_in_history(self):
        self.commit("deploy.sh", leakfix.sidekiq_line())
        os.remove(os.path.join(self.repo, "deploy.sh"))
        git("commit", "-qam", "remove it", cwd=self.repo)
        self.assertEqual(self.leaks().returncode, 1)

    @unittest.skipUnless(HAVE, "gitleaks not installed")
    def test_range_scans_only_the_new_commits(self):
        self.commit("deploy.sh", leakfix.sidekiq_line())
        git("tag", "base", cwd=self.repo)
        self.commit("more.md", "later work\n")
        self.assertEqual(self.leaks("base..HEAD").returncode, 0)
        self.assertEqual(self.leaks("base~1..HEAD").returncode, 1)

    def test_missing_gitleaks_fails_instead_of_skipping(self):
        empty = tempfile.mkdtemp()
        try:
            os.symlink(shutil.which("bash"), os.path.join(empty, "bash"))
            r = subprocess.run([os.path.join(empty, "bash"), LEAKS], cwd=self.repo,
                               env={"PATH": empty}, capture_output=True, text=True)
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("gitleaks not on PATH", r.stderr)


if __name__ == "__main__":
    unittest.main()

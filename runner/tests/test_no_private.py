"""ops/no-private.sh: the gate that keeps a real private address or a real
home-directory path out of tracked text files."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NO_PRIVATE = os.path.join(HERE, "..", "ops", "no-private.sh")

# Built from parts so this file itself carries neither pattern.
PRIVATE = "192." + "168.7.7"
FOREIGN_HOME = "/home/" + "someone" + "/x"
USER_HOME = "/home/" + "user" + "/x"


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
                   cwd=cwd, check=True, capture_output=True)


class NoPrivate(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git("init", "-q", "-b", "main", cwd=self.repo)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def commit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        git("add", name, cwd=self.repo)
        git("commit", "-qm", "add " + name, cwd=self.repo)

    def no_private(self):
        return subprocess.run(["bash", NO_PRIVATE], cwd=self.repo,
                              capture_output=True, text=True)

    def test_a_clean_tree_passes(self):
        self.commit("README.md", "a clean example\n")
        r = self.no_private()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no-private OK", r.stdout)

    def test_a_private_address_is_refused_and_named(self):
        self.commit("deploy.sh", "host=" + PRIVATE + "\n")
        r = self.no_private()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO-PRIVATE FAIL", r.stderr)
        self.assertIn("deploy.sh", r.stderr)
        self.assertIn(PRIVATE, r.stderr)

    def test_a_foreign_home_is_refused_and_named(self):
        self.commit("notes.md", "path " + FOREIGN_HOME + "\n")
        r = self.no_private()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO-PRIVATE FAIL", r.stderr)
        self.assertIn("notes.md", r.stderr)
        self.assertIn(FOREIGN_HOME, r.stderr)

    def test_the_user_home_is_allowed(self):
        self.commit("notes.md", "path " + USER_HOME + "\n")
        r = self.no_private()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no-private OK", r.stdout)


if __name__ == "__main__":
    unittest.main()

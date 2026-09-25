"""Exporter tests for the full starting tree: a task without `start/`, a
chained task and one with `start/` each export the language template, the
chain base and the git commit step (thread 1051 item b).

The fixtures live under /tmp/fx, and every export writes there.
"""
import os
import shutil
import tempfile
import unittest

from dark import export as export_mod

SCRATCH = "/tmp/fx"
TASK_TOML = """id = "{tid}"
title = "{tid}: a fixture task"
class = "additive"
lang = "python"
may_edit = []
stage_timeout = 900
spec = \"\"\"A fixture spec for {tid}.\"\"\"
"""
ACCEPTANCE = "#!/bin/bash\n# hidden acceptance for the fixture\necho 'CHECK hello ok'\n"


def make_task(root, tid, start=None, oracle=None, after=None):
    """A minimal dark task. `start` and `oracle` are {relative path: text}."""
    d = os.path.join(root, tid)
    os.makedirs(d)
    text = TASK_TOML.format(tid=tid)
    if after:
        text += f'after = "{after}"\n'
    with open(os.path.join(d, "task.toml"), "w") as f:
        f.write(text)
    for kind, files in (("start", start), ("oracle", oracle)):
        for rel, body in (files or {}).items():
            p = os.path.join(d, kind, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(body)
    os.makedirs(os.path.join(d, "acceptance"))
    p = os.path.join(d, "acceptance", "run.sh")
    with open(p, "w") as f:
        f.write(ACCEPTANCE)
    os.chmod(p, 0o755)
    return d


class ExportStartTree(unittest.TestCase):
    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=SCRATCH)
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_template_and_commit(self, out):
        """The exported context holds the template and the Dockerfile
        commits the tree as one git commit."""
        self.assertTrue(os.path.isfile(os.path.join(out, "start", ".dark", "verify.sh")))
        self.assertTrue(os.path.isfile(os.path.join(out, "start", "README.md")))
        with open(os.path.join(out, "Dockerfile")) as f:
            dockerfile = f.read()
        self.assertIn("COPY start/ /app/", dockerfile)
        self.assertIn("git init -q -b main", dockerfile)
        self.assertIn("git add -A -f", dockerfile)
        self.assertIn("commit -q -m 'starting tree'", dockerfile)

    def test_a_task_without_start_still_carries_the_template(self):
        task = make_task(self.tmp, "no-start")
        export_mod.export(task, self.out)
        self.assert_template_and_commit(self.out)

    def test_a_task_with_start_overlays_the_template(self):
        task = make_task(self.tmp, "with-start", start={"hello.py": "print('hi')\n"})
        export_mod.export(task, self.out)
        self.assert_template_and_commit(self.out)
        with open(os.path.join(self.out, "start", "hello.py")) as f:
            self.assertEqual(f.read(), "print('hi')\n")

    def test_a_chained_task_carries_the_base_tree(self):
        make_task(self.tmp, "chain-a", oracle={"base.py": "BASE = 1\n"})
        task = make_task(self.tmp, "chain-b", start={"b.py": "B = 2\n"}, after="chain-a")
        export_mod.export(task, self.out)
        self.assert_template_and_commit(self.out)
        with open(os.path.join(self.out, "start", "base.py")) as f:
            self.assertEqual(f.read(), "BASE = 1\n")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "start", "b.py")))

    def test_solution_does_not_keep_the_packers_uid(self):
        task = make_task(self.tmp, "ownership", oracle={"solved.py": "x = 1\n"})
        export_mod.export(task, self.out)
        with open(os.path.join(self.out, "solution.sh")) as f:
            script = f.read()
        self.assertIn("--no-same-owner", script)


if __name__ == "__main__":
    unittest.main()

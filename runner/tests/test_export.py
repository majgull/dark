"""Exporter tests: one dark task directory in, the Terminal-Bench layout out.

PyYAML is not a dependency, so task.yaml is checked by lines rather than by
parsing it. The fixtures live under /tmp/fx, and every export writes there.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from dark import export as export_mod

RUNNER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = "/tmp/fx"
TASK_TOML = """id = "{tid}"
title = "{tid}: a fixture task"
class = "additive"
lang = "python"
may_edit = []
stage_timeout = 900
spec = \"\"\"{spec}\"\"\"
"""
SPEC = """First line of the instruction.

Second paragraph with `code`."""
ACCEPTANCE = "#!/bin/bash\n# hidden acceptance for the fixture\necho 'CHECK hello ok'\n"
ORACLE = "print('hello, dark')\n"
SEVEN = ("task.yaml", "Dockerfile", "docker-compose.yaml", "run-tests.sh",
         "solution.sh", "tests", "start")


def make_task(root, tid="hello-fixture", start=True, oracle=True, acceptance=True, toml=True,
              spec=SPEC):
    d = os.path.join(root, tid)
    os.makedirs(d)
    if toml:
        with open(os.path.join(d, "task.toml"), "w") as f:
            f.write(TASK_TOML.format(tid=tid, spec=spec))
    if start:
        os.makedirs(os.path.join(d, "start"))
        with open(os.path.join(d, "start", "hello.py"), "w") as f:
            f.write("print('hello')\n")
    if acceptance:
        os.makedirs(os.path.join(d, "acceptance"))
        p = os.path.join(d, "acceptance", "run.sh")
        with open(p, "w") as f:
            f.write(ACCEPTANCE)
        os.chmod(p, 0o755)
    if oracle:
        os.makedirs(os.path.join(d, "oracle"))
        with open(os.path.join(d, "oracle", "hello.py"), "w") as f:
            f.write(ORACLE)
    return d


def run_script(path, workdir):
    env = dict(os.environ, WORKDIR=workdir)
    return subprocess.run(["bash", path], cwd=workdir, env=env, capture_output=True, text=True)


class Export(unittest.TestCase):
    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=SCRATCH)
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_layout_and_task_yaml(self):
        task = make_task(self.tmp)
        written = export_mod.export(task, self.out)
        self.assertEqual(written, self.out)
        for name in SEVEN:
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "tests", "run.sh")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "start", "hello.py")))

        with open(os.path.join(self.out, "task.yaml")) as f:
            text = f.read()
        self.assertIn("# task: hello-fixture", text)
        self.assertIn("instruction: |2", text)
        self.assertIn("  First line of the instruction.", text)
        self.assertIn("  Second paragraph with `code`.", text)
        self.assertIn("parser_name: pytest", text)
        self.assertIn("max_agent_timeout_sec: 900", text)
        self.assertIn("max_test_timeout_sec: 900", text)

        for name in ("run-tests.sh", "solution.sh"):
            self.assertTrue(os.access(os.path.join(self.out, name), os.X_OK), name)

        with open(os.path.join(self.out, "Dockerfile")) as f:
            dockerfile = f.read()
        self.assertIn("FROM debian:stable-slim", dockerfile)
        self.assertIn("COPY start/ /app/", dockerfile)
        with open(os.path.join(self.out, "docker-compose.yaml")) as f:
            compose = f.read()
        self.assertIn("services:", compose)
        self.assertIn("dockerfile: Dockerfile", compose)
        with open(os.path.join(self.out, "run-tests.sh")) as f:
            run_tests = f.read()
        self.assertIn("cp -r \"$TEST_DIR\" .acceptance", run_tests)
        self.assertIn("bash .acceptance/run.sh", run_tests)

    def test_carriage_return_gets_a_quoted_scalar(self):
        # a bare carriage return is a YAML line break; it cannot live in a
        # block scalar, so the instruction becomes a double-quoted scalar
        export_mod.export(make_task(self.tmp, spec="A `\\r` right before the newline."), self.out)
        with open(os.path.join(self.out, "task.yaml")) as f:
            text = f.read()
        self.assertIn('instruction: "A `\\r` right before the newline."', text)

    def test_solution_applies_the_oracle(self):
        export_mod.export(make_task(self.tmp), self.out)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        r = run_script(os.path.join(self.out, "solution.sh"), work)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(work, "hello.py")) as f:
            self.assertEqual(f.read(), ORACLE)

    def test_missing_oracle_still_exports(self):
        export_mod.export(make_task(self.tmp, oracle=False), self.out)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "solution.sh")))
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        r = run_script(os.path.join(self.out, "solution.sh"), work)
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT AVAILABLE", r.stdout)

    def test_refusal_writes_nothing(self):
        # start/ is optional since 1051 item b: the export builds the
        # starting tree from the template, so only these two are required.
        for absent in ("toml", "acceptance"):
            task = make_task(self.tmp, tid=f"bad-{absent}", **{absent: False})
            with self.assertRaises(export_mod.ExportError):
                export_mod.export(task, os.path.join(self.out, absent))
            self.assertFalse(os.path.exists(os.path.join(self.out, absent)))

    def test_cli_exit_code(self):
        task = make_task(self.tmp, tid="bad-cli", acceptance=False)
        out = os.path.join(self.out, "cli")
        env = dict(os.environ, PYTHONPATH=RUNNER + os.pathsep + os.environ.get("PYTHONPATH", ""))
        r = subprocess.run([sys.executable, "-m", "dark", "export-harbor", task, out],
                           cwd=RUNNER, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("export-harbor:", r.stderr)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()

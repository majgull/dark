"""The starting-tree step stays quiet when the image already holds the commit.

`dark_tasks.py`'s `starting_tree` setup step is one `sh -c` line run in the
sandbox. The exported image now commits the same starting tree in its own
Dockerfile (`runner/dark/export.py`), so the step must commit only when the
working tree it was handed differs from HEAD; a second commit of an unchanged
tree exits non-zero and fails every sample.

These tests run that same shell line with plain `subprocess` in a temporary
directory: no docker, no Inspect.
"""
import ast
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent


def start_tree_script():
    """The step's shell line, read from `dark_tasks.py`.

    `dark_tasks` imports `inspect_ai`, which the runner's test environment does
    not install, so the constant is read from the source rather than imported.
    """
    tree = ast.parse((HERE / "dark_tasks.py").read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "START_TREE_SCRIPT" in names:
            return ast.literal_eval(node.value)
    raise AssertionError("dark_tasks.py defines no START_TREE_SCRIPT")


SCRIPT = start_tree_script()


def run_step(cwd):
    return subprocess.run(["sh", "-c", SCRIPT], cwd=cwd, capture_output=True, text=True)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def head(cwd):
    return git(cwd, "rev-parse", "HEAD").stdout.strip()


def commits(cwd):
    return git(cwd, "rev-list", "--count", "HEAD").stdout.strip()


def test_the_solver_runs_that_one_line():
    """The step must use the constant this test runs, not a copy of it."""
    tree = ast.parse((HERE / "dark_tasks.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "starting_tree")
    assert "START_TREE_SCRIPT" in ast.unparse(fn)


def test_no_repository_commits_the_tree(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    done = run_step(tmp_path)
    assert done.returncode == 0, done.stderr
    assert commits(tmp_path) == "1"
    assert "hello.py" in git(tmp_path, "show", "--format=", "--name-only", "HEAD").stdout


def test_a_tree_already_committed_is_left_alone(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    assert run_step(tmp_path).returncode == 0
    first = head(tmp_path)
    again = run_step(tmp_path)
    assert again.returncode == 0, again.stderr
    assert head(tmp_path) == first
    assert commits(tmp_path) == "1"


def test_a_different_tree_gets_a_new_commit(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    assert run_step(tmp_path).returncode == 0
    first = head(tmp_path)
    (tmp_path / "hello.py").write_text("print('changed')\n")
    done = run_step(tmp_path)
    assert done.returncode == 0, done.stderr
    assert head(tmp_path) != first
    assert commits(tmp_path) == "2"
    assert git(tmp_path, "show", "HEAD:hello.py").stdout == "print('changed')\n"

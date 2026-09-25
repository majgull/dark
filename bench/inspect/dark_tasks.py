"""dark_tasks - dark tasks as one Inspect task, run in a docker sandbox.

The dataset is a directory of tasks already written in the Terminal-Bench
layout by `python3 -m dark export-harbor <task-dir> <out-dir>`: one sample per
subdirectory, its Dockerfile as that sample's sandbox, its `task.yaml`
instruction as the prompt.

  inspect eval bench/inspect/dark_tasks.py -T tasks_dir=DIR --model mockllm/model

`tasks_dir` may be left out when DARK_TB_DIR names the directory, and
`source_dir` (the dark-tasks checkout the export was made from) when
DARK_TASKS does, the variable the other bench tools read. The hidden tests
never enter the sandbox until scoring, as in dark itself.

The export holds only a task's own `start/`. dark's work repo is more: the
language template, the tree an earlier chain step leaves, and one git commit
of it all (dark/tasks.py). `starting_tree` lays that tree down before any
solver runs, using the runner's own functions read-only, so an oracle or an
agent starts from what dark's executor starts from.
"""

import io
import os
import shlex
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer, stderr
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import SandboxEnvironment, sandbox

REPO = Path(__file__).resolve().parents[2]
# the runner package sits beside the bench in this checkout, and its task
# functions are the one implementation of how a start tree is built
sys.path.append(str(REPO / "runner"))

from dark import tasks as dark_source  # noqa: E402

TEMPLATES = REPO / "templates"
WORKDIR = "/app"
# Where the exported scripts look for the tests: Harbor's own path.
TEST_DIR = "/tests"
# export-harbor writes both timeouts from the dark task's stage_timeout, and
# ten minutes is dark's default when a task sets none.
DEFAULT_TIMEOUT = 600


def _sample(directory: Path, source: str) -> Sample:
    spec = yaml.safe_load((directory / "task.yaml").read_text())
    return Sample(
        id=directory.name,
        input=spec["instruction"],
        metadata={
            "directory": str(directory),
            "source": source,
            "test_timeout": spec.get("max_test_timeout_sec", DEFAULT_TIMEOUT),
        },
        # The exported docker-compose.yaml is Terminal-Bench's: it names its
        # one service "client" and reads T_BENCH_* variables, neither of
        # which Inspect supplies. The Dockerfile alone builds the same image.
        sandbox=("docker", str(directory / "Dockerfile")),
    )


async def _put_tree(box: SandboxEnvironment, local: Path, remote: str) -> None:
    """Copy a directory's contents into the sandbox as one tar, so modes survive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for child in sorted(local.iterdir()):
            tf.add(child, arcname=child.name)
    await box.write_file("/tmp/tree.tar", buf.getvalue())
    made = await box.exec([
        "sh", "-c",
        f"mkdir -p {shlex.quote(remote)}"
        f" && tar --no-same-owner -xf /tmp/tree.tar -C {shlex.quote(remote)}"
        f" && rm /tmp/tree.tar",
    ])
    if not made.success:
        raise RuntimeError(f"copying {local} to {remote}: {made.stderr}")


def _start_tree(source: str) -> dict[str, bytes]:
    """The tree dark's executor starts a task from: the template, or the
    oracle tree of the step this one follows, plus the task's own start/."""
    dark_task = dark_source.load_task(source)
    base = None
    if dark_task.after:
        earlier = dark_source.load_task(os.path.join(os.path.dirname(dark_task.dir), dark_task.after))
        base = dark_source.oracle_tree(earlier, str(TEMPLATES))
    return dark_source.work_tree(dark_task, str(TEMPLATES), base_tree=base)


@solver
def starting_tree() -> Solver:
    """Lay dark's starting tree over the exported image and commit it."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        box = sandbox()
        with tempfile.TemporaryDirectory() as tmp:
            dark_source.write_tree(_start_tree(state.metadata["source"]), tmp)
            await _put_tree(box, Path(tmp), WORKDIR)
        # dark's stager and acceptance read the repo's root commit
        committed = await box.exec([
            "sh", "-c",
            "git init -q -b main && git add -A -f"
            " && git -c user.name=dark-runner -c user.email=dark-runner@localhost"
            " commit -q -m 'starting tree'",
        ], cwd=WORKDIR)
        if not committed.success:
            raise RuntimeError(f"committing the starting tree: {committed.stderr}")
        return state

    return solve


@solver
def oracle() -> Solver:
    """Run the task's reference solution: no model is called."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        directory = Path(state.metadata["directory"])
        box = sandbox()
        await box.write_file("/solution.sh", (directory / "solution.sh").read_text())
        ran = await box.exec(["bash", "/solution.sh"], timeout=state.metadata["test_timeout"])
        # solution.sh unpacks its tar as root with `cp -a`, which gives /app
        # the uid of whoever packed it; git then refuses the repository as
        # "dubious ownership" and an acceptance that reads history sees none.
        await box.exec(["chown", "-R", "root:root", WORKDIR])
        state.store.set("oracle_returncode", ran.returncode)
        state.store.set("oracle_output", (ran.stdout + ran.stderr)[-2000:])
        return state

    return solve


@scorer(metrics=[accuracy(), stderr()])
def run_tests():
    """CORRECT when the hidden tests, run in the sandbox, exit 0."""

    async def score(state: TaskState, target: Target) -> Score:
        directory = Path(state.metadata["directory"])
        box = sandbox()
        await _put_tree(box, directory / "tests", TEST_DIR)
        await box.write_file("/run-tests.sh", (directory / "run-tests.sh").read_text())
        ran = await box.exec(
            ["bash", "/run-tests.sh"],
            env={"TEST_DIR": TEST_DIR},
            timeout=state.metadata["test_timeout"],
        )
        return Score(
            value=CORRECT if ran.returncode == 0 else INCORRECT,
            answer=f"exit {ran.returncode}",
            explanation=(ran.stdout + ran.stderr)[-2000:],
        )

    return score


@task
def dark_tasks(tasks_dir: str | None = None, source_dir: str | None = None) -> Task:
    root = tasks_dir or os.environ.get("DARK_TB_DIR")
    if not root:
        raise ValueError("pass -T tasks_dir=DIR (exported tasks) or set DARK_TB_DIR")
    index = dark_source.task_dir_index(source_dir or os.environ.get("DARK_TASKS"))
    directories = sorted(
        p for p in Path(root).iterdir() if (p / "task.yaml").is_file() and (p / "Dockerfile").is_file()
    )
    if not directories:
        raise ValueError(f"{root}: no exported task directories (task.yaml and Dockerfile)")
    unknown = [d.name for d in directories if d.name not in index]
    if unknown:
        raise ValueError(f"not in the dark-tasks checkout (-T source_dir=DIR or DARK_TASKS): {unknown}")
    return Task(
        dataset=[_sample(d, index[d.name][1]) for d in directories],
        setup=starting_tree(),
        solver=oracle(),
        scorer=run_tests(),
    )

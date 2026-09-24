# Contributing

Thanks for looking at the runner. This file says how to run the checks and
where the pieces live. Every term is defined once in `docs/concepts.md`.

## What you need

Python 3.11 or newer, and the Go and Python toolchains for the task checks
that build a reference solution. `pytest` is needed for the repository
hygiene test; it is listed in the `dev` extra in `pyproject.toml`.

## Run the tests

`bash verify.sh` is the one gate for the runner: the unit tests, the config
validation and the no-binaries check. It prints `verify OK` when all three
pass.

```
cd runner && bash verify.sh
```

## Check the tasks

The task set lives in its own repository,
[dark-tasks](https://github.com/majgull/dark-tasks). The two example tasks
under `bench/tasks/` let every tool run without a second checkout:

```
python3 bench/tools/check_tasks.py
```

For the full set, put a dark-tasks checkout beside this repository at
`../dark-tasks` and point the tools at it with `DARK_TASKS`:

```
DARK_TASKS=../dark-tasks python3 bench/tools/check_tasks.py
DARK_TASKS=../dark-tasks python3 bench/tools/validate.py
```

`check_tasks.py` validates every task record. `validate.py` builds each
reference solution against its own hidden checks and confirms that the
untouched starting tree is rejected. `bench/README.md` lists the other
tools, including the mutant check and the arms.

## Add a task

Tasks live in [dark-tasks](https://github.com/majgull/dark-tasks), not in
this repository. A task is a directory `tasks/<id>/` holding `task.toml`,
an optional `start/` overlay, a hidden `acceptance/run.sh`, and an optional
`oracle/`. `bench/README.md` describes each field, the chain rule and the
tools that validate a new task. Add the task there and run the task checks
above.

## Repository hygiene

`runner/tests/test_repo_hygiene.py` keeps evidence out of git. It fails on
any tracked file over 200 KiB, and on any tracked file of a log or media
kind (`.jsonl`, `.ndjson`, `.log`, `.cast`, images, archives) over 20 KiB.
Run it with:

```
python3 -m pytest runner/tests/test_repo_hygiene.py
```

## Commit style

History uses a short imperative subject line, often prefixed with the area
it touches (`runner:`, `bench:`, `docs:`, `README:`). Explain why in the
body when the change is not self-evident. Keep one idea per commit.

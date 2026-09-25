# dark tasks under Inspect

`dark_tasks.py` is one [Inspect](https://inspect.aisi.org.uk) task whose samples are dark tasks exported to the Terminal-Bench layout. Each sample runs in its own docker sandbox built from its exported `Dockerfile`; the hidden `tests/` are copied in only at scoring. Inspect is a bench-side tool: it lives in its own virtual environment, never in `pyproject.toml`.

## Install

Docker with Compose 2.21 or newer, then `python3 -m venv /tmp/fx/venv-inspect && /tmp/fx/venv-inspect/bin/pip install inspect-ai` (written against 0.3.269).

## Export

`export-harbor` refuses a task with no `start/` directory (`missing start`), so those tasks are skipped. `DARK_TASKS` is a dark-tasks checkout, the variable the other bench tools read:

    for d in "$DARK_TASKS"/tasks/*/; do PYTHONPATH=runner python3 -m dark export-harbor "$d" /tmp/fx/tb/$(basename "$d"); done

## Run

The export holds a task's own `start/` only, so the task's `setup` step lays dark's full starting tree (language template, earlier chain steps, one git commit) over it, read from `DARK_TASKS` with the runner's own functions. The oracle applies each reference solution, calls no model, and should score every sample CORRECT:

    DARK_TASKS=... /tmp/fx/venv-inspect/bin/inspect eval bench/inspect/dark_tasks.py -T tasks_dir=/tmp/fx/tb --solver oracle --model mockllm/model --log-dir /tmp/fx/inspect-logs

Read a log with `inspect log dump FILE` or `inspect view --log-dir /tmp/fx/inspect-logs`. Keep `--log-dir` outside the tree: `runner/ops/no-binaries.sh` refuses a committed `.eval` file.

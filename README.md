# dark factory

dark factory is a pipeline that lets a language model write code on its own, inside a
virtual machine that exists for one task and is deleted when the task ends, judged by
tests it never sees. A task with hidden acceptance tests goes through preflight,
execution in a throwaway machine, verification, a push, and a second fresh machine that
runs the hidden tests; the run ends as exactly one outcome in an append-only log. Three
parts live here. The **runner** is that pipeline. The **bench** is its measuring
instrument: the tools that validate a task set against its reference solutions and
deliberately broken copies, the frozen limits a comparison runs under, and the arms (the
ways a model is put to a task). The **templates** are the Go and Python starting trees a
task is laid over. The task set itself, with its hidden tests, is the repository
[dark-tasks](https://github.com/majgull/dark-tasks); two example tasks stay here so the
tools run without another checkout.

## What you need

Running the runner needs what `runner/host.toml` names: a Proxmox host with a VM
template, a Gitea instance, and an OpenAI-compatible model endpoint. Every value in that
file is an example for a local machine; replace it with your own. A container backend is
planned, so the runner can be deployed without Proxmox.

The bench tools need Python 3, `bash`, coreutils and `git`. The task check needs the task
set [dark-tasks](https://github.com/majgull/dark-tasks) cloned beside this checkout;
without it, the same command with no argument checks the two example tasks kept here.

## Quick start

```
cd runner && bash verify.sh                  # tests, config check, no binaries
cd bench && python3 tools/check_tasks.py     # the two example tasks: 0 problems
```

With the full task set cloned beside the repository, `python3 tools/check_tasks.py
../../dark-tasks` checks all 31 tasks. `docs/fresh-container.md` records a bare container
running the checks.

## Layout

```
runner/      the pipeline: the dark/ package, models.toml, budgets.toml, host.toml, ops/, tests/
bench/       tools/ that validate a task set, frozen/ limits, arms.toml; tasks/ and mutants/ hold two examples
templates/   the Go and Python starting trees a task is laid over
docs/        concepts.md (the terms the code uses), history.md (where it came from) and fresh-container.md
```

## Documentation

- `docs/concepts.md` defines every term the code uses.
- `docs/history.md` says where the code came from.
- [dark-tasks](https://github.com/majgull/dark-tasks) is the 31-task set with its hidden tests.
- [dark-paper](https://github.com/majgull/dark-paper) is a frozen study of the tool on its first task set.

## License

The whole repository is dedicated to the public domain under CC0 1.0 Universal;
`LICENSE` is its text.

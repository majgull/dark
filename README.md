<p align="center"><img src="docs/banner.svg" alt="dark factory" width="480"></p>

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
available: [docs/docker.md](docs/docker.md) builds the runner image, starts Gitea and
the runner on one internal network with `compose.yaml`, and spawns each sandbox as a
sibling container, so the runner can be deployed without Proxmox. A third backend,
`lxc`, makes each sandbox a full clone of a snapshot of a real container on the Proxmox
host, named by `sandbox_container` and `sandbox_snapshot` in `runner/host.toml`. Its
`sandbox_pool` (default `""`, no Proxmox pool) is the pool each clone is placed in, so
the throwaways stay grouped, and its `sandbox_allow_in` (default `""`) is a
comma-separated list of `"<ipv4>:<tcp port>"` clients the clone's default-drop firewall
lets in besides the service host. A
sandbox reaches Gitea and one model gate, and `DARK_CONF_DIR` keeps the three toml
files, with your own endpoints and model ids, outside the checkout.

The bench tools need Python 3, `bash`, coreutils and `git`. The task check needs the task
set [dark-tasks](https://github.com/majgull/dark-tasks) cloned beside this checkout;
without it, the same command with no argument checks the two example tasks kept here.

## Quick start

```
python3 -m venv .venv && . .venv/bin/activate
pip install .                                # installs the dark console script
dark --help                                  # the commands the runner offers
(cd runner && bash verify.sh)                # tests, config check, no binaries
python3 bench/tools/check_tasks.py           # the two example tasks: 0 problems
```

For real use, point `DARK_TASKS` at a task set, such as a clone of
[dark-tasks](https://github.com/majgull/dark-tasks). It takes several task sets
at once, `:` separated, so a public set and a private one are checked in one
run; a task name found in two sets is refused, naming both, rather than one
silently winning:

```
export DARK_TASKS=$HOME/src/dark-tasks:$HOME/src/my-tasks
python3 bench/tools/check_tasks.py           # every task in both sets
python3 bench/tools/validate.py              # reference solutions pass, starting trees fail
```

`dark user --task <task.toml> --tier <id>` runs one user-arm task: a browser sandbox checks a deployed URL step by step.

`python3 -m dark long --task <dir> --tier <id>` runs one long-arm task: a session executor works in the task's several repositories and a reviewer session judges the branches it pushed.

`docs/fresh-container.md` records a bare container
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
- `docs/mcp.md` says how to start and register the MCP server.
- `docs/history.md` says where the code came from.
- [Contributing](CONTRIBUTING.md) says how to run the tests and the task checks.
- [dark-tasks](https://github.com/majgull/dark-tasks) is the 31-task set with its hidden tests.
- [dark-paper](https://github.com/majgull/dark-paper) is a frozen study of the tool on its first task set.

## License

The whole repository is dedicated to the public domain under CC0 1.0 Universal;
`LICENSE` is its text.

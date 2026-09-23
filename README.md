# dark

A pipeline that lets a language model write code on its own, inside a virtual machine that exists for one task and is deleted when the task ends, judged by tests it never sees. Two things live here. The **runner** is the pipeline: a task with hidden acceptance tests goes through preflight, execution in a throwaway machine, verification, a push, and a second fresh machine that runs the hidden tests; the run ends as exactly one outcome in an append-only log. The **bench** is the runner's measuring instrument: the rules a task must satisfy, the tools that validate a task set against reference solutions and deliberately broken copies, frozen limits, and the arms. The task set itself, 31 tasks with hidden tests, is the repository [github.com/majgull/dark-tasks](https://github.com/majgull/dark-tasks); two example tasks stay here for reference.

The first study run on them is the paper [github.com/majgull/dark-paper](https://github.com/majgull/dark-paper): 8 small tasks, 6 models, 368 runs. The hidden tests were validated before they judged anything; a shell and file tools changed no verdict and cost three and a half times the model calls; every model solved more with a thinking budget, and a model with no budget spends its calls repeating itself. Every number in it re-derives from the run log with one command, and the tag `v0.1.0` of this repository is the tree that produced that log.

## Two commands check the code

```
cd runner && python3 -m pytest -q tests      # 362 tests
cd bench && python3 tools/check_tasks.py ../../dark-tasks   # 31 tasks, 0 problems, with dark-tasks cloned beside this checkout
```

They need Python 3, `pytest`, `jq`, `bash`, coreutils and `git`, and the second needs the task set [github.com/majgull/dark-tasks](https://github.com/majgull/dark-tasks) cloned next to this repository (without it, the same command with no argument checks the two example tasks kept here); nothing else is installed for them, and `docs/fresh-container.md` is the transcript of a bare Debian container running them.

## How this was made

By one person and several language models, and the split is stated so a reader can weigh it. majgull chose the questions, the task classes, the models, the limits and the licence, took the decisions the paper records as decisions, and reviewed every draft with inline comments. Claude, running as a coding agent on his machine, coordinated. Delegated sessions of DeepSeek, GLM and Claude Sonnet models wrote the runner and the bench, the tasks with their gold solutions and mutants, and reviewed the tree. No line of code or prose in this repository was typed by a person. Section 8 of the paper says the same in more detail, and says why the repository is dedicated to the public domain rather than licensed.

## Status

- **What it is.** A working pipeline that ran the paper's 368 unattended runs, one of which is recorded as a failure of the machinery (a model endpoint refused a request; section 5.4 of the paper).
- **What it is not.** Not a benchmark for ranking models: 8 tasks and 6 models are the instrument's first calibration, not a leaderboard. Not a security result: isolation is the premise and the paper says it was not tested.
- **What is proven.** The bench's hidden tests are validated (section 5.1 of the paper). The runner's tests and the bench's task check pass.
- **What a stranger can run.** The two commands above, on any machine. The runner itself needs what `runner/host.toml` names: a Proxmox host with a VM template, a Gitea instance, and an OpenAI-compatible model endpoint. The role names in that file (`git-host`, `cpu-host`, `gpu-host`) are where private addresses were before this copy was made.
- **What is next.** The runner and the bench become one package with subcommands. On the measurement side: the no-reasoning cells run through the same deployment as the other budgets, so one variable moves; sampling parameters and a timestamp per call in every record; more rounds per cell for a variance; a tools comparison that holds reasoning spent level, not only the cap. Section 7 of the paper gives the list in order.

## The trust boundary

What the model could touch, what it could not, and how that is known.

- Each run gets its own virtual machine, cloned from one template and deleted at the end. Its firewall denies everything in both directions except traffic to the experiment host, DNS and DHCP. This was checked once from inside a live machine; it is configuration, not a security proof.
- The machine holds one Git token scoped to the organisation the work goes into, and no provider key. The model endpoints sit on the experiment host.
- The hidden tests run in a second fresh machine, after the model's machine is gone, against the branch the run pushed. The model never sees them and never runs where they are.
- Nothing the machine says is trusted. A verdict counts only when it carries a value created after the testing machine started, which the model's machine never saw.
- Every run writes an opening record with its limits and their hash, and a closing record with what it did and what decided it. The log is append-only, and the paper's repository pins the snapshots it used by hash.

## Layout

```
runner/      the pipeline: dark/ package, models.toml, budgets.toml, host.toml, ops/, tests/
bench/       tools/ that validate a task set, frozen/ limits, admission/, arms.toml; tasks/ and mutants/ hold two examples
templates/   the Go and Python starting trees a task is laid over
docs/        the fresh-container transcript
```

This repository holds code and configuration only. Run logs, launcher logs, transcripts and figures never enter it: a test in `runner/tests` fails on any tracked file over 200 KB, or over 20 KB for a file of those kinds, and the evidence of a study goes to a dataset the study's repository pins by revision and hash, as the paper's does at [huggingface.co/datasets/majgull/dark-evidence](https://huggingface.co/datasets/majgull/dark-evidence).

## Provenance

The runner and bench were developed in a private Git server and imported here at the commits the paper names (runner `40fa4cb`, the version that ran the paper's second round; bench at the `acceptance-v4` tag its records carry, whose task set is now `dark-tasks` at `v1.0`). Private identifiers (network addresses, account names, and the names of private repositories and tools) were replaced by role names or generic words before import, the same substitution on every file, and nothing else was changed.

## How to cite

Cite the paper, whose citation target is the tagged release `v1.0` of [github.com/majgull/dark-paper](https://github.com/majgull/dark-paper/releases/tag/v1.0). The code it describes is the tag `v0.1.0` here.

## License

The whole repository is dedicated to the public domain under CC0 1.0 Universal; `LICENSE` is its text. Most of this repository was generated by language models, and section 8 of the paper says, with the sources, that under EU, German and US law such material is mostly outside copyright already; the dedication gives away whatever remains, with a fallback licence for any right the law does not let a person waive, and disclaims warranty. Nothing asks for attribution. A citation of the paper is welcome and not required. Anyone carrying code from here into a project with its own rules on model-written contributions should treat it as model-written, because it is.

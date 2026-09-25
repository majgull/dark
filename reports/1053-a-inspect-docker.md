# dark tasks under Inspect: the oracle in a docker sandbox, then a cheap model as the agent

Date 2026-09-25. Branch `wt/dark-code-writer-t1053-a-inspect-docker`, two code commits, `3f34140` (item 1, the task file and the oracle) and `850b0a0` (item 2, the agent solver), then this report. Files: `bench/inspect/dark_tasks.py`, `bench/inspect/README.md`, this report.

## Terms, once

**dark task**: a directory in the dark-tasks checkout holding `task.toml` (the spec), `start/` (files laid over a language template), `acceptance/` (hidden tests, `run.sh` exits 0 on pass) and `oracle/` (a reference solution). **Oracle**: that reference solution, run in place of a model to prove the hidden tests can pass. **Chain**: a dark task with `after = <id>` starts from the tree the named earlier task leaves. **Starting tree**: what dark's executor is given: the language template in `templates/`, or the tree an earlier chain step leaves, plus the task's own `start/`, committed as one git commit (`runner/dark/tasks.py`). **Terminal-Bench layout**: the files `python3 -m dark export-harbor <task-dir> <out-dir>` writes for one task: `task.yaml`, `Dockerfile`, `docker-compose.yaml`, `run-tests.sh`, `solution.sh`, `tests/` (and `start/`).

**Inspect** (`inspect-ai`, MIT, UK AI Security Institute): an evaluation framework. A **task** is a dataset of samples plus a solver plus a scorer, returned from a function marked `@task`. A **sample** is one row of the dataset, here one dark task. A **solver** is what acts on a sample: it is handed the sample's state and returns it changed. A **scorer** grades the final state and returns a score, here `C` (CORRECT) or `I` (INCORRECT). A **sandbox** is where commands run, here a docker container built from a Dockerfile. A **setup step** is a solver a task always runs first, even when another solver is chosen on the command line. A **tool call** is one request by the model to run a tool, here `bash` in the container. A **ReAct agent** (`react()`) is Inspect's built-in loop: the model reasons, calls tools, reads results, and repeats until it stops or calls `submit()`. A **message limit** caps how many messages one sample may use. An **eval log** is the file Inspect writes per run (`.eval`), read with `inspect log dump` (JSON) or opened with `inspect view`. The **mock model** (`mockllm/model`) is Inspect's stand-in provider for runs that never call a model.

**dark runner terms, used only in the last section.** **Staging**: after a run, dark judges the delivered branch in a second, fresh machine, never the one the model worked in. **Envelope**: the limits one run may spend (calls, seconds, reasoning). **Ledger row**: one line in dark's append-only JSONL ledger, holding a run's start, transitions and end. **Records repository**: the Gitea repository a run's stream, task record and hash are pushed to.

## Result

Inspect 0.3.269 runs dark tasks: one task file, one docker sandbox per sample, declared per sample. The oracle scores CORRECT on 15 of 15 exported tasks (log `2026-09-25T18-31-17-00-00_dark-tasks_PgsHpGsFwtKMU63aKPTiNf.eval`), and a solver that does nothing scores 0 of 4 on the same tasks, so the scorer discriminates. It got there only after a setup step, because the exported layout on its own is not enough: the oracle scored 6 of 15 on the bare export (section "What the export lacks"). `export-harbor` refuses 16 of the 31 tasks (`missing start`). The agent (Inspect's `react()` with `bash()`, on `dsf:latest` over the LAN endpoint, 2 samples) ran to completion with its tool calls in the log: 1 CORRECT, 1 INCORRECT on a hidden check.

## Sources read (fetched with `curl -sL`, this session)

Every page returned 200 as HTML (140 to 260 KB of markup each), so I read the `.html.md` form of the same URL; NOT FOUND applies to none of the six. Extra pages read to finish a job: `custom-scorers`, `react-agent`, `agents`, `models`, `options`.

- tasks: "Tasks provide a recipe for an evaluation consisting minimally of a dataset, a solver, and a scorer (and possibly other options) and is returned from a function decorated with `@task`." Used for the shape of `dark_tasks`. And: "In these scenarios you can define a `setup` solver that is always run even when another `solver` is substituted." Used for `starting_tree`. And: "You can substitute an alternate solver for the solver that is built in to your Task using the `--solver` command line parameter". Used for `--solver oracle`.
- sandboxing: "You can either define a default `sandbox` for an entire Task as illustrated above, or alternatively define a per-sample `sandbox`. For example, you might want to do this if each sample has its own Dockerfile and/or custom compose configuration file." Used for `Sample(sandbox=...)`. And: "a tuple of sandbox type and config file (e.g. `("docker", "compose.yaml")`)". Used for `("docker", <Dockerfile>)`. And: "A sandbox environment must be specified at the sample, task, or evaluation level if any tools, agents or scorers call the sandbox() function." And the table row: "`Dockerfile` | Creates a sandbox environment by building the image."
- scorers: "Scorers evaluate whether solvers were successful in finding the right `output` for the `target` defined in the dataset, and in what measure." The page covers built-in scorers only; the custom-scorer sentences are on `custom-scorers`: "Custom scorers are functions that take a TaskState and Target, and yield a Score." and "Built-in correctness scorers use the constants `CORRECT` (`"C"`), `INCORRECT` (`"I"`), `PARTIAL` (`"P"`), and `NOANSWER` (`"N"`)."
- solvers: "A solver is a Python function that takes a TaskState and `generate` function, and then transforms and returns the TaskState (the `generate` function may or may not be called depending on the solver)." And: "Early termination might also occur if you specify the `message_limit` option and the conversation exceeds that limit". The CLI flag is `--message-limit INTEGER  Limit on total messages used for each sample.` (from `inspect eval --help`, run here); there is no `--max-messages`.
- providers: `OPENAI_API_KEY` is "API key credentials (required)." and `OPENAI_BASE_URL` is "Base URL for requests (optional, defaults to `https://api.openai.com/v1`)". `inspect eval` also has `--model-base-url TEXT  Base URL for for model API` (`--help` and the options page). I used the environment variable. The `openai` package is separate from `inspect-ai`: the first run stopped with "OpenAI API requires optional dependencies. Install with: pip install openai".
- providers, the mock model: NOT FOUND. The providers page does not name `mockllm`, and neither do `tasks`, `models` or `options`. The name is right anyway: the installed package registers it (`inspect_ai/model/_providers/providers.py:301`, `@modelapi(name="mockllm")`) and every oracle run above used `--model mockllm/model`.
- eval-logs: "`inspect log dump` | Print log file contents as JSON." And: "`.eval` | Binary file format optimised for size and speed." And: "You can also use the Inspect log viewer for interactive exploration of logs."
- agents and react-agent: "The agents module includes a flexible, general-purpose react agent". And: "It runs a tool loop until the model calls a special `submit()` tool indicating it is done." The installed version has `react()` (`from inspect_ai.agent import react`), so `basic_agent()` was not needed.

## Setup

`python3 -m venv /tmp/fx/venv-inspect` (Python 3.12.13), `pip install inspect-ai` installed **inspect-ai 0.3.269**; `pip install openai` for the agent run (pip reports 3.19.2). Docker Engine on the host, buildx plugin absent (the classic builder ran and warned). `pyproject.toml` still has `dependencies = []`; nothing was installed outside the venv.

## Export

`PYTHONPATH=runner python3 -m dark export-harbor <dir> /tmp/fx/tb/<name>` over the 31 directories of `/home/blt/main/projects/dark-tasks/tasks/` (readable): 15 exported, 16 refused with `export-harbor: <dir>: missing start`. Exported: csvline-python, duration-python, errwrap-go, intervals-go, median-go, obs-04-tzfix, obs-all, rename-helpers-python, rename-package-go, roman-python, rpn-go, slugify-python, snake-config-go, split-module-python, typehints-python. Refused: csvstat-python, hello-go, hello-python, jsonflat-python, lru-cache-go, obs-01-parse, obs-02-count, obs-03-package, obs-05-report, obs-06-typehints, ratelimit-go, semver-go, smoke-chain-a, smoke-chain-b, vmreap-plan, wcl-python. The refusal contradicts the task format: `bench/README.md` and `runner/dark/tasks.py` both say `start/` is optional, while `runner/dark/export.py` lists it in `REQUIRED`. Not changed (another session edits `runner/dark/`); the acceptance below is over the 15 exported tasks.

## What the export lacks

The first oracle run used the exported directories as they are, with `Sample(sandbox=("docker", <dir>/Dockerfile))`, the hidden `tests/` and `run-tests.sh` copied in at scoring, and `solution.sh` run before it (log `2026-09-25T18-03-47-00-00_dark-tasks_EW5z2QPxtKVBLavvsV3aNK.eval`). Every `solution.sh` exited 0, and the scores were 6 CORRECT and 9 INCORRECT. The nine, and why (each read from the score's explanation):

| task | cause |
|---|---|
| errwrap-go, intervals-go, median-go, rename-package-go, rpn-go, snake-config-go | no `go.mod`: `go test ./...` prints `directory prefix . does not contain main module`, and `no-extra-files` counts 2 `.go` files where the template's `main.go` makes 3. The export copies `start/` only, not `templates/go/` |
| obs-04-tzfix | a chained task: its starting tree is the tree of steps 1 to 3 (`obs.py`, `observer/`), which the export does not carry: `No module named 'test_counts'` |
| obs-all | `root-files` expects `README.md` at the root, a template file the export omits |
| split-module-python | the acceptance reads `git rev-list --max-parents=0 HEAD` and `git show <root>:textutil.py`; the exported image holds no git repository |

`dark_tasks.py` therefore adds a setup step, `starting_tree`, that builds dark's starting tree with `dark.tasks.work_tree` and `oracle_tree` (read-only import from `runner/`, the way `bench/tools/check_tasks.py` does), copies it over `/app`, and makes one git commit. With that, a four-task run (median-go, obs-04-tzfix, obs-all, split-module-python) passed three and split-module-python still failed, on the same `numutil-unchanged` check. Cause: the exported `solution.sh` unpacks a tar as root and copies with `cp -a`, which gives `/app` and its files the uid of whoever packed the tar (1000 on this host); git then refuses `/app` (`detected dubious ownership`), the acceptance's `git` calls return nothing, and `numutil-unchanged` fails. The `oracle()` solver runs `chown -R root:root /app` after `solution.sh`. Both are workarounds in my file; the causes are in `runner/dark/export.py` (no template, no chain base, no git commit, ownership-preserving `solution.sh`). The exported `docker-compose.yaml` is not used: it names its one service `client` and reads `T_BENCH_*` variables Inspect does not supply, so the Dockerfile alone is the sandbox.

## Item 1: the oracle

Command (from the checkout root, `DARK_TASKS` is the dark-tasks checkout):

```
DARK_TASKS=/home/blt/main/projects/dark-tasks /tmp/fx/venv-inspect/bin/inspect eval bench/inspect/dark_tasks.py -T tasks_dir=/tmp/fx/tb --solver oracle --model mockllm/model --log-dir /tmp/fx/inspect-logs
```

Summary line: `accuracy 1.000`, `stderr 0.000`, total time 0:04:45. Per-sample table from `inspect log dump 2026-09-25T18-31-17-00-00_dark-tasks_PgsHpGsFwtKMU63aKPTiNf.eval` (status `success`, model `mockllm/model`, solver `oracle`; `answer` is the exit code of `run-tests.sh`, `oracle exit` is the exit code of `solution.sh` kept in the sample store):

| sample | score | answer | oracle exit |
|---|---|---|---|
| csvline-python | C | exit 0 | 0 |
| duration-python | C | exit 0 | 0 |
| errwrap-go | C | exit 0 | 0 |
| intervals-go | C | exit 0 | 0 |
| median-go | C | exit 0 | 0 |
| obs-04-tzfix | C | exit 0 | 0 |
| obs-all | C | exit 0 | 0 |
| rename-helpers-python | C | exit 0 | 0 |
| rename-package-go | C | exit 0 | 0 |
| roman-python | C | exit 0 | 0 |
| rpn-go | C | exit 0 | 0 |
| slugify-python | C | exit 0 | 0 |
| snake-config-go | C | exit 0 | 0 |
| split-module-python | C | exit 0 | 0 |
| typehints-python | C | exit 0 | 0 |

The same command was re-run on the committed file at `850b0a0` (log `2026-09-25T19-07-02-00-00_dark-tasks_ACT3J3gmoJXUtNRnDCDDcT.eval`): 15 samples, all `C`, `accuracy 1.000`, total time 0:04:34.

Negative control: `--solver generate --model mockllm/model` (no tools, the tree stays unsolved) on roman-python, median-go, split-module-python and obs-04-tzfix: all four `I`, accuracy 0.000 (log `2026-09-25T18-39-56-00-00_dark-tasks_TuQ594p4YKJEBtdoStHkEY.eval`). The hidden tests only enter the container at scoring: `run_tests()` copies `tests/` to `/tests` and `run-tests.sh` to `/run-tests.sh` then, and runs `TEST_DIR=/tests bash /run-tests.sh`.

## Item 2: a cheap model as the agent

Solver `react_bash` is `react(prompt=..., tools=[bash(timeout=180)])`, `react()` and not `basic_agent()`; it is the task's default solver. Command:

```
DARK_TASKS=/home/blt/main/projects/dark-tasks OPENAI_BASE_URL=http://192.168.1.206:11434/v1 OPENAI_API_KEY=unused /tmp/fx/venv-inspect/bin/inspect eval bench/inspect/dark_tasks.py -T tasks_dir=/tmp/fx/tb --model openai/dsf:latest --limit 2 --message-limit 30 --log-dir /tmp/fx/inspect-logs
```

The endpoint answered (its model list holds `dsf:latest`; a direct chat request returned a completion). The eval ran to completion, status `success`, total time 0:01:34, `openai/dsf:latest 21,942 tokens [I: 17,787, O: 4,155, R: 0]`, `accuracy 0.500`, `stderr 0.500`. Log `2026-09-25T18-50-15-00-00_dark-tasks_JE6jgufCxTqj8RonXqN23g.eval`. Scores from `inspect log dump`:

| sample | score | messages | bash calls | note |
|---|---|---|---|---|
| csvline-python | C | 9 | 3 | all hidden checks pass |
| duration-python | I | 9 | 3 | `CHECK hidden-rejects fail (1 accepted)`: it accepted `'1h\n'` (a `$` anchor lets a trailing newline through); the other five checks passed |

Neither sample reached the message limit; each ended when the model answered without a tool call after three bash calls. First three tool calls of the first sample, as recorded in the log's messages (`bash` arguments, cut at 220 characters, newlines shown as `\n`):

1. `bash`: `cd /app && ls -la && echo "---" && cat csvline.py && echo "---" && cat test_csvline.py && echo "---" && cat .dark/verify.sh 2>/dev/null`
2. `bash`: `cd /app && cat > csvline.py <<'EOF'\n"""split_line(line) -> list[str]: the fields of one CSV record.\n\nFields are separated by commas. A field is either unquoted (taken as\nis, spaces included, may be empty) or quoted: it s` (truncated)
3. `bash`: `cd /app && python3 -c "\nfrom csvline import split_line\ncases = {\n '': [''],\n 'a,b,': ['a','b',''],\n ',': ['',''],\n '\"\"': [''],\n '\"x,y\",z': ['x,y','z'],\n '\"a \"\"b\"\" c\",x': ['a \"b\" c','x'],\n 'a, b ,c': ['a',' b` (truncated)

Two samples say nothing about the model's ability; the run shows the path works end to end.

## What dark's own runner does for these tasks that this eval did not, and what Inspect gave for free

- Staging in a second machine: `stager.py` clones the pushed branch into a fresh VM, refuses a branch whose `.dark/` or `.gitea/` differ from main, runs `.dark/verify.sh`, and only then unpacks the hidden acceptance; here the tests ran in the container the agent worked in, with no tamper check and no verify gate (the exported `run-tests.sh` only runs `run.sh`).
- The envelope: `run.py` holds a run to its envelope (a live run over it ends `fail:budget`, a silent one `fail:structural`); here the only bounds were `--message-limit 30` and per-command timeouts, and a sample can only be `C` or `I`.
- The ledger row: dark writes `run.start`, `run.transition` and `run.end` lines to an fsynced JSONL ledger (`ledger.py`) with outcome, fail kind, seconds, calls, tokens and `checks_ok/checks_total`; the eval log holds a score and token totals, and the per-check `CHECK` lines only as text in the score explanation (last 2000 characters).
- The records repository: dark pushes a session's stream, task record and a sha256 to Gitea; here the `.eval` file on local disk is the only record, unhashed and unpushed.
- Also not done: the runner's nonce on the staging verdict, and the chain rule (a step starts from the tree the earlier step delivered when it passed); `starting_tree` always starts a chained task from the oracle tree of the earlier steps.
- Free from Inspect: the log (one `.eval` per run with every message, tool call, score and token count per sample, read here with `inspect log dump`), the sandbox lifecycle (one image built per sample at startup, containers started and removed; no `inspect` container or image was left afterward), and the per-sample sandbox declaration (one `("docker", <Dockerfile>)` per sample, no compose file written by me).

## Not found / not verified / blocked

- NOT FOUND: the mock model name `mockllm` is not on the providers page (or `tasks`, `models`, `options`); confirmed from the installed source and by runs instead.
- NOT VERIFIED: `inspect view` (the viewer) was not run; only `inspect log dump` was.
- NOT VERIFIED: the agent's ability beyond two samples (`--limit 2`, one of two CORRECT); the 13 other exported tasks were not run under the agent.
- NOT VERIFIED: the exported `docker-compose.yaml` under Harbor or Terminal-Bench (not used here).
- Defects seen, not fixed (files outside this task's scope): `runner/dark/export.py` refuses tasks with no `start/` although the task format makes it optional (16 of 31); it omits the language template, the chain base tree and a git commit; its `solution.sh` leaves `/app` owned by the packer's uid, which breaks git.
- Not done: no `CHANGELOG.md` line (the file was outside the named scope) and `cd runner && bash verify.sh` was not run; `bash runner/ops/no-binaries.sh` prints `no-binaries OK` (eval logs are under `/tmp/fx/inspect-logs`, none committed).
- Note: the README and this report name Inspect's own `OPENAI_API_KEY` variable because the `openai` provider requires it set; no variable of mine uses that word, and `dark_tasks.py` does not contain it.
- BLOCKED: NONE.

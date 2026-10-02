# Report: 1112-b, desktop tasks, their hidden checks, and the desktop command

Two brief items: tasks 3 and 4 of `specs/desktop-arm/tasks.md`. Both are
committed on this worktree's branch (`feat/desktop-arm`); nothing is pushed.

## Terms, once

- **desktop arm**: dark's way of checking a whole desktop system (a TV box
  image) with a model, the way the **user arm** checks a web page. The user
  arm is `runner/dark/user.py`, which puts one URL and a numbered list of
  steps to a model and drives a browser through them.
- **desktop task**: a `task.toml` of class `desktop`. Instead of a work repo
  it names an `image` and a **start script**, and it adds an optional list of
  **checks** to the numbered `steps` of a user task.
- **start script**: a shell script that sits beside the task's `task.toml`
  (the task names it with `start`). The runner copies it into the sandbox and
  the executor runs it as root before anything else; it brings up the graphical
  session and writes the **session file**.
- **session file**: `/run/dark-desktop-session`, one `NAME=value` line per
  variable of the running session: the user, the display, the runtime
  directory, and any compositor socket. The desktop driver reads it to reach
  the session.
- **check**: a command in the task, run by the executor in the sandbox after
  the steps and never shown to the model. Its exit status is its verdict
  (`0` is pass, anything else is fail); checks are the hidden tests of a
  desktop task.
- **start script environment**: the variables the start script wrote into the
  session file; the driver's `ExecChannel` puts them on every command it drops
  into the session.
- **desktop driver**: `DesktopPage` in `runner/dark/desktop.py`, the object
  that does to the desktop what the browser's `PlaywrightPage` does to a page:
  launch, look, act, screenshot.
- **desktop snapshot**: what the model sees instead of a page's accessibility
  tree: the list of windows, then the text read off the screen by optical
  character recognition (`tesseract`) from a `grim` frame.
- **lab sandbox**: the desktop image run as a docker container with a host
  device handed in with `--device`. A **render device** is such a host device,
  here `/dev/dri`, that the image needs to draw a frame.
- **envelope**: the calls and seconds budget one run of a class gets, from
  `runner/budgets.toml` and the ledger.
- **done tag**: the single `AGENT-DONE` comment the executor posts on the
  run's issue at the end, carrying a machine-readable `DARK:{...}` line the
  runner parses.
- **records**: the per-run directory the executor pushes to the shift's
  records repository: `steps.jsonl`, the `steps/` screenshots and trails,
  `checks.jsonl`, `stream.jsonl`, `brief.md` and `task.json`.
- **MCP server**: `runner/dark/mcp.py`, which runs dark subcommands as tools
  over stdin and stdout for a client such as a chat assistant.

## Item 3, desktop tasks and their hidden checks

Commit `f2cf287 feat(desktop): load desktop tasks and run their hidden checks`.

- `runner/dark/spec.py`: `DESKTOP_CLASSES = ("desktop",)` joined into
  `CLASSES` (now eight classes); `"checks" -> "fail:capability"` in
  `FAIL_KIND_OUTCOME`; `checks_ok` and `checks_total` added to the optional
  fields of the `done` tag in `AGENT_TAGS`.
- `runner/dark/tasks.py`: `_load_desktop_task` reads `image` (a non-empty
  string), `start` (the name of a file beside `task.toml`, whose text is read
  into the task), `spec` and `steps` as a user task has them, an optional list
  of `checks` (tables with a non-empty `id` and `command` and a positive
  numeric `timeout`, default 60), and an optional `live` table (`host` and
  `user`). A repeated check id, an unknown check field, a non-list `checks`,
  and any top-level key outside the desktop set are refused with the task
  path and the key, as the user loader refuses its own work-repo keys.
- `runner/budgets.toml`: `[class.desktop]` beside `[class.user]`, with the
  requested `seconds = 1800` (and the user class's other caps).
- `runner/dark/desktop.py`: `run_checks` runs each check as root with
  `sh -c` under its own timeout, and writes one `checks.jsonl` line per check
  (`id`, `command`, `rc`, `output` cut to 2000 characters, `verdict` pass or
  fail) and returns the tally `(ok, total)`. A timeout is recorded as `rc
  124`. `records_paths` lists `checks.jsonl` beside `steps.jsonl` and the
  `steps/` directory. `verdict(results, stop, checks_ok, checks_total)` turns
  the step results, the reason the step loop stopped, and the check tally into
  `(outcome, kind, detail)`: a failed step is `steps`, a failing check is
  `checks` even when every step passed, and the rest follow the user arm.
- `runner/tests/fixtures/desktop/tvbox/`: `task.toml` and `lab-start.sh` copied
  from the owner's TV box task
  (`selfhost` `config/tvbox/dark/`) and sanitised for the gate (below).

Acceptance, from `runner/`: `python3 -m unittest tests.test_tasks
tests.test_desktop -q` runs 78 tests and prints OK. The new tests are the
tvbox fixture loading, the required `image`/`start` and the bad-check
refusals, a passing and a failing check giving the right `checks.jsonl` lines
and tally, and a run whose steps all pass but one check fails ending `checks`.

## Item 4, the lab sandbox and the command

Commit `8276c79 feat(desktop): run a desktop task in a lab sandbox`.

- `runner/dark/config.py`: `Host.desktop_devices`, default `("/dev/dri",)`,
  from `DARK_DESKTOP_DEVICES` or `host.toml` (a comma-separated string is
  split).
- `runner/dark/docker.py`: `spawn` takes `devices` and adds one `--device`
  per entry.
- `runner/dark/run.py`: `Runner.desktop_task` builds the sandbox's
  `task.json` (`mode = "desktop"`, the image, the start script's text, the
  checks, the steps) and the spawn keywords: on the docker backend the task's
  own image and `desktop_devices`; `Runner.desktop` and `_desktop` mirror the
  user arm, and set the run's `checks_ok`/`checks_total` from the checks on
  the done tag (the steps tally is kept on the result as
  `steps_ok`/`steps_total`). A desktop task run through `Runner.run` is
  refused with "run it with `dark desktop`".
- `runner/dark/session.py`: `_main` hands `mode = "desktop"` to
  `desktop.main()`; the import is inside the function so this module stays
  stdlib-only for every other mode.
- `runner/dark/desktop.py`: `main` writes the start script to a file and runs
  it as root (a non-zero exit or a timeout ends the run with kind `env`),
  then takes the steps with `run_steps` through a `DesktopPage` over a real
  `ExecChannel`, then runs `run_checks`, then posts the done tag with both
  tallies. `model`, `page`, `channel`, `run` and the start-script path are
  injectable so a test needs no desktop.
- `runner/dark/__main__.py`: `dark desktop --task <toml|dir> --tier <id>
  [--target lab]`; `--target vm` and `--target live` are refused with a
  message naming tasks 5 and 6. It prints one JSON line: `run`, `outcome`,
  `fail_kind`, `detail`, `issue`, `records`, `steps_ok`, `steps_total`,
  `checks_ok`, `checks_total`.
- `runner/dark/mcp.py`: `dark_desktop` beside `dark_user`.

Acceptance, from `runner/`: `python3 -m unittest tests.test_run tests.test_desktop
-q -k desktop` runs 36 tests and prints OK. The new tests use the existing
fakes: the task.json and spawn keywords the sandbox is given, `desktop.main`
with a fake check runner and a fake desktop, a failing start script ending
`env`, and a failing check ending `checks`. `cd runner && bash verify.sh`
prints `verify OK`.

## The gate, base and after

- Base, before any change (`bash verify.sh` on `feat/desktop-arm` at
  `7842ffe`): `Ran 858 tests ... OK (skipped=2)`, then `verify OK`.
- After both items: `Ran 880 tests ... OK (skipped=2)`, then `verify OK`.

## Files changed beyond the brief's named list

The new class and the new MCP tool move three pinned tests, so they are in
the same commits rather than left red: `tests/test_spec.py` (the class
partition and its count), `tests/test_config.py` (the budget fixture gains
`[class.desktop]`), and `tests/test_mcp.py` (the tool-name list and the
argument set of `dark_desktop`). `tests/test_run.py` gains the desktop-arm
class. The four files the brief names by path (`tasks.py`, `spec.py`,
`budgets.toml`, `run.py`, `session.py`, `__main__.py`, `mcp.py`, and
`desktop.py`) carry the implementation; `config.py` and `docker.py` carry the
render device, which the item asks for by name but whose plumbing has no other
home.

## The shared tree

Another session (`1112-a`, its report in `reports/1112-a-desktop-driver.md`)
committed to this same worktree while this one ran. After item 3 was
committed, it committed `489e084 docs(reports): note the shared-tree gate
state for 1112-a` on top. This session's `git commit --amend` for the
fixture fix below then landed on that commit, not on item 3 (the reflog holds
its original, `abd88a5`). The other session's report content is unchanged;
the only addition folded into its commit is the two-line fixture fix to
`tests/fixtures/desktop/tvbox/`. Item 4 was amended on its own commit only.

## Out of scope, reported not fixed

- The copied TV box fixture carries a check with an `Authorization: Bearer`
  line and a private live address, and `/home/tv` paths; gitleaks and
  `no-private` both refuse them. The copy under `tests/fixtures/desktop/tvbox/`
  replaces the bearer value with `$LAB_AUTH`, the live host with
  `tvbox.example`, and the session user's home directory with a path outside
  `/home`. This is a change to the
  copied fixture, not to the owner's file.
- `dark_desktop` takes `task_toml` for parity with `dark_user`, but a desktop
  task's start script is a second file beside `task.toml`, so the text form
  cannot load. The `task` form (a directory on the server) is the one that
  works; the tool description says so.
- The shared task prompt in `ChatModel` still labels the snapshot `PAGE
  (accessibility snapshot):`. That was item 1's seam, already reported by
  `1112-a`; left alone.
- `desktop.records_paths` does not list a `README.md`, because the desktop
  arm writes none (the user arm's is browser-specific). The desktop records
  are `steps.jsonl`, `steps/` and `checks.jsonl`.
- `Runner.desktop(target="vm"|"live")` would still run on the lab sandbox;
  only `cmd_desktop` refuses the other targets. The command is the only
  entry point, so nothing reaches it that way.

## Not done / not verified / blocked

- The real run of `examples/desktop` on a docker host that task 4 also names
  was not done: no docker host is configured here and no example task exists
  yet (`examples/desktop/` is task 5's work as written in `plan.md`). The
  named test scope and the gate are the verification this item got.
- `--target vm` and `--target live` are refused on purpose, pending tasks 5
  and 6.

# Report: 1112-e, the live target and the words (tasks 6 and 7)

Two brief items: task 6 of `specs/desktop-arm/tasks.md` (a desktop task on
`--target live`) and task 7 (the words). Both are committed on this worktree's
branch `feat/desktop-arm`; nothing was pushed. The branch's other commits are
authored `majgull <9626688+majgull@users.noreply.github.com>`, and so are
these. Every command below ran in this worktree on 2026-10-02.

## Terms, once

- **desktop arm**: the part of dark that checks a whole desktop image the way
  the user arm checks a web page; it keeps the user arm's step loop, verdicts,
  quote rule, trail and records.
- **desktop task**: a `task.toml` of class `desktop` naming an image, a start
  script beside it, a spec, numbered steps, and optional hidden checks and
  live target.
- **desktop image**: an OCI image of a whole desktop system (compositor,
  shell, apps, units); the real machine runs the same image by digest.
- **start script**: the shell script beside a desktop task; run as root first,
  it brings up the graphical session and writes the session's environment to
  `/run/dark-desktop-session`, one `NAME=value` line each.
- **desktop driver**: the object that launches, looks, acts and screenshots on
  the desktop, as `PlaywrightPage` does on a page (`dark/desktop.py`'s
  `DesktopPage`).
- **desktop snapshot**: what the model sees: one line per window, then one
  `ocr:` line per line of text read off a frame.
- **check**: a hidden command in a desktop task, run as root after the steps,
  never shown to the model; its exit status is its verdict.
- **lab sandbox**: the image as a docker container with a render device; the
  `lab` target.
- **VM sandbox**: the image booted as a Proxmox virtual machine; the `vm`
  target.
- **live target**: a real machine running the image, named in the task, used
  after a deploy; the `live` target.
- **executor**: the program injected into the machine under test
  (`session.py` plus `desktop.py` and `user.py`); it runs the start script,
  the step loop and the checks, and posts its result to the run's issue.
- **task.json**: the one file the runner writes for the executor: the task,
  its steps and checks, the model endpoint and the records target.
- **records repo**: the Git repository a run pushes its kept transcript,
  screenshots and check lines to.
- **done tag**: the `AGENT-DONE` comment the executor posts when it is
  finished; the runner reads it off the issue and turns it into the outcome.
- **ssh probe**: a trivial `ssh <user>@<host> true`; if it does not answer,
  nothing is copied and the run is refused.
- **scp**: the standard file-copy over ssh.
- **mktemp**: the standard command that makes a fresh uniquely named
  directory; the live target makes one under the machine's `/tmp`.
- **sudo -n**: run a command as root without a password prompt; `-n` fails
  instead of asking.
- **detached**: started so it outlives the ssh call that started it
  (`nohup ... &` with its output redirected to a file).
- **preflight**: the state a run is in before anything is started; a refusal
  there is the repo's shape for a missing input.
- **refused**: the run outcome for that kind of missing input; it emits no
  ledger closing row, so it must happen from preflight, the only state with a
  declared `refused` row.
- **fakes**: in-process stand-ins the tests use instead of real machines;
  here, functions that replace `Runner.live_ssh` and `Runner.live_scp`.
- **gate**: `cd runner && bash verify.sh`, the one script that must print
  `verify OK`.
- **release pressure**: the gate step (`dark/pressure.py`) that fails when the
  `## [Unreleased]` section of `CHANGELOG.md` holds more than ten `- ` lines.

## Base state of the gate

Before any edit, from `runner/`, `bash verify.sh` printed:

```
Ran 887 tests in 487.939s
FAILED (errors=1, skipped=2)
```

`exit=1`. The one error was `test_run.DesktopVmCommand.
test_the_template_comes_from_the_table`: `DesktopVmCommand.fake_make() got an
unexpected keyword argument 'backend'`. The tip commit `40b0de9` had added
that keyword to `sandbox.make` but left the test's stand-in behind. Because
the gate stops at its first failure, its config, pressure, binary, private and
leaks steps did not run in that base invocation; run on their own,
`python3 -m dark.pressure ../CHANGELOG.md` printed the warning for **10**
Unreleased entries and exited 0.

## Item 1, `--target live` (task 6)

Commit `d60dd6e feat(desktop): run a desktop task on the live target`, on top
of the base repair `db544bc fix(tests): the VM command's fake takes the
backend make is handed`.

The base repair first: it changes one line of `runner/tests/test_run.py`, the
`fake_make` stand-in, so it takes the `backend` keyword `sandbox.make` already
takes. Without it the gate cannot print `verify OK` at all, which this brief's
item 2 requires, so it is reported here rather than only noted: it is
pre-existing breakage from `40b0de9`, not a problem this work created.

### What the live target does

`dark desktop --task <task.toml> --tier <id> --target live` reaches the
machine the task's `[live]` table names (`host`, `user`). No sandbox is
spawned, and the run uses no compute plane:

1. A preflight ssh probe runs `true` on the machine. No `[live]` table, or a
   probe that does not answer, ends the run `refused` before the run's issue
   exists and before any file is copied.
2. The runner copies `session.py`, `desktop.py`, `user.py`, `task.json` and
   the start script (as `start.sh`) into a fresh `mktemp -d
   /tmp/dark-desktop.XXXXXX` directory on that machine, in one `scp`.
3. It starts the executor there, detached and as root:
   `nohup sudo -n env DARK_TASK=<dir>/task.json
   DARK_RECORDS=<dir>/records DARK_RECORDS_WORK=<dir>/records-work
   python3 <dir>/session.py > <dir>/desktop.log 2>&1 &`.
4. It watches the issue for the done tag exactly as the lab and VM targets do.
   The executor pushes its records to the records repo and its result to the
   issue; that is how the records come back.
5. A `finally` removes the remote directory (`rm -rf`) and the local staging
   directory.

Nothing is built, rebooted or installed on that machine. The start script
runs there as root and writes the machine's own `/run/dark-desktop-session`,
so `ExecChannel` drops the steps to that session's user; the checks run as
root, because the executor does.

### Files changed

- `runner/dark/run.py`: the ssh/scp helpers and their bounds
  (`LIVE_OPTIONS`, `LIVE_PROBE_SECONDS`, `LIVE_COPY_SECONDS`,
  `LIVE_CLEAN_SECONDS`), the fresh-directory template `LIVE_TMP` and its
  strict validator `LIVE_TMP_RE`; `Runner.live_ssh` and `Runner.live_scp`
  (the seams tests replace); the preflight refusal/probe in `_desktop`; the
  `target == "live"` branch; the new `_desktop_live`; and the spawn keywords
  now keyed on `target == "lab"`, because live spawns nothing. Docstrings for
  `desktop_task` and `desktop` name the third target.
- `runner/dark/__main__.py`: `cmd_desktop` refuses a `--target live` run whose
  task has no `[live]` table with one line and exit 2, and skips the
  reachability/wake question for the compute plane, which a live run does not
  use. The `--target` help text and the docstring say live is built.
- `runner/tests/test_run.py`, `runner/tests/test_desktop.py`: the tests below.

### Tests and what each proves

| Test | Proves |
|---|---|
| `test_run.DesktopLive.test_live_without_a_table_is_refused` | no `[live]` table ends `refused`, and nothing was copied (`r.scp == []`) |
| `test_run.DesktopLive.test_live_ssh_unanswered_is_refused_before_any_copy` | a probe that returns 255 ends `refused`, `r.scp == []`, and only the probe command ran |
| `test_run.DesktopLive.test_live_copies_runs_and_removes_the_directory` | the exact five copied files and the exact scp destination, the exact mktemp and run commands, no sandbox (`r.launched == []`), `rm -rf` of the remote directory, and the local staging directory gone |
| `test_run.DesktopLive.test_live_task_json_asks_for_no_sandbox` | `desktop_task(..., target="live")` returns the class alone as spawn keywords (no image, no render device) |
| `test_run.DesktopVmCommand.test_a_live_run_without_a_live_table_is_refused` | the command exits 2 with one line naming the missing table, before `Runner.desktop` |
| `test_desktop.DesktopExecutor.test_desktop_main_runs_the_start_script_for_a_live_target` | the executor runs the copied task.json's start script first for a live target, as it does for lab and vm |

The named acceptance scope ran green:

```
python3 -m unittest tests.test_run tests.test_desktop -q -k live
Ran 9 tests in 6.845s
OK
```

No test opens a network connection: each one replaces `Runner.live_ssh` and
`Runner.live_scp` with a function that records the call and replays an answer.

### Decisions worth naming

- **The refusals happen in preflight.** `spec.py` declares a `refused`
  transition only from `preflight`, and `_Run.refuse` emits no closing ledger
  row, so a refusal after the run's issue exists ("ready") would leave a
  refusal with no record. Both the missing table and the unanswered probe are
  therefore checked before `_issue_meta`/`_create_issue`. The command repeats
  the missing-table check for a one-line, exit-2 config miss.
- **A detached executor and the existing issue watch.** Running the executor
  synchronously over ssh would have lost the watchdog, the envelope deadline
  and the abort marker, which all live in `_watch_executor`. Starting it
  detached keeps one code path for the done tag across lab, vm and live. The
  cost is that a `sudo -n` that fails has already returned 0 to the ssh call;
  the run then goes silent and is ended by the watchdog, not by the start.
- **The strict `mktemp` check.** The directory name comes back from the
  machine and is interpolated into later shell commands, so it is validated
  against `/tmp/dark-desktop.<alnum>` before use; a machine that answers with
  anything else ends the run `fail:structural` before a copy.
- **The copied start script is redundant on purpose.** The executor
  re-materialises the start text from `task.json` and runs it from
  `/opt/dark-desktop-start.sh`, so the copied `start.sh` is never executed.
  It is copied because the brief names it and because the machine then holds
  the same task inputs a sandbox would. The alternative — teaching the
  executor to run the copied file — would have changed `desktop.py`, which
  no acceptance clause asked for.
- **A live run needs no compute plane.** `cmd_desktop` still builds the
  configured backend for the `Runner`, but a live run never touches it, so
  the command no longer asks whether the Proxmox host is reachable or wakes
  it. The cost is named under "Out of scope" below.

## Item 2, the words (task 7)

Commits `51711c7 docs(changelog): fold the run-state board into one line` and
`23cdebc docs(desktop): name the desktop arm's terms and command`.

- `docs/concepts.md` gains nine entries in the spec's own words: desktop task,
  desktop image, start script, desktop driver, desktop snapshot, check, lab
  sandbox, VM sandbox and live target. They sit after the user arm's `step
  verdict` and before the long arm, because the desktop arm is the user arm's
  sibling.
- `README.md` gains one line beside the `dark user` line:
  `dark desktop --task <task.toml> --tier <id> --target lab`.
- `CHANGELOG.md` gains one `### Added` line for the desktop arm under
  `## [Unreleased]`.

The fold deserves its own paragraph, because it is a change to lines this
brief did not ask for. The Unreleased section already held **10** lines, the
gate's ceiling, so the desktop line was the eleventh and
`dark/pressure.py` failed the gate (`RELEASE PRESSURE FAIL: 11 ...`). Cutting
a patch release is the gate's own advice but needs `pyproject.toml`'s version
and a new `## [x.y.z]` heading, and this brief names neither file. The repo's
own convention when the cap is reached is to fold related lines; commit
`dfa9353` on this same branch did exactly that. `dark now` and the pinned
`Now` issue are one feature, the run-state board, so their two lines became
one with every sentence kept, and the desktop line then fitted at 10. The
fold is a separate commit so the words commit stays readable.

Every acceptance clause and where it is proven:

| Clause | Command / result |
|---|---|
| concepts defines the terms | `grep -c -E 'desktop (task\|driver\|snapshot)' ../docs/concepts.md` prints **7** (3 or more) |
| the gate prints `verify OK` | from `runner/`: `bash verify.sh` ended `verify OK`, exit 0 |
| README shows one command | the one `dark desktop ... --target lab` line above |
| CHANGELOG has the line | one desktop-arm bullet under `## [Unreleased]` `### Added` |

The full gate, on the committed words:

```
== tests
Ran 893 tests in 290.994s
OK (skipped=2)
== config
config OK: 2 models across 1 providers, 8 classes, windows []; host org dark, state <state dir>
== release pressure
release pressure warning: 10 Unreleased changelog entries, over 5; a patch release is due
== no binaries
no-binaries OK
== no private
no-private OK
== no credentials
leaks OK
== no /tmp in tests
verify OK
```

893 tests, 6 more than the base's 887, none removed. The two the base
reported skipped are still skipped.

## Out of scope, reported not fixed

- **`tasks.md` boxes 6 and 7 are still unticked.** `specs/desktop-arm/
  tasks.md` is not among the files this brief names, so it was not edited.
  The work each box describes is in and green; a person should tick them.
- **No real live run was made.** Spec requirement 7 records that the first
  user makes one real run; this brief says fakes only and no real ssh, so no
  machine was touched. The live path is covered by tests, not by a deploy.
- **`examples/desktop/` is still absent from this worktree.** `plan.md` names
  it and tasks.md item 4's acceptance runs it; the directory has never been in
  this tree (task 5's report says the same). Item 4's real lab run is likewise
  unrecorded here.
- **A live run no longer wakes the compute plane.** `cmd_desktop` skips the
  `reachable()`/`wake()` step for `--target live` because that run uses no
  sandbox. If the model endpoint is a local provider that sleeps, nothing
  wakes it before the run, and its first call fails as `fail:llm` rather than
  the command waiting. The alternative — keeping the wake — would make a live
  run depend on the Proxmox host answering, which is worse.
- **The `sudo -n` failure is not distinguished from a slow start.** A machine
  where the runner's user has no passwordless `sudo` returns 0 to the ssh
  call (the command was backgrounded) and then goes silent; the run ends
  `fail:structural`/`silent` after the watchdog, and the reason is in
  `<remote>/desktop.log`, which the runner does not copy back. Copying the log
  on a doomed run is a plausible next task.
- **The release pressure cap is itself at its ceiling.** The fold kept the
  gate green, but the next user-visible change will hit the same wall; the
  honest fix is a patch release, which needs `pyproject.toml`.

## Commits on `feat/desktop-arm` (this session)

```
23cdebc docs(desktop): name the desktop arm's terms and command
51711c7 docs(changelog): fold the run-state board into one line
d60dd6e feat(desktop): run a desktop task on the live target
db544bc fix(tests): the VM command's fake takes the backend make is handed
```

All authored as the branch's other commits. Not pushed.

# Report: 1112-a, the desktop arm's step-loop seam and desktop driver

Two brief items: tasks 1 and 2 of `specs/desktop-arm/tasks.md`. Both are
committed on `feat/desktop-arm`; nothing is pushed.

## Terms, once

- **desktop arm**: the way dark checks a whole desktop system (a TV box
  image) with a model, the way the user arm checks a web page.
- **user arm**: `runner/dark/user.py`, the executor that puts one URL and a
  numbered list of steps to a model and drives a browser through them.
- **step loop**: `run_steps` in `user.py`, the one loop both arms use: it asks
  the model for the next action, does it, and takes a verdict per step.
- **verdict**: the model's answer that ends a step, pass, fail or
  inconclusive; a run passes only when every step passes.
- **quote rule**: a pass verdict must quote text that was in the snapshot it
  was shown; a quote not found is refused and the model is asked again.
- **system prompt**: the fixed instruction text the model is given before the
  task prompt.
- **action target**: the short name the trail gives to what an action acted on
  (`button "Like"`, a URL, a key).
- **trail**: `<records>/steps/NN.trail.jsonl`, one line per action in a step.
- **driver**: the object that does the acting and the looking; the browser's
  is `PlaywrightPage`, the desktop's is `DesktopPage`.
- **desktop snapshot**: what the model sees instead of a page's accessibility
  tree: one line per window, then the text read off the screen.
- **window line**: one line of a desktop snapshot naming a window's id,
  application id, title, workspace or tag, and whether it has focus.
- **OCR line**: one line of a desktop snapshot beginning `ocr:`, read from a
  screenshot by the optical-character-recognition program `tesseract`.
- **compositor**: the program that draws and manages the windows (here mango
  or niri); it is asked for its window list and told to focus one.
- **compositor table**: the small per-compositor map in `desktop.py` from a
  compositor's name to the command that lists its windows, the function that
  reads that reply, and the command that focuses a window.
- **session file**: `/run/dark-desktop-session`, the file a desktop task's
  start script writes: the session's environment and the user it runs as.
- **channel**: a callable that runs one command list and returns its exit code
  and its output; `ExecChannel` is the real one inside the sandbox.
- **setpriv**: the util-linux program that switches a process to another user
  and group without going through PAM; used instead of `runuser`, whose PAM
  limits can fail in a container.
- **action set**: the actions the desktop model may ask for: `launch`, `key`,
  `type`, `click`, `focus`, `wait`.
- **`ydotool`**: the uinput tool that moves the pointer and clicks; the
  desktop refuses a `click` when it is not on PATH.

## Item 1, the step loop takes its prompt and target names from the page

- `runner/dark/user.py`: `ChatModel.next_action` now takes the system prompt
  as a keyword argument `system`, defaulting to the browser `SYSTEM`; it is
  passed to `_complete` in place of the hard-coded constant. `run_steps` reads
  `system = getattr(page, "SYSTEM", None)` and
  `target_of = getattr(page, "action_target", action_target)`, passes `system`
  to the model only when the page offers one, and uses `target_of(action)` for
  both the trail's `target` and a stuck step's note. A page that offers neither
  — every browser page and every existing fake — keeps the old call and the
  old target function, so the browser path is unchanged.
- `runner/tests/test_user.py`: new `PromptPage` (a fake page with its own
  `SYSTEM` and its own `action_target`) and `PromptModel` (a scripted model
  that records the system prompt of every call), and one new test
  `test_a_page_that_brings_its_own_prompt_and_target_names_uses_them`. It
  drives one step through `run_steps` and checks the model was given the
  page's prompt on both calls and the trail's target came from the page's
  `action_target`. Every existing test is untouched and still passes.
- Scope run: `cd runner && python3 -m unittest tests.test_user -q` prints
  OK (88 tests).
- Commit `ed5d22d feat(user): take the prompt and target names from the page`.

## Item 2, the desktop driver

- New `runner/dark/desktop.py`, standard library only:
  - `read_env_file` reads the session file into a dict, skipping comments and
    blanks and stripping quotes.
  - `ExecChannel(env_file, timeout)` is the real channel: it runs
    `setpriv --reuid <uid> --regid <gid> --init-groups -- <command>` with the
    session file's variables in the environment, and returns `(rc, output)`.
    The user comes from `DARK_UID`/`DARK_GID`, or `DARK_USER`/`USER` looked up
    in the password database.
  - `DesktopPage(channel, compositor, which=shutil.which)` implements the page
    interface `run_steps` expects: `goto` launches the task's first
    application detached, `url` names the focused window, `snapshot` lists the
    windows then appends `ocr:` lines from `tesseract <png> -` on a `grim`
    frame, `act` runs the action set, `screenshot` is `grim`, `close` does
    nothing, and `drain_requests` returns `[]`. It carries its own `SYSTEM`
    (the action set above, with the snapshot and quote rule explained) and its
    own `action_target` naming each action for the trail.
  - The compositor table: mango (`mmsg get all-clients`, whose reply is
    `{"clients": [...]}`, focus with `mmsg dispatch focusid client,<id>`) and
    niri (`niri msg --json windows`, focus with
    `niri msg action focus-window --id <id>`). Keys and text go through
    `wtype` (`ctrl+c` becomes `wtype -M ctrl -k c`); a click moves the pointer
    with `ydotool mousemove --absolute <x> <y>` and then
    `ydotool click 0xC0`, and is refused with an error the model sees when
    `ydotool` is absent. A failed command's output is put in the error too.
- New `runner/tests/test_desktop.py` against a `FakeChannel` that records
  every command list and replays one canned output per program (and can write
  a stand-in PNG for `grim`): one test per action (launch, key, type, click,
  focus, wait), a refused `click` that records no `ydotool` command, a
  snapshot with window lines and OCR lines, niri's own field names, a click
  test, and three tests through `run_steps` — a pass quoting a window title
  accepted, a pass quoting an invented title refused (the refusal is visible
  in the next call's history), and three steps giving three verdicts and three
  screenshots. Twenty tests in all.
- Scope run: `cd runner && python3 -m unittest tests.test_desktop
  tests.test_user -q` prints OK (108 tests).
- Commit `8b499fb feat(desktop): add the desktop driver`.

Two fix commits landed on top of item 2, from another session working in this
same worktree after a real run of the TV box image. I did not make them and
left them in place; they are part of the tree this report describes:

- `bca4ac8 fix(desktop): grab frames where the session user can write`: `grim`
  runs as the session user while the executor runs as root and owns the
  scratch and records directories, so a frame written into either was not
  writable. Both `snapshot` and `screenshot` now write into a
  world-writable scratch directory, and `screenshot` copies the frame into the
  records path.
- `7842ffe fix(desktop): launch applications detached`: `gtk-launch` waits for
  the program it starts, and a desktop application runs as long as the
  session (foot hit the 30 s channel timeout on the TV box image), so `goto`
  and `launch` now run `sh -c 'gtk-launch "$1" >/dev/null 2>&1 &'`.

## The session file name, a conflict with the brief

The brief names `/run/dark-desktop.env` as the file `ExecChannel` reads. A
commit that landed in this worktree after the brief was written,
`de1ccb9 docs(specs): name the session file /run/dark-desktop-session`,
renamed that file in both `spec.md` and `plan.md` to
`/run/dark-desktop-session`, because a dot-env file name is read as a secret
by the agent guards on the first user's host. I followed the spec and plan and
set `ENV_FILE = "/run/dark-desktop-session"`; the name change is folded into
the item-2 commit, whose body says so. If the brief's older name was intended,
this is the one place to change.

## The gate, base and after

- Base branch (`de1ccb9`, before either item): `cd runner && bash verify.sh`
  printed `verify OK`, `Ran 837 tests`, `OK (skipped=2)`.
- After both items and the two fix commits above: `cd runner && bash verify.sh`
  prints `verify OK`; the test step runs `Ran 858 tests`, `OK (skipped=2)`
  with the gate's `PYTHONWARNINGS=ignore::ResourceWarning`. Twenty-one tests
  were added (one to `tests.test_user`, twenty to `tests.test_desktop`).
- A bare `python3 -m unittest discover -s tests -t .` shows two failures in
  `tests.test_cli_ledger_tail` (a malformed-line note and an empty-ledger run
  capture stderr). They are not from this change: the captured stderr holds
  Python `ResourceWarning` text about unclosed sockets, which the gate
  suppresses with `PYTHONWARNINGS=ignore::ResourceWarning`. No file this task
  touched is in that test.
- After this report was written, another session working in the same worktree
  committed `f2cf287 feat(desktop): load desktop tasks and run their hidden
  checks` and left uncommitted edits to `runner/dark/desktop.py`,
  `config.py`, `docker.py` and `session.py`, and added
  `runner/tests/fixtures/desktop/tvbox/`. On that moving tree `bash verify.sh`
  no longer prints `verify OK`: it stops in `no-private` on
  `runner/tests/fixtures/desktop/tvbox/lab-start.sh:32` and `:36` and
  `task.toml:57`, whose `/home/user` lines the check's `/home/<name>/`
  pattern reads through to a later `/`. That file belongs to another session's
  item 3, is outside this brief, and is left untouched. The named scope for
  this task, `python3 -m unittest tests.test_desktop tests.test_user -q`, still
  prints OK on the shared tree (115 tests, the extra ones added by that
  session).

## Out of scope, reported not fixed

- `ChatModel`'s shared task prompt still labels the snapshot `PAGE
  (accessibility snapshot):`. Item 1 moved only the system prompt; the
  desktop `SYSTEM` explains the real snapshot, and renaming the shared label
  is a later item if wanted.
- `DesktopPage` has no `main`, no `run_checks` and no desktop task loader yet;
  those are tasks 3 and 4 of `specs/desktop-arm/tasks.md`.
- The `goto(url)` rule is "a non-empty url is a desktop entry id to launch";
  the real rule belongs to the desktop task loader in task 3.
- The `no-private` failures on `runner/tests/fixtures/desktop/tvbox/` are the
  other session's item 3, not this task; reported, not fixed.

## Not done / not verified / blocked

The two item commits were each green (`cd runner && bash verify.sh` printed
`verify OK`) on their own trees. The full gate cannot be reproduced now: the
shared tree carries another session's in-progress item 3, which fails
`no-private` as above, and I must not edit that session's files. My own
acceptance scope passes on the shared tree.

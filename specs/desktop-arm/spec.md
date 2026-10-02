# Spec: the desktop arm

This is the spec for one feature. A spec says what to build and why; its plan, `plan.md`, says how, and its tasks, `tasks.md`, is the ordered checklist. Read `AGENTS.md`, `memory/constitution.md` and `specs/user-arm/spec.md` first: the desktop arm is the user arm with a desktop instead of a browser, and keeps its verdicts, its quote rule, its trail and its records.

## Terms

- **desktop task**: a `task.toml` of class `desktop` with an `image`, a `start` script, a `spec`, `steps` and optional `checks`.
- **desktop image**: an OCI image of a whole desktop system (for example a bootc image of a TV box: compositor, shell, apps, units). The same image, by digest, is what the real machine runs.
- **start script**: a shell script beside the task file that the runner copies into the sandbox and runs as root before anything else; it brings up a graphical session for one user and writes the session's environment (`WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, the user, and any compositor socket) to `/run/dark-desktop-session` (one `NAME=value` per line; not a dot-env name, which the agent guards on the first user's host read as a secret).
- **desktop driver**: the object that does to the desktop what `PlaywrightPage` does to a page: launch, look, act, screenshot. It runs inside the sandbox, as the session's user, with the session's environment.
- **desktop snapshot**: what the model sees instead of an accessibility tree: the list of windows (application id, title, workspace, which one has focus), then the text read off the screen (OCR), one line each, cut like a page snapshot.
- **check**: a command in the task, run by the executor in the sandbox after the steps, never shown to the model; its exit status is its verdict. Checks are the hidden tests of a desktop task.
- **lab sandbox**: the desktop image run as a container with a render device and a headless compositor output. **VM sandbox**: the same image booted as a virtual machine (systemd, login, timers, reboot). **live target**: a real machine running the image, named in the task, used only after a deploy.

## Purpose

A desktop system built as an image is changed often and used by people who never read its configuration. The desktop arm proves each image the way the user arm proves a web application: a model does numbered steps like a person and quotes what the screen shows, and hidden checks prove what a person cannot see (a unit running, a file in place, a webhook not sent). The same task runs against the lab sandbox, the VM sandbox and the live target, so one image gets one set of verdicts at each stage, with screenshots, in the records and on the run's issue.

The first user is the TV box of the owner's house (`selfhost` `config/tvbox`): its task set lives in that repository, not in dark.

## The user

The **operator** writes desktop tasks beside the image they build, runs `dark desktop --task <task.toml> --tier <id> [--target lab|vm|live]`, and reads the outcome line, `steps.jsonl`, `checks.jsonl` and the screenshots. A deploy script may refuse to publish an image whose lab or VM run did not pass. The **model** inside the sandbox sees the task's `spec`, one step at a time, the desktop snapshot and the notes of earlier steps; it has no source and no shell.

## Requirements

Each ends with the check that proves it.

1. **One step loop.** The desktop arm uses the user arm's `run_steps` unchanged in behaviour: the same three verdicts, quote rule, stuck rule, trail and `steps.jsonl`; only the driver, its action set and its system prompt differ. Check: the user arm's tests pass unchanged, and `tests/test_desktop.py` runs three steps through a fake desktop and gets three verdicts and three screenshots.
2. **Actions a person has.** The desktop action set is `launch` (an application by its desktop entry id), `key` (a key or chord), `type` (text), `click` (at screen coordinates), `focus` (a window from the snapshot), and `wait`; a driver that cannot do one (a container has no pointer device) refuses it with an error the model sees, never silently. Check: a test per action against the fake exec channel, and a refused `click` reaches the model as an error.
3. **What the model sees is quotable.** The desktop snapshot lists windows and OCR lines; a pass quotes them under the quote rule. Check: a test where a quoted window title passes and an invented one is refused.
4. **Hidden checks.** `checks` run after the steps, as root, each with a timeout; each writes one `checks.jsonl` line (`id`, `command`, `rc`, `output` cut, `verdict`); any failing check makes the run fail with kind `checks`, also when every step passed. A check may name its `targets` (a list of `lab`, `vm`, `live`; default all three): a check that changes the machine (turns a screen off, posts to a service) names `lab` and `vm` only, and on another target it is written as `skipped` and counted nowhere. Check: tests for a passing and a failing check and the outcome mapping.
5. **The lab sandbox.** On the docker backend, a desktop task's sandbox is its `image` with the host's render device, the start script runs first, and the executor runs as the session's user. Check: a run of the bundled example task (a small desktop image) on a host with docker passes, and its records hold screenshots that show the session.
6. **The VM sandbox.** On the Proxmox backend, the image is booted as a VM from a disk built from it (bootc's image builder), with the start script reduced to waiting for the session; reboot is an action of checks only. Check: the same example task passes on the VM sandbox.
7. **The live target.** `--target live` runs against a machine named in the task (`live.host`) over ssh, with the same driver; it never changes the machine beyond what the steps and checks do, and refuses if the task has no `live` table. Check: a test that `--target live` without `live` is refused; one real run is recorded by the first user.
8. **Records like the user arm.** `steps.jsonl`, `steps/<NN>.png`, `steps/<NN>.trail.jsonl`, `checks.jsonl`, `stream.jsonl`, the `AGENT-DONE` tag with `outcome`, `kind`, `steps_ok`, `steps_total`, `checks_ok`, `checks_total`; the run issue and the presentation read them with no desktop-specific code beyond the new files. Check: tests on the tag and on `records_paths`.
9. **Words.** `docs/concepts.md` defines desktop task, desktop driver, desktop snapshot, check and the three sandboxes; `README.md` shows one command; `CHANGELOG.md` has the line. Check: `grep` for each term in `docs/concepts.md`.

## Not in scope

Driving a desktop the model has not been given an image for; Windows or macOS; pixel-exact visual comparison (screenshots are evidence for people, the quote rule and checks are the verdicts).

# Spec: the critic

This is the spec for one feature. A spec says what to build and why; its plan, `plan.md`, says how, and its tasks, `tasks.md`, is the ordered checklist. Read `AGENTS.md`, `memory/constitution.md` and `specs/user-arm/spec.md` first. This spec covers the checks alone; the command that runs them and the demo that refuses to cut on them are in the second half of the same feature.

## Terms

- **records**: one run's directory as an arm writes it, `task.json` (the task and its numbered steps), `steps.jsonl` (one verdict and note per step), `steps/<NN>.png` (the screenshot taken when that step's verdict was given) and, when a person opens it, `README.md`, whose first line under the title is the outcome as a badge.
- **critic**: `runner/dark/critic.py`, the named check that reads one records directory and returns its findings. It is not the demo, and it does not run the steps again: it reads what the run left.
- **finding**: one thing wrong in a records directory, `{rule, step, detail}`: `rule` is the name of the check below, `step` is the numbered step it concerns (null when it is about the run as a whole), and `detail` is one sentence a person can act on.

## Purpose

`dark demo` cuts a video from whatever a run left behind, so a records directory that is missing a screenshot, or whose verdict contradicts its own note, reaches the viewer as a demo that looks complete. The critic is a named check, separate from re-planning and separate from the demo, that inspects the artefact before a person sees it and says what is wrong, in one line per finding. Findings are not a model's opinion: every rule is decided by the files themselves, and the same records give the same findings every time.

## The rules

Each rule is one sentence, checked by `critic.check(records_dir)`, and named by the `rule` field of a finding.

1. **`steps`** — every numbered step in `task.json` has exactly one verdict in `steps.jsonl`, and every verdict in `steps.jsonl` belongs to a numbered step.
2. **`screenshot`** — every verdict has its `steps/<NN>.png`, and that PNG is an image with more than one colour in it.
3. **`note`** — a step whose verdict is `pass` has no note saying it failed, and a step whose verdict is `fail` has no note saying it passed; the whole word list is `fail`, `failed`, `fails`, `failure`, `broken` against a pass, and `pass`, `passed`, `passes`, `works`, `worked`, `success` against a fail, whole words only.
4. **`outcome`** — the outcome the records claim, the badge word (`PASS`, `FAIL` or `INCONCLUSIVE`) in `README.md`, is the one the verdicts imply: `pass` only when every step passed, `fail` when a step failed, and `inconclusive` otherwise; a records directory with no `README.md`, or one whose badge cannot be read, makes no claim and the rule does not apply.

A clean records directory returns no findings. A records directory the rules cannot read at all — no `task.json`, no `steps.jsonl` — returns the finding that says so, never an exception.

## Out of scope

- A model judge: the critic never asks a model whether a verdict is right, and never judges the page, the screenshot's content, or the run's quality. Its rules are about the records being internally consistent, so a run that is wrong but honestly recorded passes the critic.
- Re-running a step, re-taking a screenshot, or editing any record: the critic reads and reports; the arm and the runner own the records.
- A missing or malformed `stream.jsonl`, `trace.zip` or `video.webm`: the demo already reports those when it cannot cut.
- The desktop arm's `checks.jsonl`: the rules read the steps every arm keeps, and the hidden checks are the runner's verdict, not the critic's.

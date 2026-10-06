# Report: 1143-a, a critic pass that checks a run's records before a person sees its demo

Date: 2026-10-06.

Branch: `wt/dark-code-writer-t1143-a-critic-pass`, two commits, one per item, then one small fix commit and this report, on top of `9945408 feat(desktop): add the desktop arm`.

## Terms, once

- **critic**: `runner/dark/critic.py`. A plain function, `critic.check(records_dir)`, that reads one run's records directory and returns everything wrong with it. It does not ask a model anything, does not look at the page, and does not re-run a step; it compares the files against each other. It is the named check, separate from re-planning and separate from `dark demo`, that a person does not have to remember to run.
- **finding**: one thing the critic found, a `Finding` (a `dict`) with three keys: `rule` (which check below), `step` (the numbered step it concerns, `None` when it is about the run as a whole) and `detail` (one sentence). The keys are also attributes (`f.rule`, `f.step`, `f.detail`).
- **claimed outcome**: the outcome the records themselves state to a reader, the badge word `PASS`, `FAIL` or `INCONCLUSIVE` on the `README.md` the arm writes first (`runner/dark/user.py:write_readme`). It is the only outcome the records carry; `task.json` does not hold one, and the ledger's `run.end` row is not part of the records.

## Why

`dark demo` cuts a video from whatever a run left behind, so a records directory missing a screenshot, or carrying a verdict whose own note says the opposite, reaches a viewer as a demo that looks complete. A critic pass makes that a named check on the artefact, before the cut, in the same spirit as the extra inspection pass in ChatDev and MetaGPT; the brief forked it out of thread 1143.

## Item 1, the checks

Commit `735325b feat(critic): check a run's records before its demo`.

Files changed:

- `runner/dark/critic.py` (new, ~300 lines): `check(records_dir) -> list[Finding]`, `line(finding)` for the one-line form, `verdict_outcome(verdicts)`, `claimed_outcome(records_dir)`, and `png_is_flat(path)`, a stdlib PNG reader (IHDR/IDAT, filters 0-4, colour types 0/2/3/4/6, 8-bit, non-interlaced) that says whether every pixel is one colour. The `steps` rule treats a verdict whose `step` is not a number in range as its own finding, so a corrupt `steps.jsonl` never raises. The four rules, in the order the findings come back:
  1. `steps`: every numbered step in `task.json` has exactly one verdict in `steps.jsonl`, and every verdict belongs to a numbered step.
  2. `screenshot`: every verdict has its `steps/<NN>.png`, and that PNG has more than one colour.
  3. `note`: `pass` against the fail words `fail`, `failed`, `fails`, `failure`, `broken`; `fail` against the pass words `pass`, `passed`, `passes`, `works`, `worked`, `success`; whole words only, so `Password` is not a `pass`.
  4. `outcome`: the badge in `README.md` equals the outcome the verdicts imply (`pass` iff every step passed; `fail` when a step failed; else `inconclusive`); no `README.md`, or no badge in it, is no claim and the rule does not apply.
- `runner/tests/test_critic.py` (new, unit checks at this commit): one clean fixture with no findings, and one fixture per rule that gives exactly that finding, missing, duplicate and extra verdicts; a missing, a flat and a not-a-PNG screenshot; a pass note saying failed and a fail note saying passed; a `PASS` claim over a failed step and a `FAIL` claim over all-passing steps; plus no-README, no-steps and no-`task.json`, and the PNG reader (flat, gradient, a Sub-filtered flat frame, a non-PNG).
- `runner/tests/pngfix.py` (new, 31 lines): builds an RGB PNG in memory. The fixtures are generated, not stored, because `runner/ops/no-binaries.sh` (run by the gate) refuses a tracked file that is not text, so a checked-in PNG could not pass the gate.
- `specs/critic/spec.md` (new): terms, purpose, each rule in one sentence, and what is out of scope. A model judge is the first thing out of scope.
- `CHANGELOG.md`: one `### Added` line under `## [Unreleased]`.

Acceptance, run from the worktree root before the commit:

```
$ python3 -m pytest -q runner/tests/test_critic.py
...................                                                      [100%]
19 passed in 0.05s
```

The same file under the gate's own runner prints `Ran 19 tests ... OK`.

## Item 2, the command and the gate

Commit `3301c9d feat(critic): run the checks from critic and before demo`.

Files changed:

- `runner/dark/__main__.py`: `cmd_critic` and the `critic` subcommand (`dark critic <records>` prints one line per finding and exits 1 on any, 0 on none); `cmd_demo` runs `critic.check` first, prints the same lines and returns 1 without calling `demo.render` on a finding, and `--no-critic` prints `demo: --no-critic: the critic is skipped` on standard error and cuts anyway. Registered `critic` in the dispatch table.
- `runner/tests/test_critic.py`: a `Command` class, `dark critic` clean (exit 0, no output) and with two findings (one line each, exit 1); `dark demo` refuses on a finding with `demo.render` mocked and asserts it was never called and no `demo.mp4` exists; `dark demo` on a clean run calls `demo.render`; `dark demo --no-critic` on a records directory the critic would refuse still calls `demo.render` and names the flag on standard error.
- `runner/tests/test_demo.py`: `write_records` now also writes the one `steps/<NN>.png` per verdict a user-arm run leaves, using `tests/pngfix.py`. This is what the demo's own render test needed once `dark demo` runs the critic first: a fixture that is not a run's records is now refused, and the test would otherwise have proved only the refusal.
- `specs/critic/spec.md`: a `## The command` section.
- `CHANGELOG.md`: a second `### Added` line for the command and the gate.

Acceptance, run from the worktree root before the commit:

```
$ python3 -m pytest -q runner/tests/test_critic.py runner/tests/test_demo.py
................................                                         [100%]
32 passed in 8.39s
```

## The fix commit

Commit `fix(critic): a corrupt step is a finding, not a crash`, after the report was first written: a verdict whose `step` is a JSON list or object (a corrupt `steps.jsonl`) made `check` raise `TypeError: unhashable type: 'list'`, because the step value was used as a dict key. The spec's "never an exception" is about exactly this, so `_check_steps` now separates the verdicts whose step is a number in range from every other one and reports the latter as a `steps` finding. `runner/tests/test_critic.py` gained `test_a_step_that_is_not_a_number_is_a_finding_not_a_crash`, and `CHANGELOG.md`'s critic line now says a records directory the rules cannot read returns the finding, never an exception.

Reruns after the fix, from the worktree root:

```
$ python3 -m pytest -q runner/tests/test_critic.py
.........................                                                [100%]
25 passed in 0.14s
$ python3 -m pytest -q runner/tests/test_critic.py runner/tests/test_demo.py
.................................                                        [100%]
33 passed in 8.27s
```

## The full suite, before and after

Both runs were `python3 -m pytest -q runner/tests` on the same machine.

- Before (base `9945408`, clean tree): `905 passed, 2 skipped, 207 subtests passed in 268.24s`.
- After (the fix commit): `930 passed, 2 skipped, 207 subtests passed in 234.79s`.

25 tests were added (20 for the checks, 5 for the command); no test that passed at the base fails now. The 2 skips are the same at the base and after: both are `runner/tests/test_user_target.py` skipping because Playwright is not installed, unrelated to this work. The render tests, which are the ones that need ffmpeg with libass and libx264, ran here (the machine has both).

The one gate, `cd runner && bash verify.sh`, prints its last line:

```
== no /tmp in tests
verify OK
```

with `Ran 931 tests in 234.288s / OK (skipped=2)` above it. unittest counts 931 tests to pytest's 932 collected (930 passed + 2 skipped) for one reason: `runner/tests/test_repo_hygiene.py` has a module-level `def test_no_tracked_file_is_evidence()`, which pytest collects and unittest does not. Both runners agree there are no failures. `config OK`, `release pressure` quiet, `no-binaries OK`, `no-private OK`, `leaks OK`.

## Out of scope, reported not fixed

- **The claimed outcome has one home.** The records carry a run's outcome only in the `README.md` badge; `task.json` does not hold one. The `outcome` rule therefore reads `README.md`, and a records directory without a readable badge makes no claim and draws no finding. That is deliberate (the spec states it) but it means the rule is only as strong as the arm's willingness to write the badge.
- **The outcome rule is written twice.** `dark/critic.py:verdict_outcome` and `dark/demo.py:outcome` decide the same thing. I did not have `demo.py` import the critic (it is the renderer, and the critic is the gate in front of it) and did not refactor `demo.py`; the two could drift. A later change could have the demo call the critic's function.
- **The PNG reader is narrow.** `png_is_flat` reads 8-bit, non-interlaced PNGs of colour types 0, 2, 3, 4 and 6. A 16-bit or interlaced PNG is reported as `cannot be read`, which is a finding, not a silent pass; a real screenshot from Playwright is 8-bit and non-interlaced.
- **No MCP tool.** `dark user` and `dark desktop` are exposed through the MCP server; `dark critic` is not, and the brief did not ask for it.
- **No plan or tasks file.** The brief named `specs/critic/spec.md` only; `specs/critic/` holds no `plan.md` and no `tasks.md`, though `AGENTS.md` describes a feature as spec, plan and tasks.
- **The desktop arm's `checks.jsonl` is not read.** The critic's rules are on the steps every arm keeps. A desktop run whose hidden checks contradict its steps is not the critic's business; the spec says so.

## Design choice a reader may disagree with

The brief said the fourth rule is "the outcome the records claim equals all steps passing". I read the claim from the `README.md` badge (the only outcome in the records) and compare it to the outcome the verdicts imply, `pass` only when every step passed, `fail` when a step failed, `inconclusive` otherwise, rather than only testing `claimed == "pass" iff all pass`. The stronger form also catches a `README.md` that says `inconclusive` over a failed step, which the arm's own summary (`runner/dark/user.py:_main`) would never write. Both forms are stated in `specs/critic/spec.md`.

## Review fix (hub, 2026-10-06)

Run over 25 real user-arm records, the `note` rule fired on correct passes whose note quotes the page ("9 failed" in a jobs list, "the most recent failed save is shown first"). It is now a warning: printed with `(warning, does not block)`, never a reason for exit 1 or a refused cut. `critic.blocking()` holds the rule set that blocks. A new test covers a note-only run: `dark critic` exits 0 and `dark demo` cuts.

# Report: 1057-a, the user arm remembers earlier steps and quotes the page

## Terms, once

- **user arm**: `runner/dark/user.py`, the executor that puts one URL and a numbered list of steps to a model and drives a browser through them.
- **snapshot**: one string of the page's accessibility tree, what the model is shown of the page; `run_steps` takes it each call and `ChatModel` cuts it to `SNAPSHOT_CHARS`.
- **earlier steps**: the steps already finished when the model is asked about the current one, each as the line `step N (pass|fail): <note>`.
- **verdict**: the model's answer that ends a step, `{"do": "verdict", "verdict": "pass"|"fail", "note": "..."}`.
- **evidence**: the text a pass verdict quotes from the page, in its `"evidence"` field; a fail needs none.
- **whitespace-normalised**: every run of whitespace squeezed to one space, so a quote copied with a different line break or indent still matches.
- **call envelope**: the `max_calls` budget for a run; every answer from the model, a refused one included, counts as one call.
- **steps.jsonl**: `<records>/steps.jsonl`, one JSON row per step, `{step, verdict, note}`, plus `evidence` on a pass.

## Item 1, earlier steps in the prompt

- Added the `EARLIER STEPS:` block to `PROMPT`, between `URL:` and `ACTIONS SO FAR IN THIS STEP:`, formatted by the new `earlier_steps(results)` as one `step N (pass|fail): <note>` line per finished step, or the single word `none` when none has finished.
- `ChatModel.next_action` takes a sixth argument, `earlier` (default `None`), and fills the block from it; `run_steps` passes `earlier=results`, the results list it builds step by step, so step N sees steps 1 to N-1.
- `SYSTEM` gains one sentence: "When a step refers to something an earlier step made (an order, a job, a code), take it from the earlier steps' notes." The `next_action` interface is one argument wider, so a caller that passes six arguments positionally is fine and one that stops at `history` still is too, because `earlier` defaults.
- Test `test_the_prompt_carries_each_finished_steps_note_and_none_for_the_first` drives a real `ChatModel` over `fakes.FakeLLM` through `run_steps` for two steps and reads the request bodies: step 1's prompt holds `EARLIER STEPS:\nnone`, step 2's first prompt holds `EARLIER STEPS:\nstep 1 (pass): the form is shown`. `ScriptedModel` now records the `earlier` list it was given.
- Scope run before the commit: `cd runner && python3 -m unittest tests.test_user -q` prints OK (30 tests).
- Commit `feat(user): show finished steps' notes in the prompt`.

## Item 2, a pass needs a quote

- Added `shown_snapshot(snapshot)`, the one place the `SNAPSHOT_CHARS` cut is spelled, used by `ChatModel` for the prompt and by `run_steps` for the check. Added `_normalised(text)` and `evidence_on_page(evidence, snapshot)`: the quote and the shown snapshot are whitespace-normalised, the quote must occur in the snapshot, and a missing or blank quote never counts.
- `run_steps`: a `pass` whose evidence fails `evidence_on_page` appends `{"error": "evidence not found on the page"}` to the step's history and asks again; the refused answer already spent a call through `S.STATS["calls"]`, so the retry counts against the envelope like any call and can hit `max_calls`. A `fail` is accepted with no evidence. A pass row gains `evidence`; a fail row has no such key.
- `SYSTEM`'s verdict line now shows `"evidence": "<text copied from the page>"` and a following sentence says a pass must carry evidence copied word for word from the snapshot, that a pass whose evidence is not on the page is refused and asked again, and that a fail needs no evidence.
- Tests, all against `ScriptedModel` and `FakePage`: a pass with its quote on the page is accepted and the row keeps the quote, and a fail row carries no evidence; a quote copied across a line break matches; a pass naming job 18 against a page showing job 17 is refused, the retry naming job 17 is accepted, and the call count is both; a pass with no `evidence` field is refused; and the job-17 case the defect came from, where step 1's note names job 18 and the page shows job 17, is refused and the model is forced to fail the step.
- The `verdict()` test helper now puts `"evidence": "Shop"` on a pass by default, matching the fake pages' snapshot, so the existing tests keep testing what they tested; the two executor tests whose page showed only the session token were given a `Shop` heading too.
- Scope run before the commit: `cd runner && python3 -m unittest tests.test_user -q` prints OK (35 tests), then `cd runner && bash verify.sh` prints `verify OK` (616 tests OK, config OK, no-binaries OK).
- Commit `feat(user): require a pass verdict to quote the page`, carrying this report.

## Out of scope, reported not fixed

- `runner/dark/run.py` reads the executor's `steps_ok` and `steps_total` off the AGENT-DONE tag and never looks inside `steps.jsonl`, so nothing in the runner uses the new `evidence` field; that is the intended shape, the quote is a record and a retry rule, not a second judge.
- The `stream.jsonl` entry records `snapshot_chars` as the length of the full snapshot, while the model was shown the `SNAPSHOT_CHARS` cut. Both lengths are the same for these tests; the mismatch predates this change and is left.
- `CHANGELOG.md` was not edited: the merge adds the entry, per the task.
- Mid-run the host's `/tmp` tmpfs reached 100% of its 1048576 inodes from the sessions running in parallel, and `verify.sh` failed with `OSError: [Errno 28] No space left on device` in 146 tests across the suite, none of them touching a file this task changed. Freeing abandoned empty scratch directories under `/tmp` (no file, only empty directories, was removed) recovered inodes, and the exact command `cd runner && bash verify.sh` then printed `verify OK` on this tree.

## Not done / not verified / blocked

NONE

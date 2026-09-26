# Plan: the user arm

This is the plan for the feature in `spec.md`. A plan says how to build what the spec says; the ordered checklist that carries it out is `tasks.md`. The terms user arm, target, snapshot, action, evidence, fragment, trail and envelope are defined in `spec.md` and used here as defined there. Two more: the **fake page** is the stand-in browser the tests hand to `run_steps` (a `goto`, `url`, `snapshot`, `act`, `screenshot` and `close` that play back a script), and the **scripted model** is the stand-in model that answers a fixed list of actions.

## Where it lives

- `runner/dark/user.py` is the executor. It runs inside the browser sandbox as `/opt/user.py` beside `/opt/session.py`, whose reporting it shares: the progress comment and heartbeat, the `AGENT-DONE` tag, the records push. It imports Playwright only inside `PlaywrightPage.__init__` (constitution rule 33).
- `runner/dark/tasks.py` parses a user task: `url` must be an `http://` or `https://` address, `steps` a non-empty list of non-empty strings, `spec` a non-empty string; `may_edit`, `after`, `lang` and `stage_timeout` are refused. `repos` is not read for a user task, but not refused either (`tasks.md` item 8).
- `runner/dark/run.py` holds `Runner.user`: it builds the sandbox from the browser image on the target network, writes `/opt/task.json` (`url`, `spec`, `steps`, the tier's `llm_url` and `llm_model`, `max_calls`, `max_seconds`), watches the issue for the `AGENT-DONE` tag, and maps the tag to the outcome through `spec.FAIL_KIND_OUTCOME`.
- `runner/dark/spec.py` names `inconclusive` in `OUTCOMES` and keeps it out of `FAIL_OUTCOMES`, so it never escalates.
- `runner/dark/__main__.py` holds `cmd_user`, which prints the one JSON line and exits 0 only on `pass`.
- `runner/budgets.toml` `[class.user]` sets the envelope.

## The step loop

`run_steps(model, page, url, steps, records_dir, max_calls, deadline)` opens the URL once and walks the steps in order. Per call it takes a snapshot, attaches the requests the previous action set off to that action's trail line, asks the model with the step text, the URL, the snapshot as `shown_snapshot` cuts it, the actions so far in this step and the notes of the finished steps, and then handles the answer:

- a verdict of an unknown kind, or a reply that is not one JSON action, goes back to the model as an error in the step's history;
- a pass whose evidence fails `first_missing_fragment` against the shown snapshot goes back as `evidence not found on the page: "<fragment>"` and feeds the refused-quote count, keyed on the snapshot and the exact answer;
- any other verdict ends the step;
- a browser action is performed; unless it is a `wait` it feeds the stuck count, keyed on the snapshot and the exact action; a failed action is recorded as an error, never raised.

The quote rule is four small functions, so each can be tested alone: `_normalised` (case, whitespace, edge punctuation), `strip_roles` (the line marker and the role token), `evidence_fragments` (the split and the minimum fragment length) and `first_missing_fragment`, which `evidence_on_page` wraps.

The two envelope checks come first in every call: calls spent ends the step and marks every later step inconclusive; seconds spent does the same with fail. `ModelError` and `BrowserError` propagate to `_main`, which turns them into `fail` tags of kind `llm` and `env`.

## The records

`records_paths` lists what the records push adds: `steps.jsonl`, `steps/`, `trace.zip` and `video.webm`; the push skips a path that does not exist. Every JSON line passes through `session.scrub` before it is written, so a token that reached the page never reaches the records. `PlaywrightPage` keeps only requests to the origin of the first URL it opened, and drains them before each step and after each model call, so the trail holds what the actions set off and nothing the model call itself did.

## The tests

`runner/tests/test_user.py` drives `run_steps` and `main` with the scripted model and the fake page, and covers each rule in the spec by name: the three verdicts, the calls and seconds envelopes, the stuck rule (a repeated click ends stuck; a wait never does; a verdict between repeats changes nothing), the refused-quote rule, each part of the quote rule (lines joined, role prefixes, fragments, a short fragment kept with its neighbour, a quote not on the page refused), the trail and the `saw` note, and the tag each ending writes. `runner/tests/test_run.py` covers the runner's mapping of each tag to its outcome, `inconclusive` included. No test starts a browser or a model.

## Constitution check

Rule 5 (the executor reports, the runner decides) by `_main` writing a tag and `Runner.user` mapping it. Rule 11 (a URL and steps only) by the task parser refusing the work-repo fields and `Runner.user` writing only the URL, spec and steps into `/opt/task.json`. Rule 12 (the target network) is the operator's `dark-target-net` and is unchanged here. Rule 19 says each step has a `pass` or `fail` verdict, which the third verdict, `inconclusive`, has outgrown, and `docs/concepts.md`'s entry for "step verdict" says the same; `tasks.md` holds the item that brings both up to date. Rule 33 by the Playwright import staying inside the constructor.

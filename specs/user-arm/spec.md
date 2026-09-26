# Spec: the user arm

This is the spec for one feature. A spec says what to build and why; its plan, `plan.md`, says how, and its tasks, `tasks.md`, is the ordered checklist. Read `AGENTS.md` and `memory/constitution.md` first. Most of this feature is built; the spec records what it must do so that later changes, and the findings of running it against dark itself, have one place to land.

## Terms

- **user arm**, **user task** and **step verdict**: as defined in `docs/concepts.md`. A user task is a `task.toml` of class `user` with a `url`, a `spec` and `steps`, the numbered step texts.
- **target**: the deployed web application a user task names by its `url`. The user arm never sees its source.
- **snapshot**: the page's accessibility tree as text, one node per line, as Playwright's `aria_snapshot` gives it; the model sees the page only through it, cut to 12,000 characters.
- **action**: one browser operation the model asks for: `click`, `fill`, `select`, `press`, `goto` or `wait`.
- **evidence** (of a pass): the text a pass verdict quotes from the page, word for word.
- **fragment**: one part of the evidence, split where a model joins separate page elements (see "The quote rule").
- **trail**: the per-step file of the actions taken and the requests each set off.
- **envelope**: the run's limits, `max_calls` model calls and `max_seconds` of wall time, from the task's class in `budgets.toml`.
- **records repository**, **ledger**, **`run.end` row**, **outcome**: as defined in `memory/constitution.md`.

## Purpose

A code task is judged by hidden tests; an application that people use is judged by whether a person can use it. The user arm puts a model in the place of that person: a fresh browser sandbox, given only a URL and numbered steps, works through the steps and gives each a verdict, and the run's outcome follows from the verdicts. It is meant to be run against any deployed web application, dark's own records pages included, and to report where a stranger gets stuck, not only whether a feature works.

## The user

Two users. The **operator** writes a user task for an application they deploy, runs it with `dark user --task <task.toml> --tier <id>` (or the `dark_user` MCP tool), and reads the outcome line, `steps.jsonl` and the screenshots to decide what to fix. The **model** inside the sandbox is the other user: it has the task's `spec`, one step at a time, the notes of the steps before, and the page; it has no source, no repository and nothing to clone (constitution rule 11).

## The three verdicts

Every step ends with exactly one verdict.

- **pass**: the step was done and the page shows it. A pass must carry evidence, and is accepted only under the quote rule below.
- **fail**: the step was shown not to work (silence, an error, wording the task's `spec` rules out). A fail needs no evidence.
- **inconclusive**: the step could not be judged. The model may give it when the page cannot tell it, and the arm gives it itself in three cases: the calls envelope ran out (`note` `calls exhausted`, and every later step `not reached: the calls envelope was spent`); the stuck rule fired; or the refused-quote rule fired.

A spent wall envelope ends the current step and every later one as `fail`, because the run as a whole is then `fail:budget`.

## The quote rule

A pass verdict is accepted only when every fragment of its evidence is found in the snapshot the model was shown for that same call. Both sides are compared lower-cased, with every run of whitespace, line breaks included, squeezed to one space, and with the characters `— – - " ' … .` dropped where they touch a space or an end. The snapshot first loses its role prefixes: a line's leading `- ` and a leading `role "name":` or `role:` token, so `- button "Open": Road Trip` is compared as `Road Trip`; a line that is only `role "name"` keeps its words. The evidence is split into fragments at ` — `, ` – `, ` - `, `; ` and `. `, and a fragment shorter than 8 characters stays joined to its neighbour. So a quote may span adjacent snapshot lines, and may join separate elements with a dash, and is still checked word for word.

A refused pass is not a verdict: the model is told `evidence not found on the page: "<the first missing fragment>"` and asked again, which costs a call. The same refused pass, on an unchanged snapshot, three times in a row, ends the step `inconclusive` with `note` `evidence not found: <the first 80 characters of the quote>`.

## The stuck rule

The same browser action on an unchanged snapshot three times in a row ends the step `inconclusive` with `note` `stuck: <what it acted on>`. The count starts again whenever the snapshot or the action changes. Only browser actions count: a verdict, accepted or refused, neither counts nor resets the count, and a `wait` never counts, so a step may wait as long as its calls allow.

## The records

Each run's records repository holds, besides the transcript and task record every arm keeps:

| path | what it holds |
|---|---|
| `steps.jsonl` | one line per step, in order: `step`, `verdict`, `note`, and `evidence` on a pass |
| `steps/<NN>.png` | a full-page screenshot taken when the step's verdict was given |
| `steps/<NN>.trail.jsonl` | one line per action of the step: `t` (seconds since the step began), `action`, `target` (the locator, address, key or seconds it acted on) and `requests`, the requests to the target's own origin that the action set off before the next snapshot, each `method`, `url` and `status` |
| `stream.jsonl` | every model call: step, call number, URL, snapshot size, the action and its result |
| `trace.zip`, `video.webm` | a Playwright trace and a video of the whole run, best effort: one that cannot be made is skipped |

A step whose trail holds a 4xx or 5xx response gets `saw <status> <method> <path>` appended to its note, once per distinct response.

## The outcome

The arm reports; the runner decides (constitution rule 5). The arm ends with one `AGENT-DONE` tag, and the runner maps it:

| the steps | the tag | the run's outcome | failure kind |
|---|---|---|---|
| every step pass | `ok` | `pass` | none |
| at least one step fail | `fail`, kind `steps` | `fail:capability` | `steps` |
| no step fail, at least one inconclusive | `inconclusive` | `inconclusive` | none |
| the wall envelope spent | `fail`, kind `seconds` | `fail:budget` | `seconds` |
| the browser did not start or the URL did not open | `fail`, kind `env` | `fail:structural` | `env` |
| the model endpoint failed | `fail`, kind `llm` | `fail:structural` | `llm` |

`inconclusive` is an outcome of its own: it is not a failure outcome and never escalates to another tier. The outcome's detail is the tally, `<n>/<total> steps pass, <m> inconclusive`, and `dark user` prints one JSON line with `run`, `outcome`, `fail_kind`, `detail`, `issue`, `records`, `steps_ok` and `steps_total`, exiting 0 only on `pass`.

## Out of scope

- Seeing the target's source, logs or database: the arm judges the page only.
- Any request to an origin other than the target's in the trail; the target network allows only the named `host:port` entries (constitution rule 12).
- Judging layout or looks from the screenshot: the model sees the snapshot, not the image.
- Starting, seeding or resetting the target: the operator deploys it before the run.
- Several browsers, several tabs, file uploads and downloads.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `runner/verify.sh` checks release pressure: `python3 -m dark.pressure CHANGELOG.md` counts the entries under `## [Unreleased]`, warns that a patch release is due above 5, and fails the gate above 10.

### Fixed

- `dark` finds its config after `pip install .`: without `--conf` or `$DARK_CONF` it reads `./runner` when the current directory is a dark checkout, then the toml files beside the package as before. When neither holds `models.toml`, every command and MCP tool refuses with exit code 2, naming the directories tried and saying to pass `--conf <dir>` or set `DARK_CONF`, instead of naming a `models.toml` in site-packages that was never installed.

## [0.6.2] - 2026-09-28

### Added

- `bench/tasks/hello-user`, an example user task: three steps a stranger can check on https://example.com, so `dark user` has a task to try; `check_tasks.py` checks a user task with the runner's loader and `validate.py` skips it, as it has no acceptance.

### Fixed

- The README's Quick start works on a fresh machine: it installs `'.[dev]'` so `verify.sh` finds pytest, the task check clones dark-tasks and exports `DARK_TASKS` at it instead of expecting a clone beside the checkout, and `docs/fresh-container.md` records that Quick start run.
- `dark preflight` on a machine without `ssh` (or `docker`) reports the compute plane as one `MISS` line naming the missing program and exits 2, instead of dying with a `FileNotFoundError` traceback.

## [0.6.1] - 2026-09-28

### Added

- `runner/verify.sh` ends with a credential scan: `runner/ops/leaks.sh` runs gitleaks over the repository's git history (or a given log range) and fails when it finds a credential, or when gitleaks is not installed, since a skipped scan would pass everything. CI installs gitleaks 8.30.1, checked against its published sha256, and checks out the full history.
- The stager reads what a run's branch adds to main before running it: gitleaks over `origin/main..HEAD`, redacted, fails the work (`fail:capability`) when the branch adds a credential; a missing gitleaks is reported as `NOT SCANNED`. When the branch's own checks write a `coverage.xml`, diff-cover adds a line on new lines no test ran to the STAGE-DONE comment, as a note and never a verdict. The sandbox image carries both tools.
- The MCP tools `dark_user` and `dark_long` take the task as text, `task_toml`, as well as a path, so a client with no files on the runner's machine can hand it work; the server lays the text out under a temporary directory named after the task's id and removes it after the run.
- Every user-arm run's records carry a `README.md`, which the Git host shows when the run's folder is opened: the outcome as a coloured badge (🟢 PASS, 🔴 FAIL, 🟡 INCONCLUSIVE) with its tally, a table of the steps with each verdict and note, and one line per file saying what it holds. A stranger reviewing the records page found neither an outcome nor an explanation of the file names.
- `dark demo <records>` cuts a user-arm run's records into `<records>/demo.mp4`, a video a person can follow without reading a report: a title card with the task, the run with the current step's text in a band above the page, each action captioned below it and each click ringed, the frame held at every verdict with the verdict and its note, and an end card with every step's verdict and the outcome. Times are read from the Playwright trace (step k ends at its verdict screenshot); `--voice <piper .onnx>` speaks the task, each verdict and the outcome. Needs ffmpeg with libass and libx264 on the machine that runs it.
- `dark --version` prints `dark <version>`, and every `run.end` row carries `version`, the dark version that wrote it (the checkout's `pyproject.toml` when dark runs from one, else the installed package's, else `unknown`), so a ledger row names the release that produced it.

### Fixed

- The user arm acts on the element the model named, not the first whose name contains it. Playwright's role locator matches a name as a substring, and the arm took `.first`: on a records page it opened the latest commit, whose message link `records: <run>` comes before the run's folder link `<run>`, and judged the next four steps on the wrong page. An exact name now wins; several partial matches and no exact one refuse the action, and the refusal quotes the candidates' names so the model can pick one.
- The stager's check that a run's branch leaves `.dark/` and `.gitea/` alone never ran: its single-branch clone had no `origin/main`, the diff against it failed, and the empty output read as unchanged. The stager now fetches main by an explicit refspec, and a diff that fails stops the stage instead of passing it.
- The user arm refuses a pass that names another identifier than an earlier step named: when step 3 sent print job `HL-L2400DWE-18` and step 4 passed on `HL-L2400DWE-17`, the first completed job the page listed, step 4 is asked again, and three such answers end it as `inconclusive`. Replayed on 161 recorded passes, it flags that one false pass and no other.
- The user arm's quote check accepts what the model quotes from the page: the evidence is split into fragments on dashes, semicolons and sentence ends, each fragment is matched against the snapshot text with role prefixes (`button "…":`, `text:` and the like) removed, and a refused quote names the first missing fragment. A wait action never counts toward the stuck rule.
- A verdict answer never counts toward the user arm's stuck rule: a pass whose quote is not found on the page is refused and asked again, and three refused quotes end the step as `inconclusive` with the quote in the note. The quote check now matches text that spans adjacent snapshot lines, with whitespace, dashes and quote marks collapsed.

## [0.6.0] - 2026-09-26

### Added

- `inconclusive` is an outcome of its own: a user-arm run in which no step failed but at least one could not be judged ends as `inconclusive` with the tally as its detail and no failure kind, instead of `fail:structural`. It is terminal, never a failure outcome and never escalates.
- The user arm has a third verdict, `inconclusive`, and never records a step it could not judge as `fail`: the model may answer it, a call envelope that runs out marks the step it ran out in and every step never reached, and the same action three times on an unchanged page is recorded as `stuck: <action>`. The summary counts inconclusive steps apart from failed ones.
- Every user-arm step writes `<NN>.trail.jsonl` beside its screenshot: one line per action with the action, what it acted on, and the HTTP requests the page issued before the next snapshot (method, URL, status); a 4xx or 5xx response is echoed in the step's note, as `saw 502 GET /api/liked`.
- `python3 -m dark export-harbor` carries a task's full starting tree: the language template, the tree an earlier chain step leaves and the task's own `start/`, built by the same `dark/tasks.py` functions a run uses and committed as one git commit inside the exported image, so the image's working directory is what dark's executor is given. `start/` is now optional in a task record, and `solution.sh` extracts the oracle as the image's own user, never the uid of the packing host.
- `python3 -m dark ledger-tail [--lines N] [--kind KIND]` prints the last 20 rows of the ledger by default, oldest first, one JSON object per line, keeps only rows of a named event kind, skips a line that is not JSON with one note on standard error, and exits 0 when the ledger is missing or empty.
- `otlp-gate` in `compose.yaml`: a pinned reverse proxy on `back` and `front` that forwards port 4318 to `DARK_OTLP_UPSTREAM`, so a runner on the internal network can send its spans to a collector elsewhere (`otlp_endpoint = "http://otlp-gate:4318"`).
- A user-arm run's trace has one child span per browser action, `execute_tool browser.<do>`, with the action, its result and the step number.
- MCP tests for `dark_run` and `dark_ledger_tail`, the last open task of `specs/mcp-server`.

### Changed

- The user arm shows each step the notes of the steps already finished, and a pass verdict must quote text from the page (`evidence`), accepted only when the quote is in the snapshot the model was shown; `steps.jsonl` keeps the quote.

### Fixed

- A dark task under Inspect starts from an exported image that already holds the starting tree's one git commit, so the `starting_tree` setup step commits only when the working tree differs from `HEAD`; it no longer fails every sample with `nothing to commit`.

## [0.5.0] - 2026-09-25

### Added

- `otlp_endpoint` in `runner/host.toml` (variable `DARK_OTLP_ENDPOINT`, empty by default) sends every finished run to an OpenTelemetry collector as one trace over OTLP/HTTP JSON, standard library only (`runner/dark/otel.py`): a parent span `invoke_agent <task>` and one `execute_tool <tool>` child per tool call in the run's stream, named after the GenAI semantic conventions. A collector that is down is logged and never changes a run; empty sends nothing. See "Traces" in `docs/docker.md`.
- `python3 -m dark mcp` is a Model Context Protocol server over stdio (`runner/dark/mcp.py`): six tools, `dark_preflight`, `dark_run`, `dark_user`, `dark_long`, `dark_review` and `dark_ledger_tail`, so an agent with an MCP client can drive the factory without a checkout or a shell. Each tool but the last runs the matching `python3 -m dark` command as a subprocess with a `timeout_seconds` limit (default 1800) and returns its output, with `isError` set and the exit code appended when it fails; the last reads the ledger itself. `docs/mcp.md` says how to register the server with Claude Code and Codex and what environment it needs.
- `python3 -m dark export-harbor` writes one dark task as a Terminal-Bench
  task directory (`runner/dark/export.py`): `task.yaml` from the task's spec
  and its stage timeout, a `Dockerfile` on the sandbox base image that copies
  `start/` into the working directory, `docker-compose.yaml`, `run-tests.sh`
  running the hidden acceptance the way the stager does, `solution.sh`
  carrying the oracle overlay, and the acceptance under `tests/`. A task with
  no `oracle/` still exports, with a `solution.sh` that says NOT AVAILABLE.
- The user arm records every run: `trace.zip` (a Playwright trace, opened with `npx playwright show-trace trace.zip`) and `video.webm` are saved beside `steps.jsonl` in the run's records, and `runner/sandbox/Dockerfile.browser` now installs Playwright's ffmpeg so the video can be encoded; rebuild the browser image to get it. A recording that cannot be made is skipped and never changes a step or a verdict. See "Watching a user-arm run" in `docs/docker.md`.
- `AGENTS.md`, the file every coding agent reads first: what dark is, where the rules and specs are, how to run the gate and how to commit.
- `memory/constitution.md`, 40 numbered rules the project never breaks, each ending with the file it comes from.
- `specs/mcp-server/spec.md`, `plan.md` and `tasks.md`, the first feature written in the spec, plan and tasks shape: dark as a Model Context Protocol (MCP) server over stdio. Nothing is built yet; the files say what and how.
- `CLAUDE.md`, one line that sends Claude Code to `AGENTS.md`.

## [0.4.1] - 2026-09-25

### Fixed

- `python3 -m dark long` prints `judge: none (no branch pushed)` when the
  long run pushed no branch, so a caller reading its output can tell that
  case from a run that is still going.

## [0.4.0] - 2026-09-25

### Added

- `runner/ops/build-runtime.sh` builds the runtime archive the session,
  long, review and user arms unpack: node from nodejs.org, checked against
  its published sha256 sums, and the pi package installed with that node.
  Until now the archive could only be made by hand.
- `runner/ops/push-runtime.sh` publishes the archive as `<org>/vm-runtime`,
  and `ops/docker-bootstrap.sh` runs it when `DARK_RUNTIME_ARCHIVE` names a
  file.
- `runner/ops/target-net.sh` creates the user arm's target network and
  limits its egress to named `host:port` addresses with DOCKER-USER rules,
  idempotently; `runner/ops/dark-target-net.service` applies it at boot.

## [0.3.0] - 2026-09-25

### Added

- An `lxc` backend: a sandbox is a full clone of a named snapshot of a real
  Proxmox container, fenced by the same default-drop firewall as a VM.
  `sandbox_pool` places each clone in a pool, and `sandbox_allow_in` opens
  it to named `<ipv4>:<port>` clients. Clones do not start with the host.
- `snapshot` and `rollback` on the sandbox interface (lxc and Proxmox; docker
  refuses).
- The long arm: a task of class `long` names several repositories, one
  session works across them for up to four hours, and
  `python3 -m dark long` hands the branches it pushed to a judging review.
- A review run can judge branches (`--review-branches`): the last line of its
  `report.md`, `VERDICT: pass` or `VERDICT: fail`, is the outcome.
- `tools = "full"` in a task gives the session arm pi's full tool set.
- The user arm: a task of class `user` gives a fresh browser sandbox a URL
  and numbered steps, and `python3 -m dark user` records a verdict and a
  screenshot per step. `runner/sandbox/Dockerfile.browser` builds its image.

### Fixed

- `ops/docker-bootstrap.sh` creates the records organisation. Without it
  every session, review and user run stopped at its first Gitea call.
- Two runs of one shift starting together no longer abort on creating the
  shift's records repository.
- The docker sandbox image includes `libatomic1`, which the session arm's
  node needs to start.
- The review class allows 40 calls instead of 4, so a judge can read before
  it writes its verdict.

## [0.2.0] - 2026-09-24

### Added

- An installable Python package. `pip install .` provides the `dark`
  command for the runner's commands.
- GitHub Actions CI that runs the runner gate and the task checks.
- `docs/concepts.md`, which defines each term the code uses in plain words,
  and `docs/history.md`, which says where the project came from.
- A banner in the README.

### Changed

- `models.toml`, `budgets.toml` and `host.toml` ship example values for one
  local machine: a placeholder OpenAI-compatible endpoint, a local Git host,
  and notifications off. Replace them with your own.
- Private deployment names were replaced by generic ones. The task contract
  directory `.factory/` is now `.dark/`, and the users, organisations and
  host names are generic. A task set written for dark-tasks v1.0 names
  `.factory/` and needs dark-tasks v1.1.
- The runner, bench, template and README prose was rewritten to read as a
  fresh project.

### Removed

- The study's frozen comparison sets and recorded probe data. They live in
  dark-paper now.

## [0.1.0] - 2026-09-23

The tree the study ran. dark-paper pins this repository at this tag.

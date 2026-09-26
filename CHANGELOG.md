# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

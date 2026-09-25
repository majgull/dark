# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

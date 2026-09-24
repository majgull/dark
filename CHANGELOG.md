# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - unreleased

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

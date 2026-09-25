# AGENTS.md

An AGENTS.md is the one file at a repository root that every coding agent (Claude Code, Codex, Cursor, Kiro and others) reads first. It is plain Markdown with no required fields, and the nearest one in the tree wins. Read this one top to bottom before you change anything.

## What dark is

dark factory is a pipeline that lets a language model write code on its own, inside a virtual machine that exists for one task and is deleted when the task ends, judged by tests it never sees.

## The constitution

The constitution is the short file of rules this project never breaks, read before any spec. Here it is `memory/constitution.md`, at most 40 numbered rules, each ending with the file it comes from. If a request conflicts with a rule, stop and name the rule instead of working around it.

## Specs

A spec says what to build and why, a plan says how, and tasks is the ordered checklist of commit-sized steps, each ending with the command that shows it done. `specs/<feature>/` holds spec, plan and tasks as `spec.md`, `plan.md` and `tasks.md`. Read the spec, then the plan, then take the first unchecked task. `specs/mcp-server/` is the first feature.

## The gate

The gate is the one script that must pass before any push: `cd runner && bash verify.sh`. It runs the tests in `runner/tests`, validates the configuration and refuses binaries, and it prints `verify OK` only when all three pass. A change is not done until it prints that line.

## Commits

- The subject is conventional, `<type>(<scope>): <summary>`, at most 72 characters; the types are feat, fix, docs, refactor, build, ci, chore, test, perf, style and revert.
- Every user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`; a release is its own commit, `chore: release X.Y.Z`.
- No binaries in the tree: `runner/ops/no-binaries.sh`, which the gate runs, refuses a tracked file that is not text or is over 1 MB.
- One idea per commit, with the reason in the body when it is not obvious.

## Records of past work

- `docs/history.md` says where the project came from.
- `docs/plans/` holds the plans earlier work followed, such as `docs/plans/three-arms.md`.
- `CHANGELOG.md` lists what changed in each release.
- `docs/concepts.md` defines every in-house term once; use its words.

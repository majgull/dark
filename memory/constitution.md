# Constitution

A constitution is the short file of rules a project never breaks, read before any spec. Each numbered rule below is one sentence that a reader can check against the repository, and ends with the file it comes from in parentheses. There are 40 rules, the most this file may hold.

## Terms

- **run**: one task put to one model by one arm, from its start to one outcome.
- **arm**: one way of putting a model to a task; the user arm has a browser check a deployed URL step by step, and the long arm has one session work across several repositories and a reviewer session judge the branches.
- **sandbox**: the throwaway machine, a virtual machine or a container, that a run works in; it is created for that run and destroyed with its disk when the run ends.
- **executor**: the program inside the sandbox that asks the model for code.
- **hidden acceptance tests**: the tests that judge a code task; the model never sees them.
- **staging**: running the hidden acceptance tests against the branch a run pushed, in a second fresh sandbox.
- **judge**: whatever decides a run's outcome, which is the hidden acceptance tests for a code task and a reviewer session for a long task.
- **ledger**: the append-only file of JSON lines where every run is written; each line is a row, and the closing row of a run is its `run.end` row.
- **model gate**: the reverse proxy in front of the models, and the only model address a sandbox is given.
- **records repository**: a Git repository, one per shift, that holds a session's kept transcript and task record.
- **evidence**: run logs, recordings and images.
- **the gate**: `runner/verify.sh`, the one script that runs the tests, the configuration check and the no-binaries check.
- **standard library**: the modules that ship with Python itself.

## Isolation

1. A run executes only in a sandbox that was created for it and is destroyed, disk included, when the run ends. (README.md)
2. The hidden acceptance tests run in a second, fresh sandbox, started only after the executor's sandbox is gone. (docs/concepts.md)
3. The executor never sees the hidden acceptance tests or the reference solution. (docs/concepts.md)
4. The judge never sees the model's claims: staging reads the pushed branch, a reviewer is given the task and the pushed branches, and neither is given the executor's own account of its work. (docs/concepts.md)
5. The executor reports what happened and never decides an outcome. (docs/concepts.md)
6. A staging verdict is trusted only when it carries a one-time value the executor never saw. (docs/concepts.md)
7. The executor holds the agent token and never the acceptance archive. (docs/docker.md)
8. On the container backend a sandbox reaches Gitea and the model gate and nothing else. (docs/docker.md)
9. A sandbox is given the model gate's address and never a model provider's own address. (docs/docker.md)
10. The docker socket is mounted into the runner and never into a sandbox. (docs/docker.md)
11. A user-arm sandbox is given a URL and numbered steps only, with no work repository and nothing to clone. (docs/concepts.md)
12. The user arm's target network drops every connection its subnet opens except one accept per named `host:port`. (docs/docker.md)
13. At the end of a run every sandbox it launched is confirmed gone, and the run's `run.end` row records `vms_destroyed` in its `asserts`. (docs/docker.md)

## Records

14. Every run writes exactly one `run.start` row and one `run.end` row to the ledger, and ends as exactly one outcome. (docs/concepts.md)
15. The ledger is append-only, so a row is never edited or deleted. (docs/concepts.md)
16. Pass rates, window and watts use, and admission are derived from the ledger and from nothing else. (docs/concepts.md)
17. Energy that cannot be metered is recorded as null and never guessed. (docs/docker.md)
18. A session-arm run pushes its kept transcript and task record to its shift's records repository. (docs/concepts.md)
19. Each user-arm step has a `pass` or `fail` verdict and a screenshot in the records repository, and the run passes only when every step passes. (docs/concepts.md)
20. A long-arm run's outcome is the reviewer's one final `VERDICT: pass` or `VERDICT: fail` line. (docs/concepts.md)
21. Evidence lives in records repositories and datasets and never in this repository's git, so no tracked file is over 200 KiB and no tracked log or media file is over 20 KiB. (CONTRIBUTING.md)

## Change

22. Every code change comes with tests in `runner/tests`. (runner/verify.sh)
23. `cd runner && bash verify.sh` prints `verify OK` before any push. (runner/verify.sh)
24. A new check is added to `verify.sh` itself, so that it stays the one gate. (runner/verify.sh)
25. Every tracked file that is not empty is text and under 1 MB. (runner/verify.sh)
26. A check is skipped only by a flag typed for that run and never by default, as `preflight --no-model` is. (docs/docker.md)
27. Every user-visible change has an entry under `## [Unreleased]` in `CHANGELOG.md`. (CHANGELOG.md)
28. `CHANGELOG.md` keeps the Keep a Changelog format. (CHANGELOG.md)
29. A version is cut by one commit, `chore: release X.Y.Z`, that moves the Unreleased entries under a dated heading and sets the same version in `pyproject.toml`. (CHANGELOG.md)
30. A release tag is never moved, and `v0.1.0`, which dark-paper pins, is never deleted. (CHANGELOG.md)
31. Every in-house term is defined once in `docs/concepts.md`, and other prose uses its words. (docs/concepts.md)

## Dependencies

32. `dependencies = []` in `pyproject.toml` stays empty. (pyproject.toml)
33. Modules in `runner/dark` import only the standard library, except that `user.py` imports Playwright inside the function that starts the browser, which runs only in the browser sandbox image. (pyproject.toml)
34. The one other package is pytest, pinned in the `dev` extra and imported only by tests. (pyproject.toml)
35. The bench tools need only Python 3, bash, coreutils and git. (README.md)
36. A downloaded runtime is checked against its published sha256 sums before it is used. (CHANGELOG.md)

## Public

37. The whole repository is dedicated to the public domain under CC0 1.0 Universal, and `LICENSE` is its text. (README.md)
38. Links to this project's repositories name the majgull account. (README.md)
39. Real endpoints and model ids live in a configuration directory outside the checkout, and the values in `runner/host.toml` are examples. (README.md)
40. No real private hostname, address or account name appears in code, tests or docs, and examples are generic. (CHANGELOG.md)

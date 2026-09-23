# dark/runner — the factory v2 runner

A task with a class, a spec and hidden acceptance tests goes through preflight, execute (throwaway VM), verify, push, stage (a fresh VM runs the hidden tests) and ends as exactly one outcome in a JSONL ledger. Every bound is data; which tier may run which class is derived from the ledger. Design: hub `docs/949-v2-design.md`; why: `docs/949-factory-v2-bootstrap.md`.

## Files a human edits

- `models.toml` the catalog: provider, cost (`local` = watts, `sub-window` = a daily window), speed, watts, roles. Every id is checked against the live provider at preflight.
- `budgets.toml` the hard caps per class, headroom, admission thresholds, the provisional tier order, windows, watts, watchdog, shift size.
- `host.toml` where things are on the runner host (Gitea URLs, token files, Proxmox, ntfy, checkouts). Env overrides are listed inline.

Nothing else carries a number. Measured values live in `~/.dark/ledger.jsonl` and the digest renders them.

## Commands (on the runner host, `cd ~/dark-runner`)

```
python3 -m dark check-config
python3 -m dark preflight [--no-vm]        # one line per check; refuses on any miss
python3 -m dark admission                  # the derived admission table
python3 -m dark status                     # windows, watts, envelopes today
python3 -m dark shift [--tasks a,b] [--tier <id>] [--max-runs N] [--arm factory] [--no-push]
python3 -m dark digest [--shift <id>]
python3 -m dark abort <run-id>|all
```

Detached with an unbuffered log: `bash ops/shift.sh [shift args]` (prints the log path under `~/.dark/logs/`). Session arms (design §7b) are driven from the hub by `dark/bench` `tools/arm.sh`, which ends in `python3 -m dark stage ... --slot 1` here, so a session arm can be staged while a shift is running on slot 0.

`shift` runs preflight, computes admission, runs every task on the first open admitted tier (the seed order in `budgets.toml` until a (class, tier) pair has `min_runs` of data, then measured pass rate and cost order), escalates once on `fail:capability`, blocks on `fail:structural`, parks on `fail:budget`, then commits the digest into the bench checkout and sends one ntfy line. `--tier` forces a tier for the bench: admission is skipped, envelopes and windows are not.

## The run, in one screen

| state | who moves it | evidence |
|---|---|---|
| queued → preflight → ready | runner | shift preflight passed; issue and branch exist |
| ready → executing | runner | executor VM started with `agent.py` and the task |
| executing ↔ verifying | runner, from the executor's `DARK:` tags | FILE blocks written and guards passed; `verify.sh` red with calls left |
| verifying → staging | runner | `AGENT-DONE ok`: green verify, only named paths staged, branch pushed |
| staging → pass / fail:capability | runner, from the stager's tag | hidden acceptance green / red in a fresh VM |
| → fail:budget | runner | envelope seconds exceeded with heartbeats; reasoning chars over the class cap |
| → fail:structural | runner | 5xx, crash, env, push denied, side effect, silent run, staging VM never reported |
| → abort | runner | `~/.dark/abort/<run>` or `all` |

The executor and stager never decide an outcome; they report a kind and the runner maps it through `spec.FAIL_KIND_OUTCOME`. Every transition is one `run.transition` event; every run is one `run.end`.

## Layout

```
dark/spec.py       classes, states, transitions, event registry, tag vocabulary, kind -> outcome
dark/config.py     models.toml, budgets.toml, host.toml, one-line refusals
dark/ledger.py     append-only JSONL, pass rate, p95, window and watts spend
dark/budget.py     envelopes (soft = p95 * (1 + headroom) within hard), windows, watts
dark/admission.py  admitted tiers per class, pick, single escalation
dark/preflight.py  every check a shift needs, waking the compute plane through the gate
dark/run.py        one run through the state table; the watchdog
dark/shift.py      a shift: preflight, admission, runs, digest
dark/digest.py     the one report and the one ntfy line
dark/tasks.py      task records, template + start overlay, force-pushed main, acceptance tarball
dark/agent.py      the executor (injected into the VM); the reviewed v1 contract plus heartbeat
dark/stager.py     the stager (injected into a fresh VM)
dark/vm.py         Proxmox over ssh; dark/gitea.py the API; dark/llm.py the model client
ops/               gitea-bootstrap, archive-v1, deploy, no-binaries
tests/             143 tests; the executor and stager run as real subprocesses behind the runner
```

`bash verify.sh` is the gate: tests, config validation, no binaries.

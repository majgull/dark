# Concepts

Every in-house term this repository uses, once, in plain words.

**dark factory**: the idea the project is named for, a factory that runs with
the lights off, where code is written and judged with nobody watching. The
runner is that factory's machinery.

**runner**: the pipeline. It takes one task from intake to a verdict: it
checks the deployment, runs the model in a throwaway machine, verifies the
result, pushes a branch, and runs the hidden tests in a second fresh machine.

**bench**: the runner's measuring instrument: the rules a task must satisfy,
the tools that validate a task set, the frozen limits, and the arms.

**task**: one unit of work: a spec, the files the executor starts from
(`start/`), a hidden acceptance test set, and an optional reference solution.
Its record is `tasks/<id>/task.toml`.

**hidden acceptance tests**: the test script and fixtures that judge a task.
They run in a fresh machine after the executor's machine is gone; the executor
never sees them. `run.sh` prints `CHECK <name> ok|fail` lines, and its exit
code is the verdict.

**oracle**: a worked reference solution for a task, used only to check that
the hidden tests are passable. It is never shown to an executor.

**mutant**: a copy of a reference solution broken on purpose. Every mutant
must fail at least one hidden check, which is how the tests are shown to
measure the thing they claim to.

**arm**: one way of putting a model to a task. The two arms here are `agent`
(the runner's own executor) and `session` (a coding-agent CLI solving the task
as a free session). The `user` arm checks a deployed application instead of
writing code (see user arm), and the `long` arm runs one session over several
repositories and has a reviewer session judge it (see long arm).

**class**: the kind of work a task declares at intake: `additive`,
`mechanical`, `repair`, `spec`, `review`, `long` or `user`. The class decides the limits and
the admission rules.

**tier**: one model entry in `models.toml`: a provider, a cost kind, a speed
or watts class, and roles. A tier is admitted to a class by its measured pass
rate.

**executor**: the program injected into the task machine. It clones the work
repository, asks the model for whole-file replacements, and pushes a branch.
It never decides an outcome; it reports what happened.

**harness**: the runner plus the deployment it runs on, as opposed to the
model under test. A "harness fault" is a failure in that machinery, not in the
model.

**control plane**: the calls the runner makes itself, outside a task machine:
preflight probes, spec review and validator calls.

**envelope**: the limit one run may spend: calls, seconds, reasoning
characters, and how many runs of a task are allowed.

**frozen envelope**: an envelope written to a file and pinned by its hash, so
every round of a comparison set runs under the same numbers instead of limits
recomputed from a ledger that keeps growing.

**shift**: one bounded stretch of work: a set of tasks run under one envelope,
admission rules and thinking level, ending in one digest.

**round**: one pass of one arm over the task set inside a shift.

**run**: one task on one tier by one arm, from preflight to one outcome. A run
writes exactly one opening record and one closing record.

**stager, staging machine**: the fresh machine that runs the hidden tests
against the branch a run pushed. Its verdict is trusted only when it carries a
one-time value the executor never saw.

**container target**: a sandbox that is a full clone of a real container,
taken from a named snapshot of it, on a bridge that reaches only the service
host. The `lxc` backend builds one, so a change to a running service can be
rehearsed on a copy of the container that runs it before it is applied to the
real one.

**admission**: the rule that decides which tiers may run which class, from the
ledger's measured pass rates, before any human tuning.

**ledger**: the append-only JSONL file where every shift, run, transition,
park and verdict is written. It is the only source of pass rates, window and
watts use, and admission.

**gate**: the service in front of the local models that also powers the model
host down when idle. It offers a keep-awake lease, which the runner takes for
the length of a run.

**preflight**: the checks before a shift starts: tokens, the ledger, the
templates, the Git host and org, every configured model id served, admission,
and the VM host reachable.

**window**: a provider's daily allowance of calls and tokens. A window is the
scarce thing on a subscription tier, where watts are the scarce thing on a
local tier.

**session arm**: an arm where a coding-agent CLI solves the task as a free
session with its own tools, inside the same task machine the runner builds.

**review mode**: a session-arm run whose input is a brief and a set of files
and whose only deliverable is `report.md`; no branch and no hidden tests.

**user arm**: an arm that checks a deployed application the way a user
would, from something that never saw its source. A user task (class `user`)
is a URL and a task written as numbered steps; a fresh sandbox built from the
browser image gets only those, no work repo and nothing to clone, and
`dark/user.py` asks the model for one browser action at a time, given the
step and the page's accessibility snapshot, until the step has a verdict. It
runs with `dark user --task <task.toml> --tier <id>`.

**step verdict**: the user arm's judgement of one step, `pass` or `fail` with
one line of note, recorded with a screenshot as `steps/<NN>.png` and a line
of `steps.jsonl` in the records repository. A user-arm run passes when every
step's verdict is pass; a failed step is `fail:capability`.

**long arm**: an arm that runs one session for hours over several
repositories and has a reviewer session judge it. A long task (class `long`)
is a spec and `repos`, each naming a repository (`name`, `url` and `base`);
nothing stages it, and its deliverable is the branches the session pushes.
A reviewer session then reads those branches and ends `report.md` with one
final `VERDICT: pass` or `VERDICT: fail` line, and that verdict is the run's
outcome. It runs with `python3 -m dark long --task <dir> --tier <id>`.

**pi**: the coding-agent CLI the session arm drives inside the machine.

**thinking level**: dark's own budget of visible thinking characters per call
(`none`, `low`, `medium`, `high`), cut live from the stream and the same for
every tier.

**digest**: the report one shift writes: runs by class and tier, pass rates,
window and watts used, the admission table, and what was parked or blocked.

**comparison set**: a group of rounds whose counts are meant to be read
against each other, held to one frozen envelope.

**chain**: a task that follows another: its starting tree is what the previous
step delivered, or that step's oracle when it failed.

**slot**: a small integer that gives two concurrent shifts distinct machine
ids.

**records repository**: a Git repository, one per shift, where a session arm
pushes its kept transcript, brief and task record.

**park, block, escalate**: the three non-terminal endings for a task in a
shift. Park means a limit was reached and the task waits for the next shift;
block means a structural failure stopped it; escalate means one retry on the
next admitted tier.

**adaptation**: admission, cooling and escalation taken together. A shift can
run with it off, which forces one named tier to do the whole task.

**provisional order**: the configured cheapest-first tier order used for a
(class, tier) pair until it has enough measured runs to be judged.

**MCP server**: `python3 -m dark mcp`, the program that speaks the Model Context Protocol over standard input and output, so an MCP client can list and call the six tools that drive dark's commands; `docs/mcp.md` says how to start and register it.

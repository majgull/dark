# dark/bench: ground truth first

The runner's measuring instrument: the rules a task must satisfy, the tools that validate a task set against its reference solutions and its deliberately broken copies, the frozen limits a run is held to, and the arms (the ways a model is put to a task). The task set itself is the repository [dark-tasks](https://github.com/majgull/dark-tasks); `tasks/` and `mutants/` here keep two example tasks and one example mutant for reference, so every tool runs without another checkout.

## A task

```
tasks/<id>/task.toml      id (= directory), title, class (additive | mechanical | repair), lang (go | python), spec, may_edit
tasks/<id>/start/         files laid over the language template before the run (optional)
tasks/<id>/acceptance/    hidden: run.sh (+ fixtures). Runs in the second, fresh VM at the repo root; prints `CHECK <name> ok|fail` lines; exit 0 = pass
tasks/<id>/oracle/        a reference solution overlay, used only to validate the acceptance set (never shown to any arm)
mutants/<id>/<name>/      a solution broken on purpose; every mutant must fail at least one hidden check
```

The executor sees the template, `start/`, and `spec`. Nothing else. `may_edit` is the only way an existing file may be rewritten, and additive tasks grant none.

## Chains

A task may follow another: `after = "<id>"` in its `task.toml`. Its starting tree is then the tree the named task delivered in the same run (the run branch of a pass, git log included) or, when that step failed or did not run, that step's oracle tree; its own `start/` lays over either. Chains run in id order, so ids carry a numeric prefix (`obs-01-parse` ... `obs-06-typehints` in the task set: one product built from an empty repo over six steps, three additive, two mechanical, one repair). Each step's acceptance re-runs the earlier steps' hidden checks as regression. `tools/validate.py` builds a chained step from its predecessors' oracles. Session arms run a chain with `tools/chain.sh <arm> <id>...` (continuity is the repository, one fresh session per step).

`obs-all` is the same product from one specification: the six parts in a single ticket, starting from the template, judged by the union of the chain's behaviour checks plus the last step's structure and annotation table. The two comparisons that need an earlier delivery (base untouched, module unchanged apart from annotations) are not in it, so a pass on `obs-all` and a pass on `obs-06-typehints` are not the same claim. It is the free-path measure beside the chain's fixed path: the chain says whether a model can take a ticket sequence, `obs-all` says whether it can choose the decomposition itself.

## Tools

Every tool reads the task set from `DARK_TASKS`, one root of a `dark-tasks` checkout or several joined with `:`, and unset, the examples here. A task name in two task sets is refused, naming both, rather than one silently winning.

```
python3 tools/check_tasks.py [path ...]  every tasks/<id> is a valid record; the CI gate
python3 tools/validate.py             each reference solution against its own hidden checks
python3 tools/mutants.py              each broken copy is caught by at least one hidden check
python3 tools/refcheck.py             a solved tree per task against the hidden checks
python3 tools/refwork.py              the spec-only workspace a second reference solution is written in
tools/arm.sh <arm> <id> <shift>       one session arm on one task, recorded under a shift id; tools/chain.sh runs a chain of them
```

`tools/arm.sh` launches its session through an agent-session launcher named by `DARK_SESSION_CMD`.

`frozen/` holds the limits a comparison set is held to (one worked example), `arms.toml` the arms, `probes/` the thinking-budget probe.

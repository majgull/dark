"""dark/shift.py — one shift: preflight, admission, runs, digest
(design §5, §6).

For each task: the cheapest admitted tier whose window or watts are open
takes one run inside the class envelope. fail:capability escalates once
to the next admitted tier if the envelope has a second run;
fail:structural blocks the task; fail:budget parks it for the next shift.
A forced tier (the bench) skips admission and the pick, never the
envelope or the window.
"""

import dataclasses
import os
import time

from . import admission, budget, digest, notify, preflight, spec, tasks
from . import gitea as G
from .run import Runner


# --- the launch a resumed shift repeats ---------------------------------------
# The arguments a shift was launched with. They are recorded in shift.start
# so that `--resume` continues the same round: the first resume smoke
# (2026-09-03) re-ran its kill-point tasks at think=low because the
# command line that resumed a think=none launch simply did not say --think.
LAUNCH_KEYS = ("tasks", "task_dir", "tier", "think", "arm", "slot", "work_org",
               # a comparison set is one limit and one adaptation setting: a resume
               # that quietly dropped either would put two regimes under one shift id
               "frozen", "no_adapt",
               # which executor runs inside the VM: the pipeline's, or a session
               "executor",
               # the stall cut (dark/agent.py); 0 = off, like max_stall itself
               "max_stall")


def launch_of(ledger, shift_id):
    """The launch arguments the shift with this id recorded, or None (a
    shift launched before the runner recorded them)."""
    out = None
    for e in ledger.events("shift.start"):
        if e.get("shift") == shift_id and e.get("launch"):
            out = e["launch"]
    return out


def resume_args(rec, given):
    """Merge a --resume command line into the launch it continues.

    Returns (args, None), or (None, refusal) when a value on this command
    line differs from the recorded one: both values are named, and the
    caller repeats the launch by leaving the option off.
    """
    out = {}
    for k in LAUNCH_KEYS:
        want, got = rec.get(k), given.get(k)
        if got is not None and got != want:
            return None, (f"{k} {got!r} differs from the launch's {want!r}: a resumed shift "
                          "repeats the launch it continues, it does not change it")
        out[k] = want
    return out, None


class Shift:
    def __init__(self, catalog, budgets, host, ledger, gitea, px, clock=time.time,  # noqa: PLR0913
                 sleep=time.sleep, log=print, arm="factory", chat=None, served=None, think=None,
                 resume=None, launch=None, frozen=None, no_adapt=False, executor="agent", max_stall=0):
        self.catalog = catalog
        self.think = think  # dark's thinking level for every run of this shift (None = each tier's default)
        # frozen: the pinned envelope this comparison set runs under (dark/frozen.py),
        # read instead of the ledger. no_adapt: admission, escalation and the retry
        # they carry are off, so the tier named on the command line does the whole task.
        self.frozen = frozen
        self.no_adapt = no_adapt
        self.max_stall = max_stall or 0  # this shift's stall cut, 0 = off; a frozen per-class pin wins
        self.budgets = budgets
        self.host = host
        self.ledger = ledger
        self.gitea = gitea
        self.px = px
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.arm = arm
        # seconds and the arm: two shifts launched in the same minute (the
        # 35B round and the variance subset, 13:46) shared an id and their
        # events merged in each other's digest
        # --resume reuses the id of the shift that broke, so its runs, its
        # digest and this attempt's runs are one shift in the ledger
        self.id = resume or (time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())) + f"-{arm}")
        self.resumed = bool(resume)
        self.launch = launch  # the command line, recorded in shift.start for --resume
        self.prior = []   # run.end records this shift wrote before the break
        self.runner = Runner(catalog, budgets, host, ledger, gitea, px, clock, sleep, log, self.id, arm)
        self.runner.frozen = frozen.as_dict() if frozen else None
        self.runner.executor = executor or "agent"
        kw = {}
        if chat:
            kw["chat"] = chat
        if served:
            kw["served"] = served
        self.pre = preflight.Preflight(catalog, budgets, host, ledger, gitea, px, clock, log,
                                       shift=self.id, sleep=sleep, **kw)
        self.results = []
        self.parked = []
        self.blocked = []
        self._based = set()  # tasks whose chain.base this shift has recorded (once, after the base is in place)

    # --- resume (decision 26) ---------------------------------------------------
    def load_prior(self):
        """The run.end records this shift id already holds, voided ones left
        out. A task with a pass among them is not run again, and a chained
        task starts from the branch that pass delivered."""
        skip = self.ledger.voided()
        self.prior = [r for r in self.ledger.events("run.end")
                      if r.get("shift") == self.id and r["run"] not in skip]
        return self.prior

    def done_pass(self, task_id):
        """The valid pass this shift already holds for `task_id`, or None.
        Valid: outcome pass, not voided, and a delivered branch on the
        record (a pass with no branch cannot seed the next step)."""
        for r in reversed(self.prior):
            if r["task"] == task_id and r["outcome"] == "pass" and r.get("branch"):
                return r
        return None

    # --- pieces -----------------------------------------------------------------
    def preflight(self, need_vm=True):
        checks = self.pre.run(need_vm=need_vm)
        line = preflight.refusal_line(checks)
        for c in checks:
            self.log("  " + c.line())
        if line:
            self.ledger.emit("preflight.refused", shift=self.id, check=[c.name for c in checks if not c.ok][0],
                             detail=line)
            notify.push(self.host.ntfy_url, line, title="dark: shift refused", log=self.log)
        else:
            self.ledger.emit("preflight.ok", shift=self.id, checks=[c.name for c in checks])
        return not line, line

    def chain_base(self, task):
        """Where a chained task starts (decision 17): ("delivered", branch)
        when the step it follows passed in this shift, ("oracle", None) when
        that step failed or did not run here, ("none", None) for a task that
        follows nothing."""
        if not task.after:
            return "none", None
        prev = [r for r in self.results if r.task == task.after]
        if prev and prev[-1].outcome == "pass" and prev[-1].branch:
            return "delivered", prev[-1].branch
        done = self.done_pass(task.after)  # resume: the pass this shift holds from before the break
        if done:
            return "delivered", done["branch"]
        return "oracle", None

    def ensure_work_repo(self, task):
        """The work repo of `task` in the work org, created if it is not
        there. `dark archive-work` leaves a redirect on the old path, so
        "the API answers here" is not "the repo is here"."""
        full = f"{self.host.work_org}/{task.repo_name}"
        here, why = self.gitea.repo_here(full)
        if not here:
            if why == "archived":
                raise tasks.TaskError(f"{full} is archived and cannot take a run: move it out of "
                                      f"{self.host.work_org} or run in a fresh work org")
            self.gitea.create_repo(self.host.work_org, task.repo_name, description=f"dark task {task.id}: {task.title}")
        return full

    def ensure_repo(self, task, base=("none", None)):
        full = self.ensure_work_repo(task)
        os.makedirs(self.host.scratch_dir, exist_ok=True)
        kind, branch = base
        clone = None
        if kind == "delivered":
            prev = tasks.predecessors(task)[-1]
            clone, prev_tree = tasks.fetch(self.gitea.push_url(f"{self.host.work_org}/{prev.repo_name}"), branch,
                                           self.host.scratch_dir, f"base-{task.repo_name}-{os.getpid()}")
            tree = tasks.work_tree(task, self.host.templates_dir, base_tree=prev_tree)
        elif kind == "oracle":
            prev = tasks.predecessors(task)[-1]
            tree = tasks.work_tree(task, self.host.templates_dir,
                                   base_tree=tasks.oracle_tree(prev, self.host.templates_dir))
        else:
            tree = tasks.work_tree(task, self.host.templates_dir)
        return tasks.materialize(task, tree, self.gitea.push_url(full), self.host.scratch_dir, base=clone)

    def one(self, task, tier, env):
        base = self.chain_base(task)
        if task.after:
            self.log(f"{task.id}: follows {task.after}, starts from its {base[0]} tree"
                     + (f" ({base[1]})" if base[1] else ""))
        try:
            self.ensure_repo(task, base)
        except (tasks.TaskError, G.GiteaError, OSError) as e:  # OSError: a git cwd gone, a clone's file (review 1.2, 3.1)
            self.ledger.emit("block", task=task.id, reason=f"materialize: {e}", shift=self.id)
            self.blocked.append((task.id, f"materialize: {e}"))
            self.log(f"{task.id}: blocked: materialize: {e}")
            return None
        if task.after and task.id not in self._based:
            # after the base is in place, once per task (an escalation re-materialises the same base)
            self._based.add(task.id)
            prev = tasks.predecessors(task)[-1]
            self.ledger.emit("chain.base", task=task.id, after=task.after, base=base[0], branch=base[1],
                             repo=f"{self.host.work_org}/{prev.repo_name}", shift=self.id)
        # the frozen file first: a comparison set pins the level with the limits,
        # so a shift launched without --think still runs at the set's level
        lvl = ((self.frozen.think(task.cls) if self.frozen and self.frozen.has(task.cls) else None)
               or self.think or self.budgets.cls(task.cls).think)  # else the tier's own, in the runner
        self.log(f"{task.id}: run on {tier} ({env.basis} envelope: {env.calls} calls, {env.seconds}s"
                 + (f", think {lvl}" if lvl else "") + ")")
        # slot: two shifts at once (a local tier's round and a cloud tier's)
        # need distinct VM ids; the session arms use slots 1 and 2 from the hub
        res = self.runner.run(task, tier, env, slot=getattr(self, "slot", 0), think=lvl,
                              base=base[0] if task.after else None)
        self.results.append(res)
        self.log(f"{task.id}: {res.outcome}" + (f" ({res.fail_kind})" if res.fail_kind else "")
                 + f" tier={tier} calls={res.calls} seconds={res.seconds} checks={res.checks_ok}/{res.checks_total}"
                 + (f" — {res.detail[:160]}" if res.detail else ""))
        return res

    def run(self, task_list, tier=None, max_runs=None, need_vm=True):
        if self.no_adapt and not tier:
            # admission is the rule that picks the tier from the ledger's pass
            # rates. With it off there is nothing left to pick with, so the tier
            # has to be named rather than guessed.
            line = "--no-adapt turns admission off: name the tier with --tier"
            self.log(line)
            self.ledger.emit("preflight.refused", shift=self.id, check="no-adapt", detail=line)
            return False
        extra = {"launch": self.launch} if self.launch else {}
        self.ledger.emit("shift.start", shift=self.id, host=os.uname().nodename, **extra)
        self.log(f"shift {self.id}: {len(task_list)} task(s), arm {self.arm}" + (f", tier {tier}" if tier else ""))
        if self.frozen:
            self.log(f"  frozen envelope {self.frozen.set_name} "
                     f"({os.path.basename(self.frozen.path)}, sha256 {self.frozen.sha256[:12]}): "
                     + "; ".join(f"{c} {v['calls']} calls, {v['seconds']}s, "
                                 f"{v['max_reasoning_chars']} reasoning chars, think {v['think']}"
                                 for c, v in sorted(self.frozen.classes.items())))
        if self.no_adapt:
            self.log("  adaptation off: no admission, no escalation, no retry on another tier")
        if self.runner.executor != "agent":
            self.log(f"  executor: {self.runner.executor} (dark/{self.runner.executor}.py in the VM)")
        ok, line = self.preflight(need_vm=need_vm)
        if not ok:
            self.log(line)
            self.ledger.emit("shift.end", shift=self.id, runs=0, passes=0)
            return False
        if self.resumed:
            self.load_prior()
            skipped = [t.id for t in task_list if self.done_pass(t.id)]
            self.ledger.emit("shift.resume", shift=self.id, runs=len(self.prior), skipped=skipped)
            self.log(f"resume {self.id}: {len(self.prior)} run(s) already recorded, "
                     f"{len(skipped)} task(s) already passed")
        if not self.no_adapt:
            table = admission.table(self.budgets, self.catalog, self.ledger, think=self.think)
            self.ledger.emit("admission", shift=self.id, table=table)
        max_runs = max_runs or self.budgets.shift["max_runs"]
        runs = 0
        day = budget.today(self.ledger)
        for task in task_list:
            done = self.done_pass(task.id)
            if done:
                self.log(f"{task.id}: resumed: passed here as {done['run']} on {done['tier']}, not run again")
                continue
            if runs >= max_runs:
                self.ledger.emit("park", task=task.id, reason="shift run cap", shift=self.id)
                self.parked.append((task.id, "shift run cap"))
                continue
            cooling = () if self.no_adapt else admission.cooling(self.ledger, self.clock())
            adm_rows = [] if tier else admission.rows(self.budgets, self.catalog, self.ledger, task.cls, think=self.think)
            adm = [tier] if tier else [r.tier for r in adm_rows if r.admitted]
            rates = {r.tier: r.rate for r in adm_rows}  # decision 20: the retry goes to the best open tier

            def env_for(t, cls=task.cls):  # the (class, tier) pair's own envelope, at the level it runs at
                if self.frozen and self.frozen.has(cls):
                    env = self.frozen.envelope(cls)   # pinned: the ledger does not size this run
                    stall = self.frozen.max_stall(cls)
                else:
                    think, legacy = budget.level(self.budgets, self.catalog, cls, t, self.think)
                    env = budget.envelope(self.budgets, self.ledger, cls, t, think=think, legacy=legacy)
                    stall = None
                # the frozen file's own pin wins when it has one; else this shift's
                # --max-stall, matching how a frozen calls/seconds/reasoning cap
                # already overrides the computed default
                stall = stall if stall is not None else self.max_stall
                return dataclasses.replace(env, max_stall=stall) if stall else env

            def is_open(t):
                return budget.tier_open(self.catalog, self.budgets, self.ledger, t, env_for(t), day)
            open_tiers, closed = admission.candidates(adm, cooling_tiers=cooling, is_open=is_open)
            if not open_tiers:
                reason = ("no admitted tier" if not adm else
                          "no open tier: " + "; ".join(closed + [f"{t}: cooling" for t in adm if t in cooling]))
                self.ledger.emit("park", task=task.id, reason=reason[:300], shift=self.id)
                self.parked.append((task.id, reason[:300]))
                self.log(f"{task.id}: parked: {reason}")
                continue
            first = open_tiers[0]
            env = env_for(first)
            res = self.one(task, first, env)
            runs += 1
            if res is None:
                continue
            if (res.outcome in spec.ESCALATES and env.runs >= 2 and not tier
                    and not self.no_adapt and runs < max_runs):
                nxt = admission.escalation(adm, first, cooling_tiers=admission.cooling(self.ledger, self.clock()),
                                           is_open=is_open, rates=rates)
                if nxt:
                    self.ledger.emit("escalate", task=task.id, run=res.run, frm=first, to=nxt)
                    self.log(f"{task.id}: escalating once {first} -> {nxt}")
                    res = self.one(task, nxt, env_for(nxt))
                    runs += 1
                    if res is None:
                        continue
                else:
                    self.log(f"{task.id}: no open tier to escalate to")
            if res.outcome == "fail:structural":
                self.ledger.emit("block", task=task.id, reason=f"{res.fail_kind}: {res.detail[:200]}", shift=self.id)
                self.blocked.append((task.id, f"{res.fail_kind}: {res.detail[:200]}"))
            elif res.outcome == "fail:budget":
                self.ledger.emit("park", task=task.id, reason=f"{res.fail_kind}: {res.detail[:200]}", shift=self.id)
                self.parked.append((task.id, f"{res.fail_kind}: {res.detail[:200]}"))
        passes = sum(1 for r in self.results if r.outcome == "pass")
        self.ledger.emit("shift.end", shift=self.id, runs=runs, passes=passes,
                         parked=len(self.parked), blocked=len(self.blocked))
        return True

    def report(self):
        """Digest text + the one ntfy line; publishing is the CLI's step."""
        return digest.render(self.ledger, self.catalog, self.budgets, self.id, self.clock())

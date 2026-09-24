"""dark/run.py — one run of one task on one tier, driven through the state
table from evidence.

The executor and the stager report WHAT happened (DARK: tags on the run's
issue); this module decides the outcome, and only through spec's tables:
every state change the table allows emits one run.transition, every run
ends in exactly one run.end, whatever arrives over the wire. Deadlines and
the watchdog are the runner's, never the VM's: a run over its envelope with
a live heartbeat is fail:budget; a silent run is fail:structural; an
abort marker file is abort.

Trust boundary: the executor VM runs model-written code
with the agent token, so anything it can post is untrusted. The stager's
verdict is accepted only with the nonce the runner gave the staging VM (the
executor never sees it) and only from a comment created after the staging
VM was spawned; AGENT-DONE is accepted only from a comment created after
the executor VM was spawned; a progress tag that asks for a move the table
has no row for is ignored, never a crash.
"""

import json
import os
import secrets
import time
from dataclasses import dataclass, field

from . import budget, gate, power
from . import gitea as G
from . import ledger as L
from . import spec, tasks, vm

HERE = os.path.dirname(os.path.abspath(__file__))
SKEW = 300  # seconds of Gitea-vs-runner clock skew tolerated on created_at checks


@dataclass
class RunResult:
    run: str
    task: str
    cls: str
    tier: str
    outcome: str
    fail_kind: str | None = None
    detail: str = ""
    seconds: int | None = 0    # executor VM lifetime (the cost column); None = not measured
    wall_seconds: int = 0      # queued -> terminal
    calls: int | None = 0
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    reasoning_chars: int | None = 0
    truncated: int | None = 0  # calls whose reply hit max_tokens
    cuts: int | None = 0       # calls whose thinking was cut at dark's level
    requests: int | None = 0   # provider requests (a cut call makes two)
    # distinct_calls/repeat_calls: replies whose sha256 was new/already seen this
    # run; stall_max: the longest run of consecutive identical replies. Only the
    # pipeline's FILE:/DELETE: executor tracks these (dark/agent.py); a session
    # arm reports None (not measured, not "zero repeats").
    distinct_calls: int | None = 0
    repeat_calls: int | None = 0
    stall_max: int | None = 0
    # tool_calls: calls the arm made to its OWN tools (read, bash, edit), not to
    # the model. The pipeline's executor has no tools and reports None; a
    # session arm counts them from its harness's own stream.
    tool_calls: int | None = None
    checks_ok: int = 0
    checks_total: int = 0
    issue: int | None = None
    branch: str = ""
    transitions: list = field(default_factory=list)
    # records: the records-repo path the session's stream, brief and task.json
    # were pushed to, or "PUSH FAILED: <error>"; records_sha256: sha256 of
    # stream.jsonl, set only when the push landed
    records: str | None = None
    records_sha256: str | None = None


def parse_tags(body):
    """DARK: lines of a comment body -> list of dicts (bad lines skipped)."""
    out = []
    for line in body.splitlines():
        if line.startswith(spec.TAG_PREFIX):
            try:
                out.append(json.loads(line[len(spec.TAG_PREFIX):]))
            except json.JSONDecodeError:
                continue
    return out


def _script(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def _created(c):
    try:
        return G.parse_time(c["created_at"])
    except (KeyError, ValueError, TypeError):
        return 0.0


class _Run:
    """The per-run state, transitions and the one run.end."""

    def __init__(self, runner, task, tier, arm, run_id, branch, t_queued):
        self.r = runner
        self.task = task
        self.tier = tier
        self.arm = arm
        self.model = runner.catalog.model(tier)
        self.full = f"{runner.host.work_org}/{task.repo_name}"
        self.t_queued = t_queued
        self.state = "queued"
        self.res = RunResult(run=run_id, task=task.id, cls=task.cls, tier=tier, outcome="abort", branch=branch)
        self.ended = False
        self.launched = {}  # name -> vmid, VMs not yet reaped
        self.reaps = {}     # name -> True iff the reap confirmed the VM gone

    def go(self, to, trigger):
        """One row of the table, or nothing: evidence that asks for a move the
        table has no row for is logged and ignored (never a crash)."""
        if not spec.transition_ok(self.state, to):
            self.r.log(f"WARN {self.res.run}: no row {self.state} -> {to} ({trigger}); ignored")
            return False
        self.r.ledger.emit("run.transition", task=self.task.id, run=self.res.run, frm=self.state, to=to,
                           trigger=" / ".join(trigger.split("\n"))[:120].strip() or to)
        self.res.transitions.append((self.state, to))
        self.state = to
        return True

    def end(self, outcome, kind=None, detail="", usage=None, reserved=None):
        """Exactly one run.end per run; the terminal transition is the
        outcome's row from the current state, or the wildcard abort row."""
        if self.ended:
            return self.res
        self.ended = True
        res = self.res
        to = outcome
        if not spec.transition_ok(self.state, to):
            self.r.log(f"WARN {res.run}: no row {self.state} -> {to}; recording as abort")
            to = "abort"
        self.go(to, detail or kind or outcome)
        res.outcome = to
        res.fail_kind = kind
        res.detail = detail[:400]
        res.wall_seconds = int(self.r.clock() - self.t_queued)
        if usage is not None:
            for k in ("seconds", "calls", "tokens_in", "tokens_out", "reasoning_chars", "truncated",
                      "cuts", "requests", "tool_calls", "records", "records_sha256",
                      "distinct_calls", "repeat_calls", "stall_max"):
                setattr(res, k, usage.get(k))
        reserved = reserved or {}
        pw = self.meter.stop() if getattr(self, "meter", None) else {}
        # a run.end the ledger refuses is a runner defect and must surface: with
        # `ended` already set, run()'s catch-all would otherwise return the result
        # with no record behind it
        self.r.ledger.emit(
            "run.end", task=self.task.id, run=res.run, cls=self.task.cls, tier=self.tier, outcome=to,
            seconds=res.seconds, calls=res.calls, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
            reasoning_chars=res.reasoning_chars, paid=self.model.paid,
            watts_class=self.model.watts if self.model.local else None,
            fail_kind=kind, detail=res.detail, checks_ok=res.checks_ok, checks_total=res.checks_total,
            arm=self.arm, shift=self.r.shift, wall_seconds=res.wall_seconds, issue=res.issue,
            branch=res.branch, reserved_calls=reserved.get("calls"), reserved_per_call=reserved.get("per_call"),
            truncated=res.truncated, cuts=res.cuts, requests=res.requests, tool_calls=res.tool_calls,
            think=getattr(self, "think", None), think_api=self.r.catalog.provider_of(self.tier).think_api,
            after=self.task.after, base=getattr(self, "base", None), repo=self.full,
            wh_cpu=pw.get("wh_cpu"), wh_gpu=pw.get("wh_gpu"), wh=pw.get("wh"), wh_overhead=pw.get("wh_overhead"),
            power=pw.get("power"), restaged_from=getattr(self, "restaged_from", None),
            reason=spec.structural_reason(kind), asserts=self.asserts(pw),
            capped=getattr(self, "capped", None), tests=self.r.tests_version(),
            records=res.records, records_sha256=res.records_sha256,
            distinct_calls=res.distinct_calls, repeat_calls=res.repeat_calls, stall_max=res.stall_max)
        try:
            if res.issue:
                self.r.gitea.issue_close(self.full, res.issue, f"RUN-END {to}"
                                         + (f" ({kind})" if kind else "") + f"\n{res.detail}")
        except G.GiteaError as e:
            self.r.log(f"issue close failed: {e}")
        return res

    def refuse(self, why):
        self.go("refused", why)
        self.res.outcome = "refused"
        self.res.detail = why[:400]
        self.ended = True
        return self.res

    def reap_all(self):
        for name, vmid in list(self.launched.items()):
            self.reaps[name] = bool(self.r.reap(vmid, name))
            self.launched.pop(name, None)

    def asserts(self, pw):
        """What the runner checked about its own machinery on this run: every
        configured energy sensor produced a reading, and every VM the run
        launched was confirmed gone. A null is nothing to check (no sensor
        configured, no VM launched); a false is a miss the record keeps, so
        a number that rests on a dead sensor cannot be read as measured."""
        p = pw.get("power") or {}
        sensors = {"cpu": (pw.get("wh_cpu") is not None) if p.get("cpu_host") else None,
                   "gpu": (pw.get("wh_gpu") is not None) if p.get("gpu_host") else None}
        checked = [v for v in sensors.values() if v is not None]
        vms = dict(self.reaps)
        return {"sensors": sensors, "sensors_alive": all(checked) if checked else None,
                "vms": vms, "vms_destroyed": all(vms.values()) if vms else None}


class Runner:
    def __init__(self, catalog, budgets, host, ledger, gitea, px, clock=time.time,
                 sleep=time.sleep, log=print, shift="adhoc", arm="factory", hold=None, meter=None):
        self.hold = hold if hold is not None else self._hold_gate
        # one energy meter per run window (dark/power.py); tests inject a fake
        self.meter = meter if meter is not None else (lambda: power.Meter(host.power_cpu_host, host.power_gpu_host))
        self.catalog = catalog
        self.budgets = budgets
        self.host = host
        self.ledger = ledger
        self.gitea = gitea
        self.px = px
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.shift = shift
        self.arm = arm
        # the frozen file this run's limits came from, as {set, file, sha256}, or
        # None when the envelope was computed. Set by the Shift, and by `dark
        # stage` for a session arm, so both arms of a comparison set say on every
        # run.start which pinned limit they ran under (dark/frozen.py).
        self.frozen = None
        self._tests = None
        self.agent_src = _script("agent.py")
        self.session_src = _script("session.py")
        self.stager_src = _script("stager.py")
        # "agent": the pipeline's FILE:/DELETE: executor. "session": pi in the
        # same VM with its own tools (dark/session.py). The two are the one
        # variable of a comparison, so both go through this same run driver.
        self.executor = "agent"
        # where session.py fetches node and pi: a file in a Gitea repo on the
        # service host, which is the one host a VM's egress group allows
        self.runtime_url = os.environ.get("DARK_RUNTIME_URL") or (
            f"{host.gitea_lan_url}/api/v1/repos/{host.org}/vm-runtime/media/runtime.tar.gz?ref=main")

    def tests_version(self):
        """The bench commit that judged this run, read once per Runner."""
        if self._tests is None:
            self._tests = tasks.tests_version(self.host.bench_dir) or ""
        return self._tests or None

    # --- VM launch (tests replace this) --------------------------------------
    def launch(self, vmid, name, files, runcmd):
        self.px.spawn(vmid, name, files, runcmd)

    def reap(self, vmid, name):
        try:
            return self.px.reap(vmid, name)
        except vm.VMError as e:
            self.log(f"reap {name}: {e}")
            return False

    def net_ip(self, vmid):
        return self.px.guest_ip(vmid)

    def _launch_networked(self, st, vmid, name, files, runcmd):
        """Spawn, then wait until the guest holds an address. A VM can boot,
        run cloud-init and never reach Gitea, holding no DHCP address; a
        silent VM costs the whole stage timeout and a data point, and a fresh
        clone fixes it. Returns the ip, or None after two silent boots.
        VMError from the spawn propagates to the caller."""
        wait = self.budgets.watchdog.get("net_wait_seconds", 180)
        for attempt in (1, 2):
            st.launched[name] = vmid
            self.launch(vmid, name, files, runcmd)
            t0 = self.clock()
            while True:
                ip = self.net_ip(vmid)
                if ip:
                    self.log(f"{name}: {ip} after {int(self.clock() - t0)}s")
                    return ip
                if self.clock() - t0 >= wait:
                    break
                self.sleep(min(5.0, wait))
            self.log(f"{name}: no address after {wait}s (boot {attempt}); reaping")
            self.reap(vmid, name)
            st.launched.pop(name, None)
        return None

    def _hold_gate(self, seconds):
        """Keep the compute plane awake for `seconds` (dark/gate.py); a gate
        that cannot be reached is logged, never fatal."""
        base = gate.control_base(self.catalog)
        if not base:
            return False, "no waking provider"
        ok, why = gate.hold(base, seconds)
        if not ok:
            self.log(f"keep-awake lease refused: {why}")
        return ok, why

    def _git_url(self):
        return self.host.git_lan_url or self.host.gitea_lan_url

    def _ensure_records_repo(self, shift=None):
        """dark-records/<shift>, created if absent: where a
        session-arm run of this shift pushes its kept stream. One repo per
        shift, so every run of it lands under its own <run-id>/ directory."""
        full = f"{self.host.records_org}/{shift or self.shift}"
        if not self.gitea.repo_exists(full):
            self.gitea.create_repo(self.host.records_org, shift or self.shift)
        return full

    # --- one run -----------------------------------------------------------------
    def run(self, task, tier, env, slot=0, think=None, base=None):
        t_queued = self.clock()
        run_id = f"{task.id}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t_queued))}"
        branch = f"run/{run_id}"
        st = _Run(self, task, tier, self.arm, run_id, branch, t_queued)
        st.think = think  # dark's thinking level for this run, None = provider default
        st.base = base    # a chained task's starting tree: delivered | oracle
        self.hold(env.seconds + task.stage_timeout + gate.MARGIN_SECONDS)
        st.meter = self.meter().start()  # energy over the whole window, executor to verdict
        try:
            return self._run(st, task, tier, env, slot)
        except L.LedgerError:
            raise  # a record the ledger refused: never a result without one
        except Exception as e:  # noqa: BLE001 — a runner defect must still end the run
            self.log(f"ERROR {run_id}: runner exception {e!r}")
            st.reap_all()
            if not st.ended:
                return st.end("fail:structural", "runner", f"runner: {e!r}")
            return st.res
        finally:
            st.reap_all()

    def _run(self, st, task, tier, env, slot):
        res, model, full = st.res, st.model, st.full
        wd = self.budgets.watchdog
        reserved = {"calls": env.calls, "per_call": budget.reservation(self.catalog, tier, env)[1] // max(env.calls, 1)}

        st.go("preflight", "shift start")
        try:
            res.issue = self.gitea.issue_create(
                full, f"run {res.run} [{tier}]",
                f"class: {task.cls}\ntier: {tier}\nenvelope: {json.dumps(env.as_dict())}\n"
                f"may edit: {', '.join(task.may_edit) or '-'}\n\n{task.spec}")
            self.gitea.delete_branch(full, res.branch)
        except G.GiteaError as e:
            return st.end("fail:structural", "gitea", f"gitea: {e}", reserved=reserved)
        st.go("ready", "issue and branch ready")
        # the shift's level, else the class's, else the tier's own
        think = getattr(st, "think", None) or self.budgets.cls(task.cls).think or model.think
        st.think = think  # the run.end records the level the run actually ran at
        prov = self.catalog.provider_of(tier)
        think_chars = self.budgets.think["levels"][think] if think else 0
        envelope = dict(env.as_dict(), **({"think": think, "think_chars": think_chars} if think else {}))
        self.ledger.emit("run.start", shift=self.shift, task=task.id, run=res.run, cls=task.cls,
                         tier=tier, envelope=envelope, arm=self.arm,
                         **({"frozen": self.frozen} if self.frozen else {}))

        agent_task = {
            "gitea": self.host.gitea_lan_url, "git_url": self._git_url(), "repo": full, "issue": res.issue,
            "branch": res.branch, "token": self.host.agent_token,
            "llm_url": prov.url, "llm_model": tier, "max_calls": env.calls, "max_stall": env.max_stall,
            "max_tokens": model.max_tokens,
            # with a level the run's cumulative thinking is the level times the
            # calls (each call may spend its budget); without one the class cap
            "max_reasoning_chars": think_chars * env.calls if think else env.max_reasoning_chars,
            "thinking_tokens": model.thinking_tokens,
            "think": think, "think_chars": think_chars, "think_api": prov.think_api,
            "think_presets": dict(model.think_presets),
            "chars_per_token": self.budgets.think["chars_per_token"],
            "llm_timeout": model.timeout, "temperature": self.catalog.defaults.get("temperature"),
            # streaming: the live thinking cut needs it, and through the gate
            # a non-streaming call can sit silent for its whole generation and
            # the VM-to-gate connection is found closed when the reply is
            # ready. Streaming keeps bytes moving.
            "llm_stream": bool(prov.stream or prov.wake),
            "may_edit": list(task.may_edit), "spec": task.spec,
            "heartbeat_seconds": wd["heartbeat_seconds"], "run": res.run, "task": task.id, "class": task.cls,
            # the session executor's own two: where its runtime comes from and
            # the wall limit it enforces on itself (the pipeline's executor is
            # cut off from outside instead, by the runner's watcher)
            "runtime_url": self.runtime_url, "max_seconds": env.seconds, "lang": getattr(task, "lang", None),
            "ctx": model.ctx,
            # where the session arm pushes its kept stream; only the session
            # executor implements this, so only it gets a repo
            "records_repo": self._ensure_records_repo() if self.executor == "session" else None}
        xvmid = self.budgets.shift["vmid_base"] + slot
        xname = f"dark-x{slot}"
        t_spawn = self.clock()
        try:
            src = self.session_src if self.executor == "session" else self.agent_src
            ip = self._launch_networked(st, xvmid, xname,
                                        {"/opt/task.json": (json.dumps(agent_task, indent=1), "0600"),
                                         "/opt/agent.py": (src, "0755")},
                                        [["bash", "-lc", "export HOME=/root; python3 /opt/agent.py >/var/log/agent.log 2>&1"]])
        except vm.VMError as e:
            return st.end("fail:structural", "env", f"executor VM: {e}", reserved=reserved)
        if not ip:
            return st.end("fail:structural", "env", "executor VM: no address after two boots", reserved=reserved)
        st.go("executing", f"executor VM started, {ip}")

        done_tag, verdict, calls_seen = self._watch_executor(st, env, t_spawn)
        seconds = int(self.clock() - t_spawn)
        st.reap_all()
        if verdict is not None:  # deadline / silent / abort / bad tag: the runner decided
            outcome, kind, detail = verdict
            return st.end(outcome, kind, detail, reserved=reserved,
                          usage={"seconds": seconds, "calls": calls_seen, "tokens_in": None,
                                 "tokens_out": None, "reasoning_chars": None, "truncated": None,
                                 "cuts": None, "requests": None, "tool_calls": None,
                                 "distinct_calls": None, "repeat_calls": None, "stall_max": None})

        usage = {"seconds": seconds, "calls": int(done_tag.get("calls") or 0),
                 "tokens_in": int(done_tag.get("tokens_in") or 0), "tokens_out": int(done_tag.get("tokens_out") or 0),
                 "reasoning_chars": int(done_tag.get("reasoning_chars") or 0),
                 "truncated": int(done_tag.get("truncated") or 0),
                 "cuts": int(done_tag.get("cuts") or 0), "requests": int(done_tag.get("requests") or 0),
                 # None, not 0: the pipeline's executor has no tools to call, and
                 # "no tools" and "nothing measured" must not read the same
                 "tool_calls": (int(done_tag["tool_calls"]) if done_tag.get("tool_calls") is not None
                                else None),
                 # None, not 0: only the pipeline's FILE:/DELETE: executor tracks
                 # stalls (dark/agent.py); a session-executor run has none to report
                 "distinct_calls": (int(done_tag["distinct_calls"]) if done_tag.get("distinct_calls") is not None
                                    else None),
                 "repeat_calls": (int(done_tag["repeat_calls"]) if done_tag.get("repeat_calls") is not None
                                  else None),
                 "stall_max": (int(done_tag["stall_max"]) if done_tag.get("stall_max") is not None else None),
                 "records": done_tag.get("records"), "records_sha256": done_tag.get("records_sha256")}
        if done_tag.get("outcome") != "ok":
            kind = done_tag.get("kind") or "crash"
            outcome = spec.FAIL_KIND_OUTCOME.get(kind, "fail:structural")
            detail = str(done_tag.get("error") or kind)
            body = getattr(st, "done_body", "")
            if body:
                # keep the tail: a verify.sh failure ends with the FAIL lines
                detail = f"{detail}: …{body[-360:]}" if len(body) > 360 else f"{detail}: {body}"
            if kind == "llm" and model.rate_limit_signature and model.rate_limit_signature in detail:
                self._rate_limited(tier)
            if kind in ("side-effect", "push") and st.state != "verifying":
                st.go("verifying", "verify.sh green (reported with the failure)")
            return st.end(outcome, kind, detail, usage=usage, reserved=reserved)

        if st.state != "verifying":
            st.go("verifying", "verify.sh green (reported with AGENT-DONE ok)")
        st.go("staging", "branch pushed; staging VM started")
        return self._stage(st, task, slot, usage=usage, reserved=reserved)

    # --- staging (shared by pipeline runs and session arms) -----------------------
    def _stage(self, st, task, slot, usage, reserved=None):
        res, full = st.res, st.full
        wd = self.budgets.watchdog
        nonce = secrets.token_hex(8)
        stage_task = {"gitea": self.host.gitea_lan_url, "git_url": self._git_url(), "repo": full,
                      "issue": res.issue, "branch": res.branch, "token": self.host.agent_token,
                      "run": res.run, "timeout": task.stage_timeout, "nonce": nonce,
                      "acceptance_tar_b64": tasks.acceptance_tar_b64(task)}
        svmid = self.budgets.shift["vmid_base"] + 50 + slot
        sname = f"dark-s{slot}"
        t_stage = self.clock()
        try:
            ip = self._launch_networked(st, svmid, sname,
                                        {"/opt/task.json": (json.dumps(stage_task), "0600"),
                                         "/opt/stager.py": (self.stager_src, "0755")},
                                        [["bash", "-lc", "export HOME=/root; python3 /opt/stager.py >/var/log/stager.log 2>&1"]])
        except vm.VMError as e:
            return st.end("fail:structural", "stage", f"staging VM: {e}", usage=usage, reserved=reserved)
        if not ip:
            return st.end("fail:structural", "stage", "staging VM: no address after two boots", usage=usage, reserved=reserved)
        tag = self._watch_stager(st, task.stage_timeout + wd["silent_kill_seconds"], nonce, t_stage)
        st.reap_all()
        if tag is None:
            return st.end("fail:structural", "stage", "staging VM never reported", usage=usage, reserved=reserved)
        res.checks_ok = int(tag.get("checks_ok") or 0)
        res.checks_total = int(tag.get("checks_total") or 0)
        detail = str(tag.get("detail") or "")
        if tag.get("ok"):
            return st.end("pass", None, detail, usage=usage, reserved=reserved)
        if tag.get("env") or "STAGE-ENV" in detail:
            return st.end("fail:structural", "stage", detail, usage=usage, reserved=reserved)
        return st.end("fail:capability", None, detail, usage=usage, reserved=reserved)

    # --- a session arm's result, staged like a pipeline run ---------------------
    def stage_only(self, task, branch, tier, arm, usage=None, slot=0, base=None,  # noqa: PLR0913
                   after_branch=None, capped=False, env=None):
        """Design §7b: a single session solved the task outside the runner and
        pushed `branch`; the same hidden acceptance runs in the same fresh VM
        shape. `usage` carries what the session's brain exposed; a missing
        count is None (NOT MEASURED), never estimated. `base` is what a
        chained step started from (delivered | oracle), as the arm reports it.
        `env` is the envelope the session was told to work inside, when there
        was one: a frozen comparison set holds both arms to the same limits,
        and without it the record would say the session arm had none."""
        usage = {k: (usage or {}).get(k) for k in ("seconds", "calls", "tokens_in", "tokens_out", "reasoning_chars",
                                                   "truncated", "cuts", "requests", "tool_calls")}
        t_queued = self.clock()
        run_id = f"{task.id}-{arm}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t_queued))}"
        st = _Run(self, task, tier, arm, run_id, branch, t_queued)
        st.base = base
        st.capped = capped or None  # the session was stopped at the default executor's wall cap, it did not finish
        # a re-judgement of a branch an earlier run delivered: the acceptance is
        # new, the run facts are not. Copy usage and level from that run.end
        # (found by branch) when the caller gave none, and name the run.
        prior = self.ledger.last("run.end", branch=branch)
        if prior and all(v is None for v in usage.values()):
            usage = {k: prior.get(k) for k in usage}
            st.think = prior.get("think")
            st.restaged_from = prior["run"]
        self.hold(task.stage_timeout + gate.MARGIN_SECONDS)
        try:
            st.go("preflight", "shift start")
            if task.after and base not in ("delivered", "oracle"):
                return st.refuse(f"{task.id} follows {task.after}: the arm must say what it started from (--base)")
            try:
                if not self.gitea.branch_exists(st.full, branch):
                    return st.end("fail:structural", "push", f"branch {branch} not on {st.full}", usage=usage)
                st.res.issue = self.gitea.issue_create(
                    st.full, f"stage {run_id} [{arm}: {tier}]",
                    f"class: {task.cls}\narm: {arm}\ntier: {tier}\nbranch: {branch}\n"
                    f"usage: {json.dumps(usage)}\n\n{task.spec}")
            except G.GiteaError as e:
                return st.end("fail:structural", "gitea", f"gitea: {e}", usage=usage)
            st.go("ready", "session arm: branch present")
            if task.after:
                prev = tasks.predecessors(task)[-1]
                self.ledger.emit("chain.base", task=task.id, after=task.after, base=base, branch=after_branch,
                                 repo=f"{self.host.work_org}/{prev.repo_name}", shift=self.shift)
            self.ledger.emit("run.start", shift=self.shift, task=task.id, run=run_id, cls=task.cls,
                             tier=tier, envelope=dict(env.as_dict(), arm=arm) if env else {"arm": arm},
                             arm=arm, **({"frozen": self.frozen} if self.frozen else {}))
            st.go("executing", "session arm: executed outside the runner")
            st.go("verifying", "session arm: the branch is the reply")
            st.go("staging", "staging VM started")
            return self._stage(st, task, slot, usage=usage)
        except L.LedgerError:
            raise  # as in run(): never a result without its record
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR {run_id}: runner exception {e!r}")
            st.reap_all()
            if not st.ended:
                return st.end("fail:structural", "runner", f"runner: {e!r}", usage=usage)
            return st.res
        finally:
            st.reap_all()

    # --- review mode: brief and files in, report.md out -------------------------
    def review(self, brief_text, files, tier, arm, shift=None, think=None, env=None, slot=0):
        """A session-arm run whose input is a brief and a set of files to
        read and whose deliverable is report.md; no hidden acceptance, no
        branch, no staging. The records repo is both this run's issue
        tracker (there is no per-task work repo) and its push target.
        `env` carries a frozen envelope when one was given; else computed
        from the "review" class the same way an exec class's envelope is."""
        t_queued = self.clock()
        shift = shift or self.shift
        run_id = f"review-{arm}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t_queued))}"
        full = self._ensure_records_repo(shift)
        task = tasks.Task(id=run_id, title="review", cls="review", lang="", spec=brief_text,
                          may_edit=(), dir="", stage_timeout=0, after=None)
        st = _Run(self, task, tier, arm, run_id, branch="", t_queued=t_queued)
        st.full = full
        st.think = think
        st.meter = self.meter().start()
        try:
            return self._review(st, task, files, slot, shift, env)
        except L.LedgerError:
            raise  # never a result without its record (as in run())
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR {run_id}: runner exception {e!r}")
            st.reap_all()
            if not st.ended:
                return st.end("fail:structural", "runner", f"runner: {e!r}")
            return st.res
        finally:
            st.reap_all()

    def _review(self, st, task, files, slot, shift, env):
        res, model, tier, full = st.res, st.model, st.tier, st.full
        wd = self.budgets.watchdog
        st.go("preflight", "shift start")
        try:
            res.issue = self.gitea.issue_create(
                full, f"review {res.run} [{tier}]",
                f"class: review\narm: {st.arm}\ntier: {tier}\n\n{task.spec}")
        except G.GiteaError as e:
            return st.end("fail:structural", "gitea", f"gitea: {e}")
        st.go("ready", "issue ready")

        think = getattr(st, "think", None) or self.budgets.cls("review").think or model.think
        st.think = think
        prov = self.catalog.provider_of(tier)
        think_chars = self.budgets.think["levels"][think] if think else 0
        if env is None:
            legacy = "none" if model.thinking_tokens == 0 else None
            env = budget.envelope(self.budgets, self.ledger, "review", tier, think=think, legacy=legacy)
        self.hold(env.seconds + gate.MARGIN_SECONDS)
        envelope = dict(env.as_dict(), **({"think": think, "think_chars": think_chars} if think else {}))
        self.ledger.emit("run.start", shift=shift, task=task.id, run=res.run, cls="review",
                         tier=tier, envelope=envelope, arm=st.arm,
                         **({"frozen": self.frozen} if self.frozen else {}))

        agent_task = {
            "gitea": self.host.gitea_lan_url, "git_url": self._git_url(), "repo": full,
            "records_repo": full, "issue": res.issue, "token": self.host.agent_token,
            "mode": "review", "spec": task.spec, "review_files": sorted(files),
            "llm_url": prov.url, "llm_model": tier, "max_calls": env.calls,
            "max_tokens": model.max_tokens,
            "max_reasoning_chars": think_chars * env.calls if think else env.max_reasoning_chars,
            "thinking_tokens": model.thinking_tokens,
            "think": think, "think_chars": think_chars, "think_api": prov.think_api,
            "think_presets": dict(model.think_presets),
            "chars_per_token": self.budgets.think["chars_per_token"],
            "llm_timeout": model.timeout, "temperature": self.catalog.defaults.get("temperature"),
            "llm_stream": bool(prov.stream or prov.wake),
            "heartbeat_seconds": wd["heartbeat_seconds"], "run": res.run, "task": task.id, "class": "review",
            "runtime_url": self.runtime_url, "max_seconds": env.seconds, "ctx": model.ctx}
        vm_files = {"/opt/task.json": (json.dumps(agent_task, indent=1), "0600"),
                   "/opt/agent.py": (self.session_src, "0755")}
        for rel, content in files.items():
            vm_files[f"/opt/work/{rel}"] = (content, "0644")
        xvmid = self.budgets.shift["vmid_base"] + slot
        xname = f"dark-x{slot}"
        t_spawn = self.clock()
        try:
            ip = self._launch_networked(st, xvmid, xname, vm_files,
                                        [["bash", "-lc", "export HOME=/root; python3 /opt/agent.py >/var/log/agent.log 2>&1"]])
        except vm.VMError as e:
            return st.end("fail:structural", "env", f"executor VM: {e}")
        if not ip:
            return st.end("fail:structural", "env", "executor VM: no address after two boots")
        st.go("executing", f"executor VM started, {ip}")

        done_tag, verdict, calls_seen = self._watch_executor(st, env, t_spawn)
        seconds = int(self.clock() - t_spawn)
        st.reap_all()
        if verdict is not None:  # deadline / silent / abort / bad tag: the runner decided
            outcome, kind, detail = verdict
            return st.end(outcome, kind, detail,
                          usage={"seconds": seconds, "calls": calls_seen, "tokens_in": None,
                                 "tokens_out": None, "reasoning_chars": None, "truncated": None,
                                 "cuts": None, "requests": None, "tool_calls": None})

        usage = {"seconds": seconds, "calls": int(done_tag.get("calls") or 0),
                 "tokens_in": int(done_tag.get("tokens_in") or 0), "tokens_out": int(done_tag.get("tokens_out") or 0),
                 "reasoning_chars": int(done_tag.get("reasoning_chars") or 0),
                 "truncated": int(done_tag.get("truncated") or 0),
                 "cuts": int(done_tag.get("cuts") or 0), "requests": int(done_tag.get("requests") or 0),
                 "tool_calls": (int(done_tag["tool_calls"]) if done_tag.get("tool_calls") is not None else None),
                 "records": done_tag.get("records"), "records_sha256": done_tag.get("records_sha256")}
        if done_tag.get("outcome") == "ok":
            return st.end("delivered", None, "", usage=usage)
        kind = done_tag.get("kind") or "crash"
        outcome = spec.FAIL_KIND_OUTCOME.get(kind, "fail:structural")
        detail = str(done_tag.get("error") or kind)
        body = getattr(st, "done_body", "")
        if body:
            detail = f"{detail}: …{body[-360:]}" if len(body) > 360 else f"{detail}: {body}"
        return st.end(outcome, kind, detail, usage=usage)

    # --- watching --------------------------------------------------------------
    def _abort_requested(self, run_id):
        d = self.host.abort_dir
        return os.path.exists(os.path.join(d, run_id)) or os.path.exists(os.path.join(d, "all"))

    def _clear_abort(self, run_id):
        try:
            os.remove(os.path.join(self.host.abort_dir, run_id))
        except OSError:
            pass

    def _rate_limited(self, tier):
        cd = self.budgets.watchdog.get("rate_cooldown_seconds", 900)
        self.ledger.emit("tier.cooldown", tier=tier, seconds=cd, reason="rate limit signature")
        self.ledger.emit("window.exhausted", provider=self.catalog.window_of(tier),
                         day=time.strftime("%Y-%m-%d", time.localtime(self.clock())))

    def _watch_executor(self, st, env, t_spawn):
        """Poll the run's issue until AGENT-DONE, the envelope deadline, a
        silent watchdog kill, or an abort marker. Mirrors the executor's
        progress tags into transitions as they appear. Returns
        (done_tag, None, calls) or (None, (outcome, kind, detail), calls),
        where calls is the number of model calls the progress tags showed
        started (the runner's own count of spend when it ends the run)."""
        task, res = st.task, st.res
        wd = self.budgets.watchdog
        poll = wd["poll_seconds"]
        deadline = t_spawn + env.seconds
        silent_after = wd["silent_kill_seconds"]
        last_beat = t_spawn
        seen = 0
        calls_seen = 0
        max_iters = int((env.seconds + silent_after) / poll) + 10
        for _ in range(max_iters):
            now = self.clock()
            if self._abort_requested(res.run):
                self._clear_abort(res.run)
                self.ledger.emit("abort", task=task.id, run=res.run)
                return None, ("abort", "abort", "abort marker file"), calls_seen
            try:
                comments = self.gitea.comments(st.full, res.issue)
            except G.GiteaError as e:
                self.log(f"poll {res.run}: {e}")
                comments = []
            fresh = [c for c in comments if _created(c) >= t_spawn - SKEW]
            alive = [c for c in fresh if c["body"].startswith("AGENT-ALIVE")]
            if alive:
                last_beat = max(last_beat, G.parse_time(alive[-1]["updated_at"]))
                tags = parse_tags(alive[-1]["body"])
                for t in tags:
                    if t.get("ev") in ("beat", "verify"):
                        calls_seen = max(calls_seen, int(t.get("started") or 0), int(t.get("calls") or 0))
                progress = [t for t in tags if t.get("ev") in ("verify", "refused")]
                for t in progress[seen:]:
                    if t["ev"] == "refused":
                        self.ledger.emit("guard.refused", task=task.id, run=res.run, paths=t.get("paths") or [])
                    elif t["ev"] == "verify":
                        st.go("verifying", f"iter {t.get('iter')}: FILE blocks written, guards passed")
                        if not t.get("ok"):
                            st.go("executing", f"iter {t.get('iter')}: verify.sh failed, calls left")
                seen = len(progress)
            done = [c for c in fresh if c["body"].startswith("AGENT-DONE")]
            if done:
                tags = [t for t in parse_tags(done[-1]["body"]) if t.get("ev") == "done"]
                if tags and spec.tag_ok("done", tags[-1]):
                    # the prose of the executor's verdict (its last verify.sh
                    # tail on a fail): what tells a capability failure from a
                    # broken environment when the digest is read later
                    st.done_body = "\n".join(l for l in done[-1]["body"].splitlines()
                                             if not l.startswith("DARK:")).strip()
                    return tags[-1], None, calls_seen
                return None, ("fail:structural", "crash", "AGENT-DONE without a valid done tag"), calls_seen
            if now - last_beat > silent_after:
                self.ledger.emit("watchdog.kill", task=task.id, run=res.run, silent_seconds=int(now - last_beat))
                return None, ("fail:structural", "silent", f"no heartbeat for {int(now - last_beat)}s"), calls_seen
            if now > deadline:
                return None, ("fail:budget", "seconds", f"envelope of {env.seconds}s exceeded"), calls_seen
            self.sleep(poll)
        return None, ("fail:structural", "silent", "watcher iteration cap reached"), calls_seen

    def _watch_stager(self, st, timeout, nonce, t_stage):
        """The stager's tag, or None. Only a STAGE-DONE created after the
        staging VM was spawned and carrying the run's nonce counts; anything
        else on the issue (the executor can post with the agent token) is
        ignored and logged."""
        poll = self.budgets.watchdog["poll_seconds"]
        t0 = self.clock()
        warned = set()
        for _ in range(int(timeout / poll) + 10):
            try:
                comments = self.gitea.comments(st.full, st.res.issue)
            except G.GiteaError as e:
                self.log(f"poll stage: {e}")
                comments = []
            for c in comments:
                if not c["body"].startswith("STAGE-DONE"):
                    continue
                tags = [t for t in parse_tags(c["body"]) if t.get("ev") == "stage"]
                tag = tags[-1] if tags else None
                if tag is None or tag.get("nonce") != nonce or _created(c) < t_stage - SKEW:
                    if c.get("id") not in warned:
                        warned.add(c.get("id"))
                        self.log(f"WARN {st.res.run}: ignored a STAGE-DONE without the run's nonce "
                                 f"(comment {c.get('id')}, created {c.get('created_at')})")
                    continue
                if spec.tag_ok("stage", tag):
                    return tag
                return {"ok": False, "checks_ok": 0, "checks_total": 0, "env": True,
                        "detail": "STAGE-ENV bad stage tag"}
            if self.clock() - t0 > timeout:
                return None
            self.sleep(poll)
        return None

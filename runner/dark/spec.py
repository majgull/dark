"""dark/spec.py - the runner's spec as data.

Everything the runtime ENFORCES about classes, states, outcomes and the
ledger's wire format lives in this one module, as tables. Tests pin the
tables; code imports them; nothing else may define a state, an outcome or
an event kind. An unregistered event kind fails loud at emit time.

Standalone by design: no imports from the rest of the package, so the
agent-side tag vocabulary can be pinned against agent.py and stager.py
(which are cloud-init-injected alone into VMs and stay self-contained).
"""

# --- classes: the kind of work, declared at intake, immutable ----------------
EXEC_CLASSES = ("additive", "mechanical", "repair")  # run in a VM: execute, verify, stage
CALL_CLASSES = ("spec", "review")                    # single budgeted control-plane calls
CLASSES = EXEC_CLASSES + CALL_CLASSES

# --- outcomes: the only terminal states of a run, decided by the runner ----
# delivered: the review class's pass - no hidden acceptance,
# just report.md landed non-empty; kept distinct from "pass" because nothing
# judged it.
OUTCOMES = ("pass", "delivered", "fail:capability", "fail:structural", "fail:budget", "abort")
FAIL_OUTCOMES = tuple(o for o in OUTCOMES if o not in ("pass", "delivered"))

# --- states of one run --------------------------------------------------------
STATES = ("queued", "preflight", "ready", "refused",
          "executing", "verifying", "staging") + OUTCOMES
TERMINAL = frozenset(OUTCOMES) | {"refused"}

# (from, to, trigger, decider). One row per bounded move; "*" = any
# non-terminal state. The runner emits exactly one `run.transition` event
# per row taken, from evidence (exit codes, tags), never from model prose.
TRANSITIONS = (
    ("queued", "preflight", "shift start", "runner"),
    ("preflight", "ready", "catalog, VM template, token, gate, ledger, windows all OK", "runner"),
    ("preflight", "refused", "any preflight check missed", "runner"),
    ("ready", "executing", "tier admitted and envelope open; executor VM started", "runner"),
    ("ready", "fail:structural", "executor VM could not start", "runner"),
    ("executing", "verifying", "executor emitted FILE/DELETE and the guards passed", "runner"),
    ("verifying", "executing", "verify.sh failed with calls left in the envelope", "runner"),
    ("verifying", "staging", "verify.sh green, only reply-named paths staged, branch pushed", "runner"),
    ("executing", "fail:capability", "calls exhausted without a green verify", "runner"),
    ("verifying", "fail:capability", "verify.sh failed and no calls left", "runner"),
    ("executing", "fail:budget", "envelope exhausted: seconds, or reasoning chars over the class cap", "runner"),
    ("verifying", "fail:budget", "envelope seconds exceeded while verifying or pushing", "runner"),
    ("executing", "fail:structural", "5xx, timeout on a warm model, crash, env miss, or a silent run", "runner"),
    ("verifying", "fail:structural", "push denied, side effect, or a silent run", "runner"),
    ("staging", "pass", "artefact ran its acceptance green in a fresh VM", "runner"),
    ("staging", "fail:capability", "acceptance red in the staging VM", "runner"),
    ("staging", "fail:structural", "staging VM never reported or could not build", "runner"),
    ("preflight", "fail:structural", "the work repo, issue or branch could not be prepared", "runner"),
    ("executing", "delivered", "review mode: report.md landed non-empty", "runner"),
    ("*", "abort", "abort marker file", "runner"),
)


def transition_ok(frm, to):
    """True iff (frm -> to) is a declared row (or the wildcard abort row)."""
    if frm in TERMINAL:
        return False
    return any((f == frm or f == "*") and t == to for f, t, _, _ in TRANSITIONS)


# --- the ledger's wire format: event kind -> (required, optional) fields ------
# Base fields are stamped by the ledger itself: ts, iso, v, kind. A field
# not listed here is refused (typos never become silent columns).
EVENTS = {
    # launch = the arguments the shift was launched with (tasks, task_dir,
    # tier, think, arm, slot, work_org); --resume repeats them, so a
    # resumed round cannot change its thinking level or its tier midway
    "shift.start": (("shift",), ("host", "launch")),
    "shift.end": (("shift", "runs", "passes"), ("parked", "blocked")),
    # a shift relaunched with --resume: runs = the records the id already
    # held, skipped = the tasks with a valid pass among them
    "shift.resume": (("shift", "runs", "skipped"), ()),
    "preflight.ok": (("shift", "checks"), ()),
    "preflight.refused": (("shift", "check", "detail"), ()),
    "admission": (("shift", "table"), ()),
    # frozen: {set, file, sha256} of the pinned envelope file this run read its
    # limits from (dark/frozen.py), absent when the envelope was computed from
    # the ledger. A comparison set is readable only if its rounds agree here.
    "run.start": (("shift", "task", "run", "cls", "tier", "envelope"), ("arm", "frozen")),
    "run.transition": (("task", "run", "frm", "to", "trigger"), ()),
    "run.end": (("task", "run", "cls", "tier", "outcome", "seconds", "calls",
                 "tokens_in", "tokens_out", "reasoning_chars", "paid", "watts_class"),
                ("fail_kind", "detail", "checks_ok", "checks_total", "arm", "shift",
                 "wall_seconds", "issue", "branch", "reserved_calls", "reserved_per_call",
                 # truncated: calls whose reply hit max_tokens; think: dark's level
                 # (None = provider default); think_api: how it reached the provider;
                 # cuts: calls whose thinking was cut at the level; requests: provider
                 # requests (a cut call makes two)
                 "truncated", "think", "think_api", "cuts", "requests",
                 # tool_calls: calls the arm made to its own tools, not to the
                 # model. null for the pipeline, whose executor has none
                 "tool_calls",
                 # tests: the bench commit whose acceptance judged this run
                 "tests",
                 # chains: after = the task this one follows; base =
                 # what it started from, delivered | oracle
                 "after", "base",
                 # measured energy over the run window (dark/power.py): wh_cpu = RAPL
                 # package on the Proxmox host, wh_gpu = nvidia-smi in the model VM,
                 # wh = their sum, wh_overhead = null (board, RAM, disks, PSU: NOT
                 # MEASURED without a wall meter); power = hosts, samples, errors
                 "wh_cpu", "wh_gpu", "wh", "wh_overhead", "power",
                 # restaged_from: this record re-judges the branch that run delivered;
                 # its usage and think are copied from that run.end, not measured here
                 "restaged_from",
                 # repo: the work repo the branch lives in (arms name their own, DARK_REPO_PREFIX)
                 "repo",
                 # reason: the named reason of a structural failure
                 # (spec.STRUCTURAL_REASONS), null for every other outcome
                 "reason",
                 # capped: the session arm was stopped at the pipeline's wall
                 # cap rather than finishing on its own (tools/arm.sh)
                 "capped",
                 # records: the records repository path the session's stream,
                 # brief and task.json were pushed to (dark-records/<shift>/
                 # <run>), or "PUSH FAILED: <error>" when the push did not
                 # land; records_sha256: sha256 of stream.jsonl, present only
                 # when the push succeeded
                 "records", "records_sha256",
                 # asserts: what the runner checked about its own machinery on
                 # this run — every energy sensor produced a reading, every VM
                 # the run launched was confirmed destroyed. null inside means
                 # nothing to check (no sensor configured, no VM launched);
                 # false means the check failed and the run says so
                 "asserts",
                 # distinct_calls/repeat_calls: replies whose sha256 was new to this
                 # run / already seen; stall_max: the longest run of consecutive
                 # identical replies. Only the pipeline's FILE:/DELETE: executor
                 # tracks these (dark/agent.py); null for a session-arm run
                 "distinct_calls", "repeat_calls", "stall_max")),
    # a chained task's starting tree: the delivered branch of the step it
    # follows (a pass in this shift) or that step's oracle tree
    "chain.base": (("task", "after", "base"), ("branch", "shift", "repo")),
    "guard.refused": (("task", "run", "paths"), ()),
    # a run whose outcome was the deployment's fault, not the tier's: the record
    # stays, the queries skip it, the digest lists it
    "run.void": (("task", "run", "reason"), ()),
    "stage.result": (("task", "run", "ok", "checks_ok", "checks_total"), ("detail",)),
    "escalate": (("task", "run", "frm", "to"), ()),
    "park": (("task", "reason"), ("shift",)),
    "block": (("task", "reason"), ("shift",)),
    "abort": (("task", "run"), ()),
    "watchdog.kill": (("task", "run", "silent_seconds"), ()),
    "window.exhausted": (("provider", "day"), ("detail",)),
    "tier.cooldown": (("tier", "seconds", "reason"), ()),
    # control-plane model calls: spec, review, validator checks, wake probes.
    # cls is a task class, or "probe" for preflight/wake calls (counted in
    # the window, never in a class's statistics).
    "call": (("shift", "cls", "tier", "seconds", "tokens_in", "tokens_out",
              "reasoning_chars", "paid"), ("task", "run", "purpose", "ok")),
    # dark bench: one manifest, five phases. bench.start
    # is written once per (name, manifest sha256); bench.round once per shift
    # the run phase launches; bench.pause/bench.resume are the only state a
    # pause or resume writes, read back before every shift of the run phase.
    "bench.start": (("name", "manifest_sha256", "tests_version", "envelope_sha256"), ()),
    "bench.round": (("name", "manifest_sha256", "round", "arm", "shift", "verified"), ()),
    "bench.pause": (("name",), ("reason",)),
    "bench.resume": (("name",), ()),
}
BASE_FIELDS = frozenset(("ts", "iso", "v", "kind"))
LEDGER_VERSION = 2


def event_ok(kind, fields):
    """(ok, problem). ok iff kind is registered, every required field is
    present, and no field is outside required + optional."""
    spec_entry = EVENTS.get(kind)
    if spec_entry is None:
        return False, f"unregistered event kind {kind!r}"
    req, opt = spec_entry
    have = set(fields) - BASE_FIELDS
    missing = [f for f in req if f not in have]
    if missing:
        return False, f"{kind}: missing {missing}"
    extra = sorted(have - set(req) - set(opt))
    if extra:
        return False, f"{kind}: undeclared field(s) {extra}"
    return True, ""


# --- agent-side tags: what the VM writes, what the runner parses -------------
# The executor keeps ONE progress comment (AGENT-ALIVE ..., PATCHed with
# `DARK:{json}` lines as it goes = the heartbeat) and posts ONE final
# AGENT-DONE comment. The stager posts ONE STAGE-DONE comment. Each DARK:
# line is {"v": 2, "ev": <kind>, ...}; required fields per kind below.
TAG_PREFIX = "DARK:"
AGENT_TAGS = {
    "start": (("model", "calls_max"), ()),
    "beat": (("at", "calls"), ("started",)),   # started: requests issued, incl. one in flight
    "verify": (("ok", "iter", "calls"), ("started",)),
    "refused": (("paths", "iter"), ()),
    "done": (("outcome", "calls", "tokens_in", "tokens_out", "reasoning_chars", "seconds"),
             # branches: [{repo, branch}] the session arm pushed, one per repository
             ("kind", "iter", "error", "files", "deletes", "branch", "branches", "truncated", "cuts",
              "requests", "tool_calls", "records", "records_sha256",
              "distinct_calls", "repeat_calls", "stall_max")),
    # env: the staging environment failed (never the work); nonce: the
    # runner's secret for this staging VM, echoed so the executor cannot forge it
    "stage": (("ok", "checks_ok", "checks_total"), ("detail", "seconds", "env", "nonce")),
}


def tag_ok(ev, fields):
    entry = AGENT_TAGS.get(ev)
    if entry is None:
        return False
    have = set(fields) | {"v", "ev"}
    return all(f in have for f in entry[0])


# The executor's failure kinds -> the run outcome. The VM reports WHAT
# happened (a kind); only the runner turns that into an outcome, and only
# through this table. Unknown kinds are structural: a failure the runner
# cannot classify is a runner defect, never a capability verdict.
FAIL_KIND_OUTCOME = {
    "calls": "fail:capability",       # call envelope spent, verify never green
    "stall": "fail:capability",       # a reply repeated one already seen: no new information could reach the model
    "no_blocks": "fail:capability",   # never produced a parseable FILE block
    "reasoning": "fail:budget",       # thinking output over the class cap
    "seconds": "fail:budget",         # runner-side deadline with heartbeats
    "push": "fail:structural",
    "crash": "fail:structural",
    "gitea": "fail:structural",        # the work repo, issue or branch could not be prepared
    "materialize": "fail:structural",  # the starting tree could not be built
    "runner": "fail:structural",       # a defect in the runner, not in the tier
    "side-effect": "fail:structural",
    "env": "fail:structural",
    "llm": "fail:structural",         # 5xx / connection failure from the model endpoint
    "silent": "fail:structural",      # watchdog kill
    "stage": "fail:structural",       # staging VM never reported / could not build
    "no_report": "fail:structural",   # review mode: report.md missing or empty
    "abort": "abort",
}
# Outcomes that may escalate once to the next admitted tier.
# fail:budget too: a run's budget failure (seconds or reasoning over the
# envelope) says this tier could not do the task inside its envelope, which
# is the same signal as calls exhausted, so it deserves the same one retry.
# Windows and watts still park before a run starts (tier_open), never
# through an outcome.
ESCALATES = frozenset({"fail:capability", "fail:budget"})

# Structural = a failure that is not the model's doing: everything before
# its first call and everything after its last push. The set was closed
# around what the executor could report, so a Gitea preparation failure or
# a missing delivered branch ended as a refusal with no record at all and
# left no reason anyone could read later. Every kind below is structural,
# is left out of every rate (ledger.pass_rate), and carries its reason into
# the record's `reason` field.
STRUCTURAL_REASONS = {
    "gitea": "the work repo, issue or branch could not be prepared",
    "materialize": "the starting tree could not be built or pushed",
    "env": "the executor or staging VM could not start, boot or reach the network",
    "llm": "the model endpoint failed: 5xx, connection refused, or a timeout on a warm model",
    "silent": "no heartbeat inside the watchdog window: the run was killed",
    "crash": "the executor script died or reported a verdict the runner cannot parse",
    "side-effect": "the executor changed something outside its grant",
    "push": "the delivered branch never reached the work repo",
    "stage": "the staging VM never reported or could not build",
    "runner": "a defect in the runner itself",
    "no_report": "review mode ended without a non-empty report.md",
}


def structural_reason(kind):
    """The named reason for a structural kind, or None for a kind that is
    the model's doing (calls exhausted, no parseable blocks, over budget)."""
    return STRUCTURAL_REASONS.get(kind)

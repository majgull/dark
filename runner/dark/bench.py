"""dark/bench.py - one manifest, five phases.

Part A: manifest parsing and validation, `--dry-run`, the run phase (a
generalised shift driver), and pause/resume through the ledger alone.
Phases bootstrap, smoke, report and archive are NOT BUILT here.
"""

import hashlib
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass

from . import tasks as T

PHASES = ("bootstrap", "smoke", "run", "report", "archive")
EXECUTORS = ("agent", "session")  # "agent": built-in FILE:/DELETE: executor; "session": a coding-agent CLI in the VM
THINK_LEVELS = ("none", "low", "medium", "high")
ORG_NAMES = ("work", "archive", "records", "results")
_HHMM = re.compile(r"^\d{2}:\d{2}$")


class ManifestError(Exception):
    """One line, naming the file and the field."""


@dataclass(frozen=True)
class Arm:
    name: str
    executor: str
    tier: str
    think: str


@dataclass(frozen=True)
class Manifest:
    path: str
    sha256: str
    name: str
    question: str
    tasks: tuple
    judge: str
    envelope: str
    rounds: int
    order: str
    arms: tuple
    orgs: dict
    smoke: dict | None
    stop: dict | None


# --- parsing and validation --------------------------------------------------

def _need_str(d, key, path, where):
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ManifestError(f"{path}: {where} needs a non-empty `{key}`")
    return v


def _need_pos_int(d, key, path, where):
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ManifestError(f"{path}: {where}.{key} must be a positive integer, got {v!r}")
    return v


def _need_str_list(d, key, path, where):
    v = d.get(key)
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise ManifestError(f"{path}: {where}.{key} must be a non-empty list of strings")
    return v


def _arm(d, i, path):
    where = f"[[arm]] #{i + 1}"
    if not isinstance(d, dict):
        raise ManifestError(f"{path}: {where} is not a table")
    name = _need_str(d, "name", path, where)
    executor = d.get("executor")
    if executor not in EXECUTORS:
        raise ManifestError(f"{path}: {where} ({name}).executor must be one of {EXECUTORS}, got {executor!r}")
    tier = _need_str(d, "tier", path, f"{where} ({name})")
    think = d.get("think")
    if think not in THINK_LEVELS:
        raise ManifestError(f"{path}: {where} ({name}).think must be one of {THINK_LEVELS}, got {think!r}")
    return Arm(name=name, executor=executor, tier=tier, think=think)


def _orgs(d, path):
    if not isinstance(d, dict):
        raise ManifestError(f"{path}: [orgs] is not a table")
    missing = [k for k in ORG_NAMES if not isinstance(d.get(k), str) or not d.get(k)]
    if missing:
        raise ManifestError(f"{path}: [orgs] is missing {missing}: needs all of {ORG_NAMES}")
    return {k: d[k] for k in ORG_NAMES}


def _smoke(d, path):
    if d is None:
        return None
    if not isinstance(d, dict):
        raise ManifestError(f"{path}: [smoke] is not a table")
    return {"tasks": tuple(_need_str_list(d, "tasks", path, "[smoke]")),
            "rounds": _need_pos_int(d, "rounds", path, "[smoke]")}


def _stop(d, path):
    if d is None:
        return None
    if not isinstance(d, dict):
        raise ManifestError(f"{path}: [stop] is not a table")
    out = {}
    if "calls_left_min" in d:
        out["calls_left_min"] = _need_pos_int(d, "calls_left_min", path, "[stop]")
    if "deadline" in d:
        dl = d.get("deadline")
        if not isinstance(dl, str) or not _HHMM.match(dl):
            raise ManifestError(f'{path}: [stop].deadline must be "HH:MM", got {dl!r}')
        out["deadline"] = dl
    return out


def parse(path):
    """Read and validate a manifest. Raises ManifestError with one line
    naming the file and the field on any miss."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ManifestError(f"{path}: {e.strerror or e}") from e
    sha = hashlib.sha256(raw).hexdigest()
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from e
    name = _need_str(doc, "name", path, "manifest")
    question = doc.get("question", "") or ""
    tasks_ = tuple(_need_str_list(doc, "tasks", path, "manifest"))
    judge = _need_str(doc, "judge", path, "manifest")
    envelope = _need_str(doc, "envelope", path, "manifest")
    rounds = _need_pos_int(doc, "rounds", path, "manifest")
    order = doc.get("order")
    if order != "alternate":
        raise ManifestError(f'{path}: `order` must be "alternate", got {order!r}')
    arm_list = doc.get("arm")
    if not isinstance(arm_list, list) or not arm_list:
        raise ManifestError(f"{path}: at least one [[arm]] is required")
    arms = tuple(_arm(a, i, path) for i, a in enumerate(arm_list))
    orgs = _orgs(doc.get("orgs"), path)
    smoke = _smoke(doc.get("smoke"), path)
    stop = _stop(doc.get("stop"), path)
    return Manifest(path=os.path.abspath(path), sha256=sha, name=name, question=question,
                    tasks=tasks_, judge=judge, envelope=envelope, rounds=rounds, order=order,
                    arms=arms, orgs=orgs, smoke=smoke, stop=stop)


# --- the plan (dry-run and the run phase share it) ---------------------------

def shift_command(manifest, arm):
    """The exact `dark shift` command line one round of `arm` runs.
    --no-adapt turns admission, cooling and escalation off for the round."""
    return (f"python3 -m dark shift --tasks {','.join(manifest.tasks)} --tier {arm.tier} "
            f"--arm {manifest.name}-{arm.name} --executor {arm.executor} "
            f"--frozen {manifest.envelope} --think {arm.think} --no-adapt --no-push")


def sequence(manifest):
    """[(round, arm), ...] in the manifest's order: order "alternate" means
    every arm once in round 1, then every arm once in round 2, and so on."""
    return [(r, arm) for r in range(1, manifest.rounds + 1) for arm in manifest.arms]


def dry_run(manifest, phases, bench=None):
    """The plan, no network: lines to print. A check that needs a file this
    host does not have prints NOT CHECKED instead of failing."""
    lines = [f"manifest {manifest.path}: sha256 {manifest.sha256}",
             f"phases selected: {', '.join(phases)}"]
    env_path = manifest.envelope
    if not os.path.isabs(env_path):
        env_path = os.path.join(os.path.dirname(manifest.path), env_path)
    if os.path.isfile(env_path):
        lines.append(f"envelope file: {env_path}")
    else:
        lines.append(f"NOT CHECKED: envelope file {manifest.envelope} (not found on this host)")
    if bench:
        roots = T.task_roots(bench)
        missing = [t for t in manifest.tasks
                   if not any(os.path.isdir(os.path.join(r, "tasks", t)) for r in roots)]
        if missing:
            lines.append(f"NOT CHECKED: bench checkout {bench}: tasks/ missing {missing}")
        else:
            lines.append(f"bench checkout: {bench} has all {len(manifest.tasks)} task(s)")
    else:
        lines.append(f"NOT CHECKED: bench checkout (tasks/) for {list(manifest.tasks)}")
    for p in phases:
        if p == "run":
            lines.append(f"run: {manifest.rounds} round(s) x {len(manifest.arms)} arm(s), order {manifest.order}")
            for r, arm in sequence(manifest):
                lines.append(f"  round {r} {arm.name}: {shift_command(manifest, arm)}")
        else:
            lines.append(f"{p}: NOT BUILT")
    lines.append("orgs: " + ", ".join(f"{k}={manifest.orgs[k]}" for k in ORG_NAMES))
    return lines


# --- pause and resume, ledger only -------------------------------------------

def is_paused(ledger, name):
    """True iff a bench.pause for `name` is newer than its bench.start and no
    bench.resume for `name` follows it. The ledger is the only state."""
    pauses = [e for e in ledger.events("bench.pause") if e.get("name") == name]
    if not pauses:
        return False
    last_pause = pauses[-1]
    starts = [e for e in ledger.events("bench.start") if e.get("name") == name]
    start_ts = starts[-1]["ts"] if starts else 0
    if last_pause["ts"] <= start_ts:
        return False
    resumes = [e for e in ledger.events("bench.resume") if e.get("name") == name]
    return not any(r["ts"] > last_pause["ts"] for r in resumes)


def verified_rounds(ledger, manifest):
    """{(round, arm name)} already holding a bench.round with verified true
    for this manifest's sha256: what --resume (and any re-entry) skips."""
    return {(e["round"], e["arm"]) for e in ledger.events("bench.round")
            if e.get("name") == manifest.name and e.get("manifest_sha256") == manifest.sha256
            and e.get("verified")}


# --- the run phase ------------------------------------------------------------

def run_phase(manifest, ledger, tests_version, envelope_sha256, run_shift, verify_round,
              calls_left=None, now_hhmm=None, log=print):
    """Rounds in the manifest's order, each verified against the ledger, the
    digest and Gitea (verify_round) before the next starts.

    run_shift(round, arm) -> shift id. verify_round(shift_id) -> (lines,
    mismatch, not_derivable). calls_left(arm) and now_hhmm() back the [stop]
    rules; either may be None to skip that rule (untestable in this
    environment, or the manifest does not set it).

    Returns True unless a round MISMATCHes; a stop, a pause or every round
    verifying are all a clean (True) return.
    """
    if ledger.last("bench.start", name=manifest.name, manifest_sha256=manifest.sha256) is None:
        ledger.emit("bench.start", name=manifest.name, manifest_sha256=manifest.sha256,
                    tests_version=tests_version, envelope_sha256=envelope_sha256)
    done = verified_rounds(ledger, manifest)
    for r, arm in sequence(manifest):
        if (r, arm.name) in done:
            log(f"round {r} {arm.name}: already verified, skipping")
            continue
        if is_paused(ledger, manifest.name):
            log(f"PAUSED before round {r} arm {arm.name}")
            return True
        if manifest.stop:
            if "calls_left_min" in manifest.stop and calls_left is not None:
                left = calls_left(arm)
                if left is not None and left < manifest.stop["calls_left_min"]:
                    log(f"stopping before round {r} arm {arm.name}: {left} calls left, "
                        f"need at least {manifest.stop['calls_left_min']}")
                    return True
            if "deadline" in manifest.stop and now_hhmm is not None:
                now = now_hhmm()
                if now is not None and now.replace(":", "") >= manifest.stop["deadline"].replace(":", ""):
                    log(f"stopping before round {r} arm {arm.name}: past deadline {manifest.stop['deadline']}")
                    return True
        shift_id = run_shift(r, arm)
        lines, mismatch, not_derivable = verify_round(shift_id)
        for line in lines:
            log(line)
        verified = not mismatch
        ledger.emit("bench.round", name=manifest.name, manifest_sha256=manifest.sha256,
                    round=r, arm=arm.name, shift=shift_id, verified=verified)
        log(f"{'PASS' if verified else 'FAIL'} round {r} {arm.name}: shift {shift_id}")
        if mismatch:
            return False
        if not_derivable:
            log(f"round {r} {arm.name}: counted; one check NOT DERIVABLE, see above")
    return True


# --- wiring the real world (the CLI uses these; tests inject fakes instead) --

def real_run_shift(catalog, budgets, host, ledger, gitea, px, manifest, task_path=None):
    """run_shift(round, arm) -> shift id, one shift launched in-process
    through dark.shift.Shift — the same path `dark shift` uses."""
    from . import frozen as frozen_mod
    from .shift import Shift

    def _run(round_no, arm):
        task_list = T.load_tasks(task_path or host.task_path(), only=list(manifest.tasks))
        fr, _ = frozen_mod.load(manifest.envelope), None
        arm_name = f"{manifest.name}-{arm.name}"
        launch = {"tasks": [t.id for t in task_list], "task_dir": None, "tier": arm.tier,
                  "think": arm.think, "arm": arm_name, "slot": 0, "work_org": host.work_org,
                  "frozen": fr.path, "no_adapt": True, "executor": arm.executor}
        sh = Shift(catalog, budgets, host, ledger, gitea, px, arm=arm_name, think=arm.think,
                   launch=launch, frozen=fr, no_adapt=True, executor=arm.executor)
        sh.run(task_list, tier=arm.tier)
        return sh.id
    return _run


def real_verify_round(repo_root, manifest, conf=None):
    """verify_round(shift_id) -> (lines, mismatch, not_derivable), via
    ops/verify-round.py, the same check the run phase already runs."""
    def _verify(shift_id):
        cmd = ["python3", os.path.join(repo_root, "ops", "verify-round.py"),
               "--shift", shift_id, "--tasks", ",".join(manifest.tasks)]
        if conf:
            cmd += ["--conf", conf]
        r = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        lines = (r.stdout + r.stderr).splitlines()
        mismatch = any("MISMATCH" in line for line in lines)
        not_derivable = any("NOT DERIVABLE" in line for line in lines)
        return lines, mismatch, not_derivable
    return _verify


def real_calls_left(catalog, budgets, ledger):
    """calls_left(arm) -> the calls left in arm.tier's provider window today
    (None for a local tier, which has no window, only watts)."""
    from . import budget as B

    def _left(arm):
        wname = catalog.window_of(arm.tier)
        if not wname:
            return None
        day = B.today(ledger)
        return B.windows(budgets, catalog, ledger, day)[wname].calls_left
    return _left


def real_now_hhmm():
    return time.strftime("%H:%M", time.localtime())

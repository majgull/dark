"""python3 -m dark <command> — the runner's CLI. Every command exits 0 on
success and prints one line per problem otherwise; no tracebacks for
config or preflight misses."""

import argparse
import json
import os
import sys

from . import admission, budget, config, digest, frozen as frozen_mod, notify, preflight, sandbox, tasks
from . import gitea as G
from . import ledger as L
from . import vm
from . import shift as shift_mod
from .shift import Shift

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(args):
    catalog, budgets = config.load(args.conf)
    host = config.load_host(os.path.join(args.conf, "host.toml"))
    host.admin_token = G.read_token(host.admin_token_file)
    host.agent_token = G.read_token(host.agent_token_file)
    return catalog, budgets, host


def _ctx(args):
    catalog, budgets, host = _load(args)
    ledger = L.Ledger(host.ledger_path)
    gitea = G.Gitea(host.gitea_url, host.admin_token, lan_url=host.gitea_lan_url)
    px = sandbox.make(host, budgets.shift["vm_template"])
    return catalog, budgets, host, ledger, gitea, px


def cmd_bench(args):
    """python3 -m dark bench <manifest.toml> [--phases ...] [--dry-run]
    [--bench <path>] [--pause | --resume]. Part A
    builds the manifest, --dry-run, the run phase and pause/resume; every
    other phase prints NOT BUILT."""
    from . import bench as B
    try:
        manifest = B.parse(args.manifest)
    except B.ManifestError as e:
        print(f"manifest: {e}")
        return 2
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    unknown = [p for p in phases if p not in B.PHASES]
    if unknown:
        print(f"bench: unknown phase(s) {unknown}: choose from {B.PHASES}")
        return 2
    if args.dry_run:
        for line in B.dry_run(manifest, phases, bench=args.bench):
            print(line)
        return 0
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    if args.pause:
        ledger.emit("bench.pause", name=manifest.name)
        print(f"bench.pause: {manifest.name}")
        return 0
    if args.resume:
        ledger.emit("bench.resume", name=manifest.name)
        print(f"bench.resume: {manifest.name}")
    rc = 0
    for phase in phases:
        if phase != "run":
            print(f"{phase}: NOT BUILT")
            continue
        bench_dir = args.bench or host.bench_dir
        try:
            envelope_sha256 = frozen_mod.load(manifest.envelope).sha256
        except frozen_mod.FrozenError as e:
            print(f"run: {e}")
            return 2
        tests_version = tasks.tests_version(bench_dir)
        run_shift = B.real_run_shift(catalog, budgets, host, ledger, gitea, px, manifest, bench_dir=bench_dir)
        verify_round = B.real_verify_round(HERE, manifest, conf=args.conf)
        calls_left = B.real_calls_left(catalog, budgets, ledger)
        ok = B.run_phase(manifest, ledger, tests_version, envelope_sha256, run_shift, verify_round,
                         calls_left=calls_left, now_hhmm=B.real_now_hhmm, log=print)
        rc = 0 if ok else 1
    return rc


def cmd_check_config(args):
    try:
        catalog, budgets, host = _load(args)
    except config.ConfigError as e:
        print(f"config: {e}")
        return 2
    print(f"config OK: {len(catalog.models)} models across {len(catalog.providers)} providers, "
          f"{len(budgets.classes)} classes, windows {sorted(budgets.windows)}; host org {host.org}, "
          f"state {host.state_dir}")
    return 0


def cmd_preflight(args):
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    pre = preflight.Preflight(catalog, budgets, host, ledger, gitea, px)
    checks = pre.run(need_vm=not args.no_vm)
    for c in checks:
        print(c.line())
    line = preflight.refusal_line(checks)
    if line:
        print(line)
        return 2
    print("preflight OK")
    return 0


def cmd_admission(args):
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    table = admission.table(budgets, catalog, ledger)
    for cls, rows in table.items():
        lvl = budgets.cls(cls).think
        print(f"{cls}" + (f" (runs at think {lvl}; rates over runs at that level)" if lvl else "") + ":")
        for r in rows:
            print(f"  {'*' if r['admitted'] else ' '} {r['tier']:34} {r['basis']:15} "
                  f"rate={r['rate'] if r['rate'] is not None else '-':<5} n={r['n']}")
    return 0


def cmd_status(args):
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    day = budget.today(ledger)
    for name, w in budget.windows(budgets, catalog, ledger, day).items():
        print(f"window {name}: {json.dumps(w.as_dict())}")
    print(f"watts: {json.dumps(budget.watts(budgets, catalog, ledger, day).as_dict())}")
    for cls in budgets.classes:
        env = budget.envelope(budgets, ledger, cls)
        print(f"envelope {cls}: {json.dumps(env.as_dict())}")
    last = ledger.last("shift.end")
    if last:
        print(f"last shift: {last['shift']} runs={last['runs']} passes={last['passes']}")
    return 0


def _task_list(args, host):
    if getattr(args, "task_dir", None):
        return [tasks.load_task(args.task_dir)]
    only = [t for t in (args.tasks or "").split(",") if t] or None
    return tasks.load_tasks(args.bench or host.bench_dir, only=only)


def _resume_launch(args, ledger, host):
    """--resume repeats the launch it continues. The recorded arguments fill
    in every option this command line left off; an option that says
    something else is refused with both values printed. Returns a refusal
    line or None."""
    rec = shift_mod.launch_of(ledger, args.resume)
    if rec is None:
        return (f"resume {args.resume}: that shift recorded no launch arguments (it ran before the "
                "runner recorded them); relaunch it by hand with the same tasks, tier and think level")
    given = {"tasks": [s.strip() for s in args.tasks.split(",")] if args.tasks else None,
             "task_dir": args.task_dir, "tier": args.tier, "think": args.think,
             "arm": args.arm, "slot": args.slot, "work_org": host.work_org}
    merged, why = shift_mod.resume_args(rec, given)
    if why:
        return f"resume {args.resume}: {why}"
    if merged["work_org"] != host.work_org:
        return (f"resume {args.resume}: work_org {host.work_org!r} differs from the launch's "
                f"{merged['work_org']!r}: point DARK_WORK_ORG or host.toml at the launch's org")
    args.tasks = ",".join(merged["tasks"]) if merged["tasks"] else None
    for k in ("task_dir", "tier", "think", "arm", "slot", "frozen", "no_adapt", "executor", "max_stall"):
        setattr(args, k, merged[k])
    return None


def _frozen(args, budgets, task_list=None):
    """(Frozen or None, refusal line or ""). A frozen file that does not cover
    a class the shift will run is refused rather than filled in: a comparison
    set with one class quietly on the moving basis is the defect the file
    exists to stop."""
    if not getattr(args, "frozen", None):
        return None, ""
    try:
        fr = frozen_mod.load(args.frozen)
    except frozen_mod.FrozenError as e:
        return None, f"frozen: {e}"
    why = frozen_mod.check_levels(fr, budgets)
    if why:
        return None, f"frozen: {why}"
    if task_list is not None:
        missing = frozen_mod.covers(fr, task_list)
        if missing:
            return None, (f"frozen: {args.frozen} pins no envelope for class(es) "
                          f"{', '.join(missing)}, which {len(task_list)} task(s) of this shift use")
    return fr, ""


def cmd_shift(args):
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    if args.resume:
        why = _resume_launch(args, ledger, host)
        if why:
            print(why)
            return 2
    if args.arm is None:
        args.arm = "factory"
    if args.slot is None:
        args.slot = 0
    try:
        task_list = _task_list(args, host)
    except tasks.TaskError as e:
        print(f"tasks: {e}")
        return 2
    fr, why = _frozen(args, budgets, task_list)
    if why:
        print(why)
        return 2
    launch = {"tasks": [t.id for t in task_list], "task_dir": args.task_dir, "tier": args.tier,
              "think": args.think, "arm": args.arm, "slot": args.slot, "work_org": host.work_org,
              # the file's path, so --resume reads the same file, and the runner
              # refuses if its bytes changed: the sha256 goes on every run.start
              "frozen": fr.path if fr else None, "no_adapt": args.no_adapt,
              "executor": args.executor, "max_stall": args.max_stall or 0}
    sh = Shift(catalog, budgets, host, ledger, gitea, px, arm=args.arm, think=args.think,
               resume=args.resume, launch=launch, frozen=fr, no_adapt=bool(args.no_adapt),
               executor=args.executor or "agent", max_stall=args.max_stall or 0)
    sh.slot = args.slot
    ran = sh.run(task_list, tier=args.tier, max_runs=args.max_runs)
    text, one = sh.report()
    bench = args.bench or host.bench_dir
    rel = digest.publish(text, sh.id, host.ledger_path, bench,
                         push_url=gitea.push_url(f"{host.org}/bench") if not args.no_push else None)
    link = f"{host.gitea_lan_url}/{host.org}/bench/src/branch/main/{rel}"
    notify.push(host.ntfy_url, f"{one}\n{link}", title="dark shift")
    print(text)
    print(one)
    return 0 if ran else 2


def cmd_materialize(args):
    """Create dark/t-<id> if missing and force-push its starting tree to main
    (what a shift does before each run), so a session arm can clone it."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    try:
        task_list = _task_list(args, host)
    except tasks.TaskError as e:
        print(f"tasks: {e}")
        return 2
    sh = Shift(catalog, budgets, host, ledger, gitea, px, log=print)
    if args.after_branch and sum(1 for t in task_list if t.after) != 1:
        print("materialize: --after-branch names one branch of one predecessor; give exactly one chained task")
        return 2
    for t in task_list:
        base = ("none", None)
        if t.after:
            base = ("delivered", args.after_branch) if args.after_branch else ("oracle", None)
        sha = sh.ensure_repo(t, base)
        print(f"{host.work_org}/{t.repo_name} main = {sha[:8]} ({t.cls}, {t.lang})"
              + (f", after {t.after} from its {base[0]} tree" + (f" {base[1]}" if base[1] else "") if t.after else ""))
    return 0


def cmd_stage(args):
    """Stage a branch a session arm pushed: same hidden
    acceptance, same fresh VM, one run.end with arm=<arm>."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    try:
        task = tasks.load_task(args.task_dir) if args.task_dir else \
            tasks.load_tasks(args.bench or host.bench_dir, only=[args.task])[0]
    except tasks.TaskError as e:
        print(f"tasks: {e}")
        return 2
    usage = {k: getattr(args, k) for k in ("calls", "tokens_in", "tokens_out", "reasoning_chars", "seconds")}
    # the compute plane may be asleep: a shift wakes it in preflight, a
    # staging on its own must do the same or every VM spawn is "no route"
    pre = preflight.Preflight(catalog, budgets, host, ledger, gitea, px, shift=args.shift or "adhoc")
    if not px.reachable() and not pre.wake():
        print(json.dumps({"run": None, "outcome": "refused", "detail": f"{host.proxmox}: unreachable and the wake failed"}))
        return 2
    fr, why = _frozen(args, budgets, [task])
    if why:
        print(json.dumps({"run": None, "outcome": "refused", "detail": why}))
        return 2
    from .run import Runner
    runner = Runner(catalog, budgets, host, ledger, gitea, px, shift=args.shift or "adhoc", arm=args.arm)
    runner.frozen = fr.as_dict() if fr else None
    res = runner.stage_only(task, args.branch, args.tier, args.arm, usage, slot=args.slot, base=args.base,
                            after_branch=args.after_branch, capped=args.capped,
                            env=fr.envelope(task.cls) if fr else None)
    print(json.dumps({"run": res.run, "outcome": res.outcome, "fail_kind": res.fail_kind,
                      "checks": f"{res.checks_ok}/{res.checks_total}", "detail": res.detail, "issue": res.issue}))
    return 0 if res.outcome == "pass" else 1


def cmd_review(args):
    """Launch one session-arm run in review mode: a brief
    and a set of files in, report.md out, pushed to the records repo beside
    the kept stream. No staging: outcome is delivered iff report.md landed."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    try:
        with open(args.brief) as f:
            brief_text = f.read()
    except OSError as e:
        print(f"review: {args.brief}: {e.strerror}")
        return 2
    files = {}
    for i, path in enumerate(p.strip() for p in (args.files or "").split(",") if p.strip()):
        try:
            with open(path) as f:
                content = f.read()
        except OSError as e:
            print(f"review: {path}: {e.strerror}")
            return 2
        name = os.path.basename(path) or f"file{i}"
        files[f"{i}-{name}" if name in files else name] = content
    fr, why = _frozen(args, budgets)
    if why:
        print(why)
        return 2
    pre = preflight.Preflight(catalog, budgets, host, ledger, gitea, px, shift=args.shift or "adhoc")
    if not px.reachable() and not pre.wake():
        print(json.dumps({"run": None, "outcome": "refused", "detail": f"{host.proxmox}: unreachable and the wake failed"}))
        return 2
    from .run import Runner
    runner = Runner(catalog, budgets, host, ledger, gitea, px, shift=args.shift or "adhoc", arm=args.arm)
    runner.frozen = fr.as_dict() if fr else None
    env = fr.envelope("review") if fr else None
    think = (fr.think("review") if fr else None) or args.think
    res = runner.review(brief_text, files, args.tier, args.arm, shift=args.shift, think=think, env=env)
    print(json.dumps({"run": res.run, "outcome": res.outcome, "fail_kind": res.fail_kind,
                      "detail": res.detail, "issue": res.issue, "records": res.records}))
    return 0 if res.outcome == "delivered" else 1


WORK_PREFIXES = ("t-", "tl-", "session-")


def cmd_archive_work(args):
    """Move every task work repo (t-*, tl-*, session-*) out of the work org
    into an archive org and archive it there, so the org the next
    experiment writes into holds nothing from the last one. Dry run unless
    --apply. The code repos (runner, bench, templates) never move."""
    _, _, host, _, gitea, _ = _ctx(args)
    src = args.src or host.work_org
    names = sorted(n for n in gitea.repos_of_org(src) if n.startswith(WORK_PREFIXES))
    print(f"{len(names)} work repo(s) in {src}" + ("" if args.apply else " (dry run; --apply to move)"))
    if not args.apply:
        for n in names:
            print(f"  {src}/{n} -> {args.to}/{n} (archived)")
        return 0
    if not gitea.org_exists(args.to):
        gitea.create_org(args.to)
        print(f"created org {args.to}")
    for n in names:
        try:
            full = gitea.transfer_repo(f"{src}/{n}", args.to)
            gitea.archive_repo(full)
            print(f"  {src}/{n} -> {full} archived")
        except G.GiteaError as e:
            print(f"  {src}/{n}: FAILED {e}")
            return 1
    return 0


def cmd_power(args):
    """One live reading per energy sensor (dark/power.py): package watts on
    the Proxmox host over --seconds, and each GPU's draw in the model VM."""
    from . import power
    _, _, host = _load(args)
    out = power.probe(host.power_cpu_host, host.power_gpu_host, args.seconds)
    out["cpu_host"], out["gpu_host"] = host.power_cpu_host or None, host.power_gpu_host or None
    print(json.dumps(out))
    return 0 if (out["cpu_watts"] is not None or out["gpu_watts"] is not None) else 1


def cmd_done(args):
    """The verdict a shift already holds for a task: one JSON line, exit 0
    iff it is a valid pass (not voided, with a delivered branch). This is
    how a session arm resumes: a step whose branch already has a verdict is
    not run again, and the branch it delivered goes to the next step."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    skip = ledger.voided()
    rs = [r for r in ledger.events("run.end")
          if r.get("shift") == args.shift and r["task"] == args.task and r["run"] not in skip
          and (args.arm is None or r.get("arm") == args.arm)]
    last = rs[-1] if rs else None
    ok = bool(last and last["outcome"] == "pass" and last.get("branch"))
    print(json.dumps({"task": args.task, "shift": args.shift, "resumable": ok,
                      "run": last["run"] if last else None,
                      "outcome": last["outcome"] if last else None,
                      "branch": last.get("branch") if last else None,
                      "tier": last["tier"] if last else None,
                      "arm": last.get("arm") if last else None}))
    return 0 if ok else 1


def cmd_envelope(args):
    """The envelope one run of a task gets on a tier: the same calls and
    seconds the default executor caps a run at, so a session arm can run under the
    same wall cap instead of none at all."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    try:
        task = tasks.load_task(args.task_dir) if args.task_dir else \
            tasks.load_tasks(args.bench or host.bench_dir, only=[args.task])[0]
    except tasks.TaskError as e:
        print(f"tasks: {e}")
        return 2
    fr, why = _frozen(args, budgets, [task])
    if why:
        print(json.dumps({"error": why}))
        return 2
    if fr:
        env, think = fr.envelope(task.cls), (fr.think(task.cls) or args.think)
    else:
        think, legacy = budget.level(budgets, catalog, task.cls, args.tier, args.think)
        env = budget.envelope(budgets, ledger, task.cls, args.tier, think=think, legacy=legacy)
    out = dict(env.as_dict(), task=task.id, cls=task.cls, tier=args.tier, think=think,
               stage_timeout=task.stage_timeout,
               **({"frozen": fr.as_dict()} if fr else {}))
    # what a session arm runs under: the run's seconds plus the time the
    # pipeline allows for judging the artefact
    out["wall_seconds"] = env.seconds + task.stage_timeout
    print(json.dumps(out))
    return 0


def cmd_spec_review(args):
    """The validator tier confirms each task's hidden acceptance follows from
    its spec; verdicts land in <bench>/reviews/<task>.md."""
    from . import review
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    try:
        task_list = _task_list(args, host)
    except tasks.TaskError as e:
        print(f"tasks: {e}")
        return 2
    tier = args.tier or (catalog.with_role("validator") or [None])[0]
    if not tier:
        print("no validator tier in models.toml and no --tier given")
        return 2
    bench = args.bench or host.bench_dir
    os.makedirs(os.path.join(bench, "reviews"), exist_ok=True)
    bad = 0
    for t in task_list:
        verdict, checks, reply, usage = review.review_task(t, catalog, budgets, ledger, tier, shift=args.shift or "adhoc")
        with open(os.path.join(bench, "reviews", f"{t.id}.md"), "w") as f:
            f.write(review.render(t, tier, verdict, checks, reply, usage, ledger.clock()))
        bad += verdict != "OK"
        print(f"{t.id:26} {verdict:10} {sum(1 for _, ok, _ in checks if ok)}/{len(checks)} implied  "
              f"tokens {usage.get('tokens_in', 0)}/{usage.get('tokens_out', 0)}")
        for name, ok, why in checks:
            if not ok:
                print(f"    NOT IMPLIED {name}: {why}")
    return 1 if bad else 0


def cmd_digest(args):
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    last = ledger.last("shift.start")
    shift_id = args.shift or (last["shift"] if last else "none")
    text, one = digest.render(ledger, catalog, budgets, shift_id, ledger.clock())
    print(text)
    print(one)
    return 0


def cmd_void(args):
    """Mark a run's outcome as the deployment's fault, not the model's: the
    record stays, every rate skips it, the digest lists it."""
    catalog, budgets, host, ledger, gitea, px = _ctx(args)
    hit = [r for r in ledger.runs(include_void=True) if r["run"] == args.run]
    if not hit:
        print(f"no run.end for {args.run}")
        return 2
    ledger.emit("run.void", task=hit[0]["task"], run=args.run, reason=args.reason)
    print(f"voided {args.run} ({hit[0]['outcome']} on {hit[0]['tier']}, arm {hit[0].get('arm')}): {args.reason}")
    return 0


def cmd_abort(args):
    catalog, budgets, host = _load(args)
    os.makedirs(host.abort_dir, exist_ok=True)
    with open(os.path.join(host.abort_dir, args.run), "w") as f:
        f.write("abort\n")
    print(f"abort marker written for {args.run} (the watcher stands down at its next poll)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dark")
    ap.add_argument("--conf", default=HERE, help="directory holding models.toml, budgets.toml, host.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config", help="load and validate the three config files")
    p = sub.add_parser("preflight", help="every check a shift needs, one line each")
    p.add_argument("--no-vm", action="store_true", help="skip the compute-plane checks (no wake)")
    sub.add_parser("admission", help="the admission table derived from the ledger")
    sub.add_parser("status", help="windows, watts and envelopes today")
    p = sub.add_parser("shift", help="preflight, run every task, digest")
    p.add_argument("--bench", help="bench checkout (default: host.bench_dir)")
    p.add_argument("--tasks", help="comma-separated task ids (default: all)")
    p.add_argument("--task-dir", help="one task directory instead of the bench")
    p.add_argument("--tier", help="force this tier (bench mode): admission and pick are skipped")
    p.add_argument("--max-runs", type=int)
    # default None, not 0/"factory": --resume fills every option the command
    # line leaves off from the launch it continues, and cannot tell a typed
    # default from an untyped one
    p.add_argument("--slot", type=int,
                   help="VM slot (executor VM = vmid_base + slot, staging = + 50 + slot); a second concurrent shift needs its own (default 0)")
    p.add_argument("--arm", help="arm name for every run of this shift (default factory)")
    p.add_argument("--think", choices=["none", "low", "medium", "high"],
                   help="dark's thinking level for every run (budgets [think]); default: each tier's own")
    p.add_argument("--resume", metavar="SHIFT",
                   help="continue the shift with this id: its passed tasks are not run again, a "
                        "chained task starts from the branch it already delivered, and the launch's "
                        "tasks, tier, think level, arm and slot are repeated (an option that says "
                        "something else is refused)")
    p.add_argument("--no-push", action="store_true", help="commit the digest locally, do not push")
    # default None on both, like --slot: --resume fills every option the command
    # line leaves off, and store_true's False cannot be told from an untyped one
    p.add_argument("--frozen", metavar="FILE",
                   help="a pinned envelope file (dark/frozen.py): its calls, seconds, reasoning "
                        "chars and think level are used instead of the ledger's, and its name and "
                        "sha256 land on every run.start")
    p.add_argument("--executor", choices=["agent", "session"], default=None,
                   help="what runs inside the VM: 'agent' (the default FILE:/DELETE: protocol loop, the "
                        "default) or 'session' (pi with its own tools, dark/session.py)")
    p.add_argument("--no-adapt", action="store_true", default=None,
                   help="admission, cooling and escalation off for this shift: the tier named by "
                        "--tier does the whole task, once")
    p.add_argument("--max-stall", type=int, default=None,
                   help="cut a run once a reply repeats one already seen this many times (0 or "
                        "unset = off); a frozen file's per-class max_stall wins over this")
    p = sub.add_parser("materialize", help="create/reset the work repos of tasks (main = starting tree)")
    p.add_argument("--bench")
    p.add_argument("--tasks", help="comma-separated task ids (default: all)")
    p.add_argument("--task-dir")
    p.add_argument("--after-branch", help="a chained task starts from this branch of the task it follows "
                                          "(default for a chained task: that task's oracle tree)")
    p = sub.add_parser("archive-work", help="move t-*/tl-*/session-* repos to an archive org and archive them (dry run without --apply)")
    p.add_argument("--to", default="dark-archive")
    p.add_argument("--src", help="org to move from (default: host work_org)")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("power", help="live energy sensors: RAPL package watts on the Proxmox host, GPU draw in the model VM")
    p.add_argument("--seconds", type=int, default=3, help="window for the package watts reading")
    p = sub.add_parser("stage", help="stage a branch a session arm pushed: hidden acceptance in a fresh VM")
    p.add_argument("--bench")
    p.add_argument("--task", help="task id in the bench")
    p.add_argument("--task-dir")
    p.add_argument("--branch", required=True)
    p.add_argument("--base", choices=["delivered", "oracle"],
                   help="what a chained step started from (required for a task with `after`)")
    p.add_argument("--after-branch", help="the predecessor's branch a delivered base was fetched from")
    p.add_argument("--arm", required=True, help="e.g. session-a")
    p.add_argument("--tier", required=True, help="the model id the session used (must be in models.toml)")
    p.add_argument("--shift", help="ledger shift id to file the run under")
    p.add_argument("--slot", type=int, default=1, help="VM slot (staging VM = vmid_base + 50 + slot); a shift uses slot 0")
    p.add_argument("--frozen", metavar="FILE",
                   help="the pinned envelope file the session was held to: recorded on this run's "
                        "run.start so both arms of a comparison set name the same limits")
    p.add_argument("--capped", action="store_true",
                   help="the session was stopped at the default executor's wall cap, it did not finish on its own")
    for k in ("calls", "tokens_in", "tokens_out", "reasoning_chars", "seconds"):
        p.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int, default=None,
                       help="as exposed by the session's brain; omit = NOT MEASURED")
    p = sub.add_parser("review", help="launch a session-arm review-mode run: a brief and files in, report.md out")
    p.add_argument("--brief", required=True, help="markdown file: the review's instructions")
    p.add_argument("--files", default="", help="comma-separated file paths to stage read-only for the session")
    p.add_argument("--tier", required=True, help="the model id to use (must be in models.toml)")
    p.add_argument("--arm", required=True, help="e.g. review-glm")
    p.add_argument("--shift", help="ledger shift id (default: adhoc); also names the dark-records repo")
    p.add_argument("--think", choices=["none", "low", "medium", "high"])
    p.add_argument("--frozen", metavar="FILE", help="a pinned envelope file for the review class")
    p = sub.add_parser("done", help="the verdict a shift already holds for a task (session-side resume); exit 0 on a valid pass")
    p.add_argument("--shift", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--arm", help="only a run of this arm counts")
    p = sub.add_parser("envelope", help="the calls, seconds and wall cap one run of a task gets on a tier")
    p.add_argument("--bench")
    p.add_argument("--task")
    p.add_argument("--task-dir")
    p.add_argument("--tier", required=True)
    p.add_argument("--think", choices=["none", "low", "medium", "high"])
    p.add_argument("--frozen", metavar="FILE", help="report the pinned envelope instead of the ledger's")
    p = sub.add_parser("spec-review", help="validator tier: does each task's hidden acceptance follow from its spec?")
    p.add_argument("--bench")
    p.add_argument("--tasks")
    p.add_argument("--task-dir")
    p.add_argument("--tier", help="default: the catalog's validator tier")
    p.add_argument("--shift")
    p = sub.add_parser("bench", help="one manifest, five phases: bootstrap, smoke, run, report, archive")
    p.add_argument("manifest", help="the experiment's TOML manifest")
    p.add_argument("--phases", default="bootstrap,smoke,run,report,archive",
                   help="comma-separated phases to run (default: all five)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; no network, works on any host")
    p.add_argument("--bench", help="bench checkout (default: host.bench_dir)")
    p.add_argument("--pause", action="store_true", help="write bench.pause for this manifest's name and exit")
    p.add_argument("--resume", action="store_true",
                   help="write bench.resume, then continue the selected phases, skipping every "
                        "(round, arm) already verified for this manifest's sha256")
    p = sub.add_parser("digest", help="render the digest for the last (or given) shift")
    p.add_argument("--shift")
    p = sub.add_parser("void", help="exclude a run from every rate (a fault in the runner or the deployment, not the model); the record stays")
    p.add_argument("run")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("abort", help="ask the watcher to abort a run (or 'all')")
    p.add_argument("run")
    args = ap.parse_args(argv)
    try:
        return {"check-config": cmd_check_config, "preflight": cmd_preflight, "admission": cmd_admission,
                "status": cmd_status, "shift": cmd_shift, "stage": cmd_stage, "digest": cmd_digest, "power": cmd_power, "archive-work": cmd_archive_work,
                "materialize": cmd_materialize, "spec-review": cmd_spec_review, "review": cmd_review,
                "done": cmd_done, "envelope": cmd_envelope, "bench": cmd_bench,
                "void": cmd_void, "abort": cmd_abort}[args.cmd](args)
    except config.ConfigError as e:
        print(f"config: {e}")
        return 2
    except (G.GiteaError, L.LedgerError, vm.VMError) as e:
        print(f"{args.cmd}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

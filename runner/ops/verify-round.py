#!/usr/bin/env python3
"""verify-round — check one shift's records against the other two places the
same runs are written down, before the round is counted.

A round is one pass over the task set by one arm. Its runs are recorded in
three places that are written by different code: the append-only ledger
(`run.end`), the shift's digest (a markdown file the runner commits to the
bench), and the work repositories on Gitea (one `run/...` branch per
delivered run). If they disagree, one of them is wrong, and the round is
not evidence until the disagreement is explained.

  python3 ops/verify-round.py --shift <id> [--digest <file>] [--json OUT]

Prints one line per check: MATCH, MISMATCH or NOT DERIVABLE, and exits 1
if any check is a MISMATCH. Runs on the runner host, which is where the
ledger, the bench checkout and the Gitea token are.
"""

import argparse
import collections
import glob
import json
import os
import sys
import tomllib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from dark import config  # noqa: E402
from dark import gitea as G  # noqa: E402
from dark import ledger as L  # noqa: E402
from dark import spec  # noqa: E402

OUTCOMES = set(spec.OUTCOMES)

BAD = 0


def chk(name, verdict, note=""):
    global BAD
    BAD += verdict == "MISMATCH"
    print(f"{verdict:14} {name}" + (f"  ({note})" if note else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shift", required=True)
    ap.add_argument("--conf", default=HERE)
    ap.add_argument("--digest", help="the digest file (default: the newest naming this shift)")
    ap.add_argument("--tasks", help="comma-separated task list the round was meant to cover "
                                    "(a session round writes no shift.start to read it from)")
    ap.add_argument("--json")
    args = ap.parse_args()

    catalog, budgets = config.load(args.conf)
    host = config.load_host(os.path.join(args.conf, "host.toml"))
    host.admin_token = G.read_token(host.admin_token_file)
    led = L.Ledger(host.ledger_path)
    gitea = G.Gitea(host.gitea_url, host.admin_token, lan_url=host.gitea_lan_url)

    voided = led.voided()
    ends = [r for r in led.events("run.end") if r.get("shift") == args.shift]
    live = [r for r in ends if r["run"] not in voided]
    starts = [r for r in led.events("run.start") if r.get("shift") == args.shift]
    print(f"== shift {args.shift}: {len(starts)} run.start, {len(ends)} run.end, "
          f"{len(ends) - len(live)} voided")

    # 1. one run.end per run.start (an interrupted run leaves a start alone)
    if len(starts) == len(ends):
        chk("run.start has a run.end", "MATCH", f"{len(starts)} of each")
    else:
        chk("run.start has a run.end", "MISMATCH",
            f"{len(starts)} run.start against {len(ends)} run.end: an interrupted run leaves a start alone")

    # 1b. the round covers its task list
    # a step can fail before it reaches the ledger at all (for example Gitea
    # answering `500 database is locked` on materialize), and then it leaves
    # no run.start and no run.end. A round two tasks short then looks whole
    # to every check that compares one ledger row against another. The list
    # comes from the shift's own launch record; a session round writes no
    # shift.start, so there --tasks has to say it.
    tasks = None
    for s_ev in led.events("shift.start"):
        if s_ev.get("shift") == args.shift:
            tasks = (s_ev.get("launch") or {}).get("tasks")
    if not tasks and args.tasks:
        tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    if not tasks:
        chk("the round covers its task list", "NOT DERIVABLE",
            "no shift.start names the tasks and --tasks was not given")
    else:
        ran = collections.Counter(r["task"] for r in live)
        absent = [t for t in tasks if not ran[t]]
        twice = sorted(t for t, n in ran.items() if n > 1)
        unasked = sorted(t for t in ran if t not in set(tasks))
        if not absent and not twice and not unasked:
            chk("the round covers its task list", "MATCH",
                f"{len(tasks)} task(s), one row each")
        else:
            chk("the round covers its task list", "MISMATCH",
                f"no row: {absent}; more than one row: {twice}; not on the list: {unasked}")

    # 2. the digest the shift committed
    # the digest names one table row per task, not per run id: compare the
    # (task, outcome) pairs, which is what the digest actually claims
    dig = args.digest
    if not dig:
        cands = [p for p in glob.glob(os.path.join(host.bench_dir, "digest", "**", "*"), recursive=True)
                 if os.path.isfile(p) and args.shift in open(p, errors="ignore").read(4000)]
        dig = cands[-1] if cands else None
    if not dig:
        chk("digest names the same runs", "NOT DERIVABLE", "no digest file names this shift")
    else:
        text = open(dig, errors="ignore").read()
        body = text.split("## Runs this shift", 1)[-1].split("\n## ", 1)[0]
        named = collections.Counter()
        for line in body.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) > 4 and cells[3] in OUTCOMES:
                named[(cells[0], cells[3])] += 1
        rows = collections.Counter((r["task"], r["outcome"]) for r in live)
        missing = sorted((rows - named).elements())
        extra = sorted((named - rows).elements())
        if not missing and not extra:
            chk("digest names the same runs", "MATCH",
                f"{sum(rows.values())} row(s), {os.path.basename(dig)}")
        else:
            chk("digest names the same runs", "MISMATCH",
                f"only in the ledger: {missing[:5]}; only in the digest: {extra[:5]}")

    # 3. one branch per delivered row, one row per branch
    # delivered = the run reached `staging`, which is where the branch is
    # pushed; a run that ended before it (out of calls, say) carries the
    # branch name it would have used, and that name must not be on the server
    staged = {e["run"] for e in led.events("run.transition") if e.get("to") == "staging"}
    want = collections.defaultdict(set)     # repo -> branches that must exist
    planned = collections.defaultdict(set)  # repo -> branches that must not
    for r in live:
        if r.get("branch") and r.get("repo"):
            (want if r["run"] in staged else planned)[r["repo"]].add(r["branch"])
    seen = 0
    stray = []
    for full in sorted(set(want) | set(planned)):
        try:
            have = {b["name"] for b in gitea.api(f"/repos/{full}/branches")}
        except G.GiteaError as e:
            chk(f"branches of {full}", "NOT DERIVABLE", str(e))
            continue
        for b in sorted(want[full]):
            if b in have:
                seen += 1
            else:
                stray.append(f"{full}:{b} staged, not on the server")
        for b in sorted(planned[full]):
            if b in have:
                stray.append(f"{full}:{b} on the server, but the run never reached staging")
        mine = {x["branch"] for x in live if x.get("repo") == full and x.get("branch")}
        for b in sorted(have):
            if b.startswith("run/") and b not in mine and b.rsplit("run/", 1)[-1].endswith(args.shift[:8]):
                stray.append(f"{full}:{b} on the server, not in this shift's records")
    if stray:
        chk("one branch per delivered row", "MISMATCH", "; ".join(stray[:5]))
    elif seen:
        chk("one branch per delivered row", "MATCH",
            f"{seen} branch(es) over {len(want)} repo(s), "
            f"{sum(len(v) for v in planned.values())} unstaged row(s) with no branch")
    else:
        chk("one branch per delivered row", "NOT DERIVABLE", "no row carries a branch and a repo")

    # 4. every row carries what its arm's mode says it must carry
    # a factory arm's run is metered end to end (the model runs on hardware
    # this host reads); a session arm's model runs outside the meter and only
    # the judging happens here, so energy is NOT MEASURED for it by mode, not
    # by accident, and what it must carry instead is the VM assert and a
    # tool-call count from the trail
    modes = {}
    arms_path = os.path.join(host.bench_dir, "arms.toml")
    try:
        with open(arms_path, "rb") as fh:
            for name, body in (tomllib.load(fh).get("arm") or {}).items():
                modes[name] = body.get("mode", "factory")
    except (OSError, tomllib.TOMLDecodeError) as e:
        chk("arm modes readable", "NOT DERIVABLE", f"{arms_path}: {e}")
    # a factory-mode row is metered only when its model ran on hardware this
    # host reads, which the runner marks with watts_class (dark/run.py sets it
    # for local tiers only). A cloud tier's model runs outside the meter, so
    # its rows carry no Wh to publish and the GPU sensor is not required to be
    # alive for them: the model VM sleeps when no local model is asked, so a
    # cloud row has nothing to measure.
    factory = [r for r in live if modes.get(r.get("arm"), "factory") != "session"]
    metered = [r for r in factory if r.get("watts_class")]
    cloudrows = [r for r in factory if not r.get("watts_class")]
    sessions = [r for r in live if modes.get(r.get("arm")) == "session"]
    # a run cut off on its seconds budget is stopped part way through a call,
    # and the reasoning characters are counted when a call completes, so that
    # row has none. The energy is still measured: the meter reads the host
    # over the run's window whether the run finished or not.
    def cut_on_time(r):
        return r.get("outcome") == "fail:budget" and r.get("fail_kind") == "seconds"

    thin = [r["run"] for r in metered
            if r.get("wh") is None
            or (r.get("reasoning_chars") is None and not cut_on_time(r))
            or (r.get("asserts") or {}).get("sensors_alive") is not True
            or (r.get("asserts") or {}).get("vms_destroyed") is not True]
    timed_out = [r["run"] for r in metered if cut_on_time(r) and r.get("reasoning_chars") is None]
    if not metered:
        chk("metered rows complete", "NOT DERIVABLE",
            f"no metered row: {len(sessions)} session row(s), {len(cloudrows)} cloud row(s), energy NOT MEASURED by mode")
    elif thin:
        chk("metered rows complete", "MISMATCH", f"{len(thin)} of {len(metered)}: {thin[:4]}")
    else:
        note = f"{len(metered)} row(s) with Wh, reasoning chars and both asserts"
        if timed_out:
            note += (f"; {len(timed_out)} cut off on the seconds budget, "
                     f"reasoning characters NOT MEASURED for {timed_out[:2]}")
        chk("metered rows complete", "MATCH", note)
    if cloudrows:
        thin_c = [r["run"] for r in cloudrows
                  if (r.get("reasoning_chars") is None and not cut_on_time(r))
                  or (r.get("asserts") or {}).get("vms_destroyed") is not True]
        if thin_c:
            chk("cloud rows complete", "MISMATCH", f"{len(thin_c)} of {len(cloudrows)}: {thin_c[:4]}")
        else:
            chk("cloud rows complete", "MATCH",
                f"{len(cloudrows)} row(s) with reasoning chars and VMs destroyed, energy NOT MEASURED by tier")
    if sessions:
        bad = [r["run"] for r in sessions
               if (r.get("asserts") or {}).get("vms_destroyed") is not True]
        if bad:
            chk("session rows complete", "MISMATCH",
                f"{len(bad)} of {len(sessions)} without a destroyed staging VM: {bad[:4]}")
        else:
            counted = sum(1 for r in sessions if r.get("calls") is not None)
            chk("session rows complete", "MATCH",
                f"{len(sessions)} row(s), staging VM destroyed, {counted} with a tool-call count, "
                f"energy NOT MEASURED by mode")

    # 5. a session round's tool calls: a NOT MEASURED count is the trail
    # pairing failing, which is a defect to fix and not a number to publish
    sess = [r for r in live if modes.get(r.get("arm")) == "session"
            or str(r.get("arm", "")).startswith("session-")]
    if sess:
        nm = [r["run"] for r in sess if r.get("calls") is None]
        chk("session steps have a paired trail", "MISMATCH" if nm else "MATCH",
            f"{len(nm)} of {len(sess)} NOT MEASURED: {nm[:4]}" if nm else f"{len(sess)} step(s)")

    # 6. every row names the acceptance version that judged it
    notests = [r["run"] for r in live if not r.get("tests")]
    if live:
        chk("rows name their test version", "MISMATCH" if notests else "MATCH",
            f"{len(notests)} of {len(live)} without one" if notests
            else sorted({r["tests"] for r in live})[0])

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"shift": args.shift, "run_end": len(ends), "live": len(live),
                       "run_start": len(starts), "mismatches": BAD}, f, indent=1)
    print(f"{BAD} mismatch(es)")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())

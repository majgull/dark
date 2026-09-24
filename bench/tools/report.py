#!/usr/bin/env python3
"""report — every table the reports carry, rendered from the ledger.

  python3 tools/report.py --ledger ledger.jsonl [--until 'YYYY-MM-DD HH:MM'] chain --shift ID [--restage ID]
  python3 tools/report.py --ledger ledger.jsonl chain --arm ARM --restage ID
  python3 tools/report.py --ledger ledger.jsonl bench [--day YYYY-MM-DD] [--cls CLS]
  python3 tools/report.py --ledger ledger.jsonl runs --shift ID

Rules the tables follow, so a reader can re-derive them:
- a run counts when it has a run.end, no run.void names it, and its outcome
  is not fail:structural (the harness failed, not the model);
- a re-judged row (a `restage` shift, which judges a delivered branch a
  second time, or any run.end carrying `restaged_from`)
  supplies the verdict; its cost, level and reasoning come from the run that
  delivered the branch (joined on `branch`), never from the re-judgement;
- a number the ledger does not carry prints as n/a (NOT MEASURED). Wh is the
  measured `wh` field only; rows without one are unmetered, never estimated;
- session-arm tool calls are not in the ledger (the agent session's own audit
  trail holds them; count `pre` records, not lines).
Every table is preceded by a comment naming the command and the ledger's
sha256, so the markdown that embeds it says where it came from."""

import argparse
import fnmatch
import hashlib
import json
import sys
import time
from collections import defaultdict


def load(path, until=None, since=None):
    events = [json.loads(l) for l in open(path) if l.strip()]
    if until:
        cut = time.mktime(time.strptime(until, "%Y-%m-%d %H:%M"))
        events = [e for e in events if e["ts"] < cut]
    if since:
        cut = time.mktime(time.strptime(since, "%Y-%m-%d %H:%M"))
        # run.void records are kept whatever their time: a void written after
        # the window still voids a run inside it
        events = [e for e in events if e["ts"] >= cut or e["kind"] == "run.void"]
    return events


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]


def valid_runs(events):
    void = {e["run"] for e in events if e["kind"] == "run.void"}
    return [e for e in events if e["kind"] == "run.end" and e["run"] not in void]


def joined(rows, all_ends):
    """Re-judged rows take cost/level/reasoning/wh from the delivering run."""
    by_branch = defaultdict(list)
    for e in all_ends:
        by_branch[e.get("branch")].append(e)
    out = []
    for r in rows:
        src = None
        if r.get("restaged_from"):
            src = next((e for e in all_ends if e["run"] == r["restaged_from"]), None)
        elif r.get("shift", "").endswith("restage") or "restage" in (r.get("shift") or ""):
            cands = [e for e in by_branch.get(r.get("branch"), []) if e["run"] != r["run"] and "restage" not in (e.get("shift") or "")]
            src = cands[0] if cands else None
        m = dict(r)
        if src:
            for k in ("calls", "tokens_in", "tokens_out", "reasoning_chars", "seconds", "think", "wh", "wh_cpu", "wh_gpu", "tier"):
                m[k] = src.get(k)
            m["_source_run"] = src["run"]
        out.append(m)
    return out


def na(v):
    return "n/a" if v is None else v


def is_session(r):
    return (r.get("arm") or "").startswith("session")


def calls_of(r):
    """A session arm's `calls` in the ledger is what its arm script counted at
    stage time (trail records, doubled and once tail-capped); the
    trail is the source. Factory calls are the executor's own count."""
    return "trail" if is_session(r) else na(r.get("calls"))


def header(args, ledger):
    return f"<!-- rendered by tools/report.py {' '.join(args)} ; ledger sha256 {sha(ledger)} ; {time.strftime('%Y-%m-%d %H:%M')} -->"


def chain(events, shift=None, arm=None, restage=None, tier=None):
    ends = [e for e in events if e["kind"] == "run.end"]
    rows = valid_runs(events)
    if restage:
        rows = [r for r in rows if r.get("shift") == restage and r["outcome"] != "fail:structural"]
        if arm:
            rows = [r for r in rows if r.get("arm") == arm]
    else:
        rows = [r for r in rows if (shift and r.get("shift") == shift) or (arm and not shift and r.get("arm") == arm)]
    rows = joined(rows, ends)
    if tier:
        rows = [r for r in rows if r.get("tier") == tier]
    # per step: the last valid attempt is the step's result; earlier attempts are listed
    steps = defaultdict(list)
    for r in rows:
        steps[r["task"]].append(r)
    lines = ["| step | attempt | tier | think | outcome | checks | calls | tokens in/out | reasoning | exec s | Wh | base |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    passed, depth, broken, tot = 0, 0, False, defaultdict(float)
    metered = 0
    for task in sorted(steps):
        att = steps[task]
        for i, r in enumerate(att, 1):
            lines.append(f"| {task} | {i} of {len(att)} | {r.get('tier')} | {r.get('think') or 'default'} | {r['outcome']} | "
                         f"{r.get('checks_ok', 0)}/{r.get('checks_total', 0)} | {calls_of(r)} | "
                         f"{na(r.get('tokens_in'))}/{na(r.get('tokens_out'))} | {na(r.get('reasoning_chars'))} | "
                         f"{na(r.get('seconds'))} | {na(r.get('wh'))} | {r.get('base') or ''} |")
            for k in ("calls", "tokens_in", "tokens_out", "seconds"):
                if r.get(k) is not None and not (k == "calls" and is_session(r)):
                    tot[k] += r[k]
            if r.get("wh") is not None:
                tot["wh"] += r["wh"]
                metered += 1
        last = att[-1]
        if last["outcome"] == "pass":
            passed += 1
            if not broken:
                depth += 1
        else:
            broken = True
    n = len(steps)
    wh = f"{tot['wh']:.3f} Wh over {metered} metered of {len(rows)} runs" if metered else "NOT MEASURED (no metered run)"
    lines += ["", f"steps passed {passed} of {n}; unbroken depth {depth}; runs {len(rows)}; model calls {int(tot['calls'])}; "
                  f"tokens {int(tot['tokens_in'])} in / {int(tot['tokens_out'])} out; executor {int(tot['seconds'])} s; energy {wh}."]
    return "\n".join(lines)


def bench(events, day=None, cls=None):
    rows = [r for r in valid_runs(events) if r["outcome"] != "fail:structural"]
    if day:
        rows = [r for r in rows if r["iso"].startswith(day)]
    if cls:
        rows = [r for r in rows if r["cls"] == cls]
    rows = [r for r in rows if not r.get("restaged_from") and "restage" not in (r.get("shift") or "")]
    g = defaultdict(list)
    for r in rows:
        g[(r["cls"], r["tier"], r.get("arm") or "factory", r.get("think") or "default")].append(r)
    lines = ["| class | tier | arm | think | pass | rate | calls/pass | tokens out/pass | exec s/pass | Wh/pass | metered | tests |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(g):
        rs = g[key]
        p = [r for r in rs if r["outcome"] == "pass"]
        calls = [r["calls"] for r in rs if r.get("calls") is not None and not is_session(r)]
        toks = [r["tokens_out"] for r in rs if r.get("tokens_out") is not None]
        secs = [r["seconds"] for r in rs if r.get("seconds") is not None]
        whs = [r["wh"] for r in rs if r.get("wh") is not None]
        per = lambda xs: "n/a" if not xs or not p else f"{sum(xs) / len(p):.1f}"  # noqa: E731
        vers = sorted({r.get("tests") for r in rs if r.get("tests")})
        tests = vers[0] if len(vers) == 1 else (f"mixed ({len(vers)})" if vers else "NOT MEASURED")
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {len(p)}/{len(rs)} | {len(p) / len(rs):.2f} | "
                     f"{'trail' if is_session(rs[0]) else per(calls)} | {per(toks)} | {per(secs)} | "
                     f"{'NOT MEASURED' if len(whs) < len(rs) else per(whs)} | {len(whs)}/{len(rs)} | {tests} |")
    return "\n".join(lines)


def runs(events, shift):
    ends = [e for e in events if e["kind"] == "run.end"]
    rows = joined([r for r in valid_runs(events) if r.get("shift") == shift], ends)
    lines = ["| task | tier | arm | think | outcome | kind | checks | calls | reasoning | exec s | wall s | Wh cpu/gpu | source |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r.get('tier')} | {r.get('arm')} | {r.get('think') or 'default'} | {r['outcome']} | "
                     f"{r.get('fail_kind') or ''} | {r.get('checks_ok', 0)}/{r.get('checks_total', 0)} | {calls_of(r)} | "
                     f"{na(r.get('reasoning_chars'))} | {na(r.get('seconds'))} | {r.get('wall_seconds', '')} | "
                     f"{na(r.get('wh_cpu'))}/{na(r.get('wh_gpu'))} | {r.get('_source_run', '')} |")
    return "\n".join(lines)


def arm_match(name, pattern):
    """Does this run's arm belong to the side named by `pattern`?

    A side is usually one arm name. It may also be a glob, because a
    comparison that names its arm per round (arm-r1, arm-r2, ...) writes one
    arm name per round and the side is the set of them. The glob is matched
    against the whole name, so "arm-*" is the whole set and "arm" alone
    still means exactly that arm."""
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(name or "", pattern)
    return name == pattern


def paired(events, arm_a, arm_b, tier=None, shifts=None):
    """The crossed comparison's own table: one row per task the two arms both
    ran, so the comparison is paired (the same task on both sides) rather
    than two rates over two task sets. A task only one arm ran is listed
    under "unpaired" and counted nowhere else.

    `shifts` names the rounds to include. Without it every shift an arm ever
    ran goes in, and two arms with different numbers of rounds are then
    compared on "passed at least once", which the arm with more attempts
    wins by having had more attempts. Naming the rounds is how the two
    sides are held to the same denominator."""
    rows = [r for r in valid_runs(events) if r["outcome"] != "fail:structural"]
    if shifts:
        want = set(shifts)
        rows = [r for r in rows if r.get("shift") in want]
    if tier:
        rows = [r for r in rows if r["tier"] == tier]
    by = {"a": defaultdict(list), "b": defaultdict(list)}
    for r in rows:
        side = ("a" if arm_match(r.get("arm"), arm_a)
                else ("b" if arm_match(r.get("arm"), arm_b) else None))
        if side:
            by[side][r["task"]].append(r)
    tasks = sorted(set(by["a"]) & set(by["b"]))
    only = sorted(set(by["a"]) ^ set(by["b"]))
    rounds_a = len({r.get("shift") for r in rows if arm_match(r.get("arm"), arm_a)})
    rounds_b = len({r.get("shift") for r in rows if arm_match(r.get("arm"), arm_b)})
    lines = [f"| task | {arm_a} ({rounds_a} round(s)) | {arm_b} ({rounds_b} round(s)) | agree |",
             "|---|---|---|---|"]
    both = a_only = b_only = neither = 0
    for t in tasks:
        pa = sum(1 for r in by["a"][t] if r["outcome"] == "pass")
        pb = sum(1 for r in by["b"][t] if r["outcome"] == "pass")
        na_, nb = len(by["a"][t]), len(by["b"][t])
        agree = "yes" if (pa > 0) == (pb > 0) else "no"
        if pa and pb:
            both += 1
        elif pa:
            a_only += 1
        elif pb:
            b_only += 1
        else:
            neither += 1
        lines.append(f"| {t} | {pa}/{na_} | {pb}/{nb} | {agree} |")
    lines.append(f"| **paired totals** | {both + a_only} of {len(tasks)} | {both + b_only} of {len(tasks)} | "
                 f"both {both}, {arm_a} only {a_only}, {arm_b} only {b_only}, neither {neither} |")
    if only:
        lines.append("")
        lines.append(f"Unpaired, counted nowhere above: {', '.join(only)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--until", help="'YYYY-MM-DD HH:MM' local: ignore events at or after")
    ap.add_argument("--since", help="'YYYY-MM-DD HH:MM' local: ignore events before (run.void kept)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("chain")
    p.add_argument("--shift")
    p.add_argument("--arm")
    p.add_argument("--restage", help="take verdicts from this re-judging shift, costs from the delivering runs")
    p.add_argument("--tier", help="only runs on this tier (after the join, so re-judged rows carry the delivering tier)")
    p = sub.add_parser("bench")
    p.add_argument("--day")
    p.add_argument("--cls")
    p = sub.add_parser("runs")
    p.add_argument("--shift", required=True)
    p = sub.add_parser("paired", help="one row per task both arms ran: the crossed comparison, paired")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--tier")
    p.add_argument("--shifts", help="comma-separated shift ids to include; without it "
                                    "every round either arm ever ran goes in and the "
                                    "two sides can rest on different numbers of rounds")
    args = ap.parse_args()
    events = load(args.ledger, args.until, args.since)
    print(header(sys.argv[1:], args.ledger))
    if args.cmd == "chain":
        if not (args.shift or args.arm):
            ap.error("chain needs --shift or --arm")
        print(chain(events, args.shift, args.arm, args.restage, args.tier))
    elif args.cmd == "bench":
        print(bench(events, args.day, args.cls))
    elif args.cmd == "paired":
        print(paired(events, args.a, args.b, args.tier,
                     [x.strip() for x in args.shifts.split(",") if x.strip()] if args.shifts else None))
    else:
        print(runs(events, args.shift))


if __name__ == "__main__":
    main()

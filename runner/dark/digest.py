"""dark/digest.py — the one report per shift, generated from the ledger
(design §6; operator: short push plus link). Runs by class and tier, pass
rates, window and watts consumed and left, the admission table, what was
parked or blocked. Committed into the bench checkout; one ntfy line with
the link.
"""

import os
import shutil
import subprocess
import time

from . import admission, budget
from . import ledger as L


def _pct(a, b):
    return f"{100.0 * a / b:.0f}%" if b else "n/a"


def render(ledger, catalog, budgets, shift_id, now):
    day = L.day_of(now)
    events = ledger.events()
    shift_ev = [e for e in events if e.get("shift") == shift_id]
    void = ledger.voided()
    runs = [e for e in shift_ev if e["kind"] == "run.end" and e["run"] not in void]
    passes = [r for r in runs if r["outcome"] == "pass"]
    lines = [f"# dark shift {shift_id}", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} from the ledger "
             f"({len(events)} events). {len(runs)} run(s), {len(passes)} pass.", ""]
    refused = [e for e in shift_ev if e["kind"] == "preflight.refused"]
    if refused:
        lines += [f"**Refused:** {refused[-1]['detail']}", ""]

    # think: the level the run ran at (default = the provider's own); reasoning:
    # measured characters, the only statement about thinking that is a fact
    # (bench probes/: glm's "low" is 22 chars); Wh: measured, n/a = not metered
    lines += ["## Runs this shift", "", "| task | class | tier | outcome | kind | calls (distinct) | tokens in/out | reasoning | seconds | wall | checks | think | Wh |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    nm = lambda v: "n/a" if v is None else v  # noqa: E731 — NOT MEASURED, never an estimate
    for r in runs:
        lines.append(f"| {r['task']} | {r['cls']} | {r['tier']}{' (' + r['arm'] + ')' if r.get('arm') not in (None, 'factory') else ''} | "
                     f"{r['outcome']} | {r.get('fail_kind') or ''} | "
                     f"{nm(r['calls'])} ({nm(r.get('distinct_calls'))}) | {nm(r['tokens_in'])}/{nm(r['tokens_out'])} | {nm(r['reasoning_chars'])} | "
                     f"{nm(r['seconds'])} | {r.get('wall_seconds', '')} | {r.get('checks_ok', 0)}/{r.get('checks_total', 0)} | "
                     f"{r.get('think') or 'default'} | {nm(r.get('wh'))} |")
    if not runs:
        lines.append("| - | | | | | | | | | | | | |")

    lines += ["", "## Pass rate by class and tier (all shifts, last "
              f"{budgets.last_runs} runs each)", "",
              "| class | tier | admitted | basis | pass rate | n | p95 seconds | p95 calls |", "|---|---|---|---|---|---|---|---|"]
    for cls in budgets.classes:
        for row in admission.rows(budgets, catalog, ledger, cls):
            if row.n == 0 and not row.admitted:
                continue
            p95s = ledger.p95(cls, "seconds", tier=row.tier)
            p95c = ledger.p95(cls, "calls", tier=row.tier)
            lines.append(f"| {cls} | {row.tier} | {'yes' if row.admitted else 'no'} | {row.basis} | "
                         f"{'-' if row.rate is None else f'{row.rate:.2f}'} | {row.n} | "
                         f"{'-' if p95s is None else p95s} | {'-' if p95c is None else p95c} |")

    wins = budget.windows(budgets, catalog, ledger, day)
    wts = budget.watts(budgets, catalog, ledger, day)
    lines += ["", f"## Windows and watts, {day}", "", "| resource | used | left | note |", "|---|---|---|---|"]
    short = []
    for name, w in wins.items():
        lines.append(f"| window {name} | {w.calls_used} calls, {w.tokens_used} tokens | "
                     f"{w.calls_left} calls, {w.tokens_left} tokens | "
                     f"{'EXHAUSTED' if w.exhausted else f'validators {w.validator_calls} calls'} |")
        short.append(f"{name} {_pct(w.tokens_left, w.daily_tokens)} left")
    lines.append(f"| watts | {wts.used_wh:.0f} Wh | {wts.left_wh:.0f} Wh | "
                 f"{'EXHAUSTED' if wts.exhausted else f'of {wts.daily_wh:.0f}'} |")
    short.append(f"watts {_pct(wts.left_wh, wts.daily_wh)} left")
    mwh, metered, unmetered = ledger.metered(day)
    lines.append(f"| watts measured | {mwh:.1f} Wh over {metered} metered runs | | "
                 f"{unmetered} local runs unmetered (placeholder draw, not a measurement) |")

    parked = [e for e in shift_ev if e["kind"] == "park"]
    blocked = [e for e in shift_ev if e["kind"] == "block"]
    if parked or blocked:
        lines += ["", "## Parked and blocked", ""]
        for e in parked:
            lines.append(f"- parked {e['task']}: {e['reason']}")
        for e in blocked:
            lines.append(f"- blocked {e['task']}: {e['reason']}")
    esc = [e for e in shift_ev if e["kind"] == "escalate"]
    if esc:
        lines += ["", "## Escalations", ""] + [f"- {e['task']}: {e['frm']} -> {e['to']}" for e in esc]
    voids = [e for e in events if e["kind"] == "run.void" and any(
        r["run"] == e["run"] for r in shift_ev if r["kind"] == "run.end")]
    if voids:
        lines += ["", "## Voided (harness fault, excluded from every rate)", ""] + \
            [f"- {e['run']}: {e['reason']}" for e in voids]
    fails = [r for r in runs if r["outcome"] != "pass"]
    one = (f"dark {shift_id}: {len(passes)} pass / {len(fails)} fail"
           + (f", {len(parked)} parked" if parked else "") + (f", {len(blocked)} blocked" if blocked else "")
           + (f" — {refused[-1]['detail']}" if refused else "") + " · " + " · ".join(short))
    return "\n".join(lines) + "\n", one


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[-300:]}")
    return r.stdout


def publish(text, shift_id, ledger_path, bench_dir, push_url=None, log=print):
    """Write digest/<shift>.md and digest/latest.md plus a copy of the
    ledger into the bench checkout; commit; push if a URL is given.
    Returns the relative path of the digest."""
    rel = f"digest/{shift_id}.md"
    os.makedirs(os.path.join(bench_dir, "digest"), exist_ok=True)
    os.makedirs(os.path.join(bench_dir, "ledger"), exist_ok=True)
    with open(os.path.join(bench_dir, rel), "w") as f:
        f.write(text)
    with open(os.path.join(bench_dir, "digest", "latest.md"), "w") as f:
        f.write(text)
    if os.path.exists(ledger_path):
        shutil.copyfile(ledger_path, os.path.join(bench_dir, "ledger", "ledger.jsonl"))
    if os.path.isdir(os.path.join(bench_dir, ".git")):
        try:
            _git("add", "-A", "digest", "ledger", cwd=bench_dir)
            if _git("status", "--porcelain", "--", "digest", "ledger", cwd=bench_dir).strip():
                _git("-c", "user.name=dark-runner", "-c", "user.email=dark-runner@git-host.local",
                     "commit", "-qm", f"digest: shift {shift_id}", cwd=bench_dir)
            if push_url:
                _git("push", "-q", push_url, "HEAD:main", cwd=bench_dir)
        except RuntimeError as e:
            log(f"digest publish: {e}")
    return rel

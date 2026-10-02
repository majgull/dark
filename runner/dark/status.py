"""dark/status.py — the running and last finished runs, read from the
ledger alone: no network call, no computed envelope. One row per run,
pairing its run.start with its run.end (if any) by run id.
"""

import datetime as _dt


def rows(ledger_rows, last=10):
    """The running runs (a run.start with no run.end yet), oldest first,
    then the last `last` finished runs, newest first. Each row is a dict
    with state (`running`, or the ledger's own outcome), kind (the failure
    kind or None), arm, task, tier, started (the run.start row's iso),
    seconds (None while running), calls, issue and records. A row this
    function cannot build, for want of a field a damaged or older record
    left out, is skipped rather than raised on."""
    starts, ends = {}, {}
    for r in ledger_rows:
        run = r.get("run")
        if run is None or "ts" not in r:
            continue
        kind = r.get("kind")
        if kind == "run.start":
            starts[run] = r
        elif kind == "run.end":
            ends[run] = r

    running = sorted((s for run, s in starts.items() if run not in ends), key=lambda r: r["ts"])
    out = []
    for s in running:
        try:
            out.append({"state": "running", "kind": None, "arm": s.get("arm"), "task": s["task"],
                        "tier": s["tier"], "started": s["iso"], "seconds": None, "calls": None,
                        "issue": s.get("issue"), "records": s.get("records")})
        except KeyError:
            continue

    finished = sorted(ends.values(), key=lambda r: r["ts"], reverse=True)[:last]
    for e in finished:
        try:
            s = starts.get(e.get("run"))
            out.append({"state": e["outcome"], "kind": e.get("fail_kind"), "arm": e.get("arm"),
                        "task": e["task"], "tier": e["tier"], "started": s["iso"] if s else e["iso"],
                        "seconds": e.get("seconds"), "calls": e.get("calls"),
                        "issue": e.get("issue"), "records": e.get("records")})
        except KeyError:
            continue
    return out


def since(row, now):
    """`<n>m` since a running row's start, or `<n>s` of a finished row's
    own seconds; `-` when a finished run's seconds was never measured."""
    if row["state"] == "running":
        started = _dt.datetime.fromisoformat(row["started"]).timestamp()
        return f"{int((now - started) // 60)}m"
    return f"{row['seconds']}s" if row["seconds"] is not None else "-"


def table(rows, now):
    """Plain text, one line per row: state, arm, task, tier, since, calls,
    issue (`#<n>` or `-`), never over 110 characters. A first line `running
    <n>, last <m> finished`."""
    running = sum(1 for r in rows if r["state"] == "running")
    lines = [f"running {running}, last {len(rows) - running} finished"]
    for r in rows:
        calls = r["calls"] if r["calls"] is not None else "-"
        issue = f"#{r['issue']}" if r["issue"] else "-"
        line = " ".join(str(x) for x in
                        (r["state"], r["arm"] or "-", r["task"], r["tier"], since(r, now), calls, issue))
        lines.append(line[:110])
    return "\n".join(lines)

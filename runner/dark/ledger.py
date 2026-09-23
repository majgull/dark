"""dark/ledger.py — the JSONL ledger: the only source for pass rates, cost,
windows, watts and admission (design §2).

Single writer: the runner process on the runner host. Every record is one
line, validated against spec.EVENTS before it is written, flushed and
fsynced. Readers tolerate a torn last line (a crash mid-write) and count
it. A record whose kind this process does not know is skipped and counted
too (`unknown`), never raised on: a shift is a long-lived process, and a
`dark void`/`dark stage` from a newer deploy beside it appends kinds it
never registered. The first pilot shift died that way, four tasks short,
on a run.void it could not read. Writers still refuse unregistered kinds.
"""

import datetime as _dt
import json
import os
import time

from . import spec


class LedgerError(Exception):
    pass


def _iso(ts):
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def day_of(ts):
    """Local calendar day of an epoch ts: the placeholder window period."""
    return _dt.datetime.fromtimestamp(ts).astimezone().date().isoformat()


class Ledger:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self.torn = 0  # torn lines seen by the last read
        self.unknown = 0  # records of a kind this process does not register, last read

    # --- write ---------------------------------------------------------------
    def emit(self, kind, **fields):
        ok, why = spec.event_ok(kind, fields)
        if not ok:
            raise LedgerError(why)
        now = self.clock()
        rec = {"v": spec.LEDGER_VERSION, "ts": now, "iso": _iso(now), "kind": kind, **fields}
        line = json.dumps(rec, sort_keys=True, default=str)
        if "\n" in line:
            raise LedgerError("a ledger record must serialise to one line")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        # One O_APPEND write() per record: a `dark stage` or `dark spec-review`
        # invoked beside a running shift appends whole lines, never interleaved.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            data = (line + "\n").encode("utf-8")
            n = os.write(fd, data)
            if n != len(data):
                raise LedgerError(f"short write to {self.path}: {n} of {len(data)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        return rec

    # --- read ----------------------------------------------------------------
    def events(self, kind=None, since=None, until=None):
        out = []
        self.torn = 0
        self.unknown = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().split("\n")
        except FileNotFoundError:
            return out
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                if i == len(lines) - 2 or i == len(lines) - 1:
                    self.torn += 1
                    continue
                raise LedgerError(f"{self.path}:{i + 1}: corrupt record")
            if rec.get("kind") not in spec.EVENTS:
                self.unknown += 1  # written by a newer deploy beside this process
                continue
            if kind is not None and rec["kind"] != kind:
                continue
            if since is not None and rec["ts"] < since:
                continue
            if until is not None and rec["ts"] >= until:
                continue
            out.append(rec)
        return out

    def voided(self):
        return {e["run"] for e in self.events("run.void")}

    @staticmethod
    def level_of(r, legacy=None):
        """dark's thinking level a run.end stands for: the recorded one, else
        `legacy`, what a record from before the levels existed (decision 13)
        counts as: "none" for a tier that does not think, None for a thinking
        tier whose thinking was then uncontrolled and belongs to no level."""
        return r.get("think") or legacy

    def runs(self, cls=None, tier=None, arm=None, since=None, include_void=False, think=None, legacy=None):
        """run.end records, chronological, optionally filtered; voided runs
        (a harness fault, see run.void) are skipped unless asked for. With
        `think`, only runs at that level (decision 14: rates and envelopes
        are per level, a tier's uncontrolled past never pools with them)."""
        skip = set() if include_void else self.voided()
        return [r for r in self.events("run.end", since=since)
                if r["run"] not in skip
                and (cls is None or r["cls"] == cls)
                and (tier is None or r["tier"] == tier)
                and (arm is None or r.get("arm") == arm)
                and (think is None or self.level_of(r, legacy) == think)]

    def pass_rate(self, cls, tier, last, think=None, legacy=None):
        """(rate, n) over the last `last` runs of (cls, tier); rate None when n == 0.
        Structural failures and aborts are not the model's (a provider 502, a
        VM that would not start, the gate powering the host down) and are
        left out of the denominator (decision 11, 2026-09-02: ollama-cloud
        answered a third of the afternoon's requests with 502)."""
        rs = [r for r in self.runs(cls=cls, tier=tier, think=think, legacy=legacy)
              if r["outcome"] not in ("fail:structural", "abort")][-last:]
        if not rs:
            return None, 0
        return sum(1 for r in rs if r["outcome"] == "pass") / len(rs), len(rs)

    def p95(self, cls, field, tier=None, think=None, legacy=None):
        """p95 of `field` over passes of the class (any tier unless given); None if no passes."""
        vals = sorted(r[field] for r in self.runs(cls=cls, tier=tier, think=think, legacy=legacy)
                      if r["outcome"] == "pass" and isinstance(r.get(field), (int, float)))
        if not vals:
            return None
        idx = max(0, int(round(0.95 * len(vals) + 0.5)) - 1)
        return vals[min(idx, len(vals) - 1)]

    def passes(self, cls, tier=None, think=None, legacy=None):
        return sum(1 for r in self.runs(cls=cls, tier=tier, think=think, legacy=legacy) if r["outcome"] == "pass")

    def _spend(self, tiers, day, validator_tiers=()):
        """calls/tokens on `day` by any tier in `tiers`, split by validator use."""
        tot = {"calls": 0, "tokens": 0, "validator_calls": 0, "validator_tokens": 0}
        for r in self.events():
            if r["kind"] not in ("run.end", "call") or day_of(r["ts"]) != day:
                continue
            if r["tier"] not in tiers:
                continue
            # None = NOT MEASURED. A run the runner ended (deadline, silent kill,
            # abort) carries the calls its progress tags showed started and no
            # token counts: it is charged its reservation per call, never zero
            # (949 review 3.1). A session arm without counts and without a
            # reservation is charged nothing, and the digest shows n/a.
            if r["kind"] == "run.end":
                calls = r.get("calls")
                if calls is None:
                    calls = r.get("reserved_calls") or 0
                if r.get("tokens_in") is None:
                    toks = calls * (r.get("reserved_per_call") or 0)
                else:
                    toks = (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
            else:
                calls = 1
                toks = (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
            tot["calls"] += calls
            tot["tokens"] += toks
            if r["kind"] == "call" and r["tier"] in validator_tiers:
                tot["validator_calls"] += calls
                tot["validator_tokens"] += toks
        return tot

    def window_used(self, tiers, day, validator_tiers=()):
        return self._spend(set(tiers), day, set(validator_tiers))

    def watts_used(self, day, draw, local_class_of):
        """Wh spent on `day`: the measured `wh` of every run that carries one
        (dark/power.py, any tier: the executor VM draws on the same host),
        else, for rows metered before the meter existed, seconds *
        draw[watts class] / 3600 for local tiers (a placeholder, never
        reported as a measurement). `local_class_of` maps tier id -> watts
        class (None for non-local)."""
        wh = 0.0
        for r in self.events():
            if r["kind"] not in ("run.end", "call") or day_of(r["ts"]) != day:
                continue
            if r.get("wh") is not None:
                wh += r["wh"]
                continue
            wc = r.get("watts_class") or local_class_of(r["tier"])
            if wc:
                wh += (r.get("seconds") or 0) * draw[wc] / 3600.0
        return wh

    def metered(self, day):
        """(measured Wh, metered runs, unmetered local runs) on `day`: what of
        watts_used is a measurement and what is a placeholder."""
        wh, n, unmetered = 0.0, 0, 0
        for r in self.events("run.end"):
            if day_of(r["ts"]) != day:
                continue
            if r.get("wh") is not None:
                wh += r["wh"]
                n += 1
            elif r.get("watts_class"):
                unmetered += 1
        return wh, n, unmetered

    def last(self, kind, **match):
        for r in reversed(self.events(kind)):
            if all(r.get(k) == v for k, v in match.items()):
                return r
        return None

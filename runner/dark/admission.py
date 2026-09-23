"""dark/admission.py — which tiers may run which class, derived from the
ledger, never typed (design §5).

A (class, tier) pair with at least min_runs runs is admitted iff its pass
rate over the last last_runs runs is at or above the class's admit_at.
With fewer runs it is admitted provisionally iff the class's provisional
list names it. Order: measured tiers first, cheapest by cost_order, then
provisional tiers in the list's order (operator's seed policy is the pick
until data replaces it). The pick is the first admitted tier that is not
cooling and whose window or watts are open; escalation is one extra run
on the open admitted tier with the best measured rate (decision 20), only
on the outcomes spec.ESCALATES names. There is no ladder.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    cls: str
    tier: str
    admitted: bool
    basis: str        # "measured" | "provisional" | "excluded" | "below-threshold"
    rate: float | None
    n: int
    rank: int

    def as_dict(self):
        return {"tier": self.tier, "admitted": self.admitted, "basis": self.basis,
                "rate": None if self.rate is None else round(self.rate, 2), "n": self.n}


def rows(budgets, catalog, ledger, cls, think=None):
    """Every catalog tier judged for `cls`, cheapest first. The rate counts
    runs at the level the class runs at (`think` overrides it, as a shift's
    --think does; a tier with no level anywhere is judged on all its runs);
    a record from before the levels existed counts as "none" for a tier
    that does not think and as no level for one that does (decision 14)."""
    cb = budgets.cls(cls)
    out = []
    for m in catalog.models.values():
        lvl = think or cb.think or m.think
        legacy = "none" if m.thinking_tokens == 0 else None
        rate, n = ledger.pass_rate(cls, m.id, budgets.last_runs, think=lvl, legacy=legacy)
        if n >= budgets.min_runs:
            ok = rate >= cb.admit_at
            basis = "measured" if ok else "below-threshold"
        else:
            ok = m.id in cb.provisional
            basis = "provisional" if ok else "excluded"
        pi = cb.provisional.index(m.id) if m.id in cb.provisional else len(cb.provisional)
        cost = budgets.cost_order.index(m.rank_key)
        rank = cost if basis == "measured" else 1000 + pi * 100 + cost
        out.append(Row(cls, m.id, ok, basis, rate, n, rank))
    out.sort(key=lambda r: (r.rank, r.tier))
    return out


def admitted(budgets, catalog, ledger, cls, think=None):
    return [r.tier for r in rows(budgets, catalog, ledger, cls, think) if r.admitted]


def table(budgets, catalog, ledger, think=None):
    return {cls: [r.as_dict() for r in rows(budgets, catalog, ledger, cls, think)]
            for cls in budgets.classes}


def cooling(ledger, now):
    """{tier: seconds left} for tiers under a tier.cooldown that has not lapsed."""
    out = {}
    for e in ledger.events("tier.cooldown"):
        left = e["ts"] + e["seconds"] - now
        if left > 0:
            out[e["tier"]] = max(out.get(e["tier"], 0), left)
    return out


def candidates(admitted_tiers, exclude=(), cooling_tiers=None, is_open=None):
    """Admitted tiers in order, minus excluded, cooling and (when is_open is
    given) closed ones. is_open(tier) -> (ok, why); the closed reasons are
    returned too so a park can say why."""
    cooling_tiers = cooling_tiers or {}
    out, closed = [], []
    for t in admitted_tiers:
        if t in exclude or t in cooling_tiers:
            continue
        if is_open is not None:
            ok, why = is_open(t)
            if not ok:
                closed.append(f"{t}: {why}")
                continue
        out.append(t)
    return out, closed


def pick(admitted_tiers, exclude=(), cooling_tiers=None, is_open=None):
    """The first admitted tier not excluded, not cooling, open; or None."""
    out, _ = candidates(admitted_tiers, exclude, cooling_tiers, is_open)
    return out[0] if out else None


def escalation(admitted_tiers, from_tier, exclude=(), cooling_tiers=None, is_open=None, rates=None):
    """The tier for the single escalation run after `from_tier`. With
    `rates` ({tier: pass rate or None}) it is the open admitted tier with
    the best measured rate, ties by admitted order (decision 20, 2026-09-03:
    the next-in-order rule could never reach a strictly better tier when the
    tiers ahead of it tied). Without rates, the next admitted tier that is
    open. One extra run either way, never a ladder."""
    if from_tier not in admitted_tiers:
        return None
    if rates is None:
        rest = admitted_tiers[admitted_tiers.index(from_tier) + 1:]
        return pick(rest, exclude, cooling_tiers, is_open)
    out, _ = candidates([t for t in admitted_tiers if t != from_tier], exclude, cooling_tiers, is_open)
    if not out:
        return None
    best = max(-1.0 if rates.get(t) is None else rates[t] for t in out)
    return next(t for t in out if (-1.0 if rates.get(t) is None else rates[t]) == best)

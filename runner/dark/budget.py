"""dark/budget.py — envelopes, windows and watts, derived from the ledger
inside the hard caps (design §4).

An envelope is what one run may spend. Its soft limits are the class's
measured p95 over passes times (1 + headroom), clipped by the hard cap,
once the class has min_runs passes; the hard cap before that. A window is
a provider's daily allowance of calls and tokens; validators may only
consume the share left after `reserve_for_data_runs`. Watts are charged
for local tiers from seconds times the class draw.
"""

import math
from dataclasses import dataclass

from . import ledger as L
from . import spec

DEFAULT_CTX = 32768


@dataclass(frozen=True)
class Envelope:
    cls: str
    runs: int
    calls: int
    seconds: int
    max_reasoning_chars: int
    basis: str  # "hard" | "soft"
    # 0 = off. Not sized from the ledger like the fields above: it is the
    # shift's --max-stall (or the frozen file's per-class pin), applied on
    # top of the computed envelope (dark/shift.py env_for).
    max_stall: int = 0

    def as_dict(self):
        return {"cls": self.cls, "runs": self.runs, "calls": self.calls, "seconds": self.seconds,
                "max_reasoning_chars": self.max_reasoning_chars, "basis": self.basis,
                "max_stall": self.max_stall}


def _soft(hard_v, p95_v, headroom):
    """The soft limit: p95 of passes plus headroom, clipped to [hard/2, hard].
    The floor is what keeps the limit from ratcheting down to the easiest
    tasks: the pilot's first soft envelopes (additive, from hello-* passes)
    were 185 s and 2000 reasoning chars, and semver-go on the validator
    tier died at 66 s / 21136 chars of a run that was going to pass."""
    if p95_v is None or p95_v <= 0:
        return hard_v
    return max(hard_v // 2, min(hard_v, int(math.ceil(p95_v * (1.0 + headroom)))))


def level(budgets, catalog, cls, tier, override=None):
    """(think, legacy) for runs of `cls` on `tier`: the shift's override, else
    the class's level, else the tier's own (None = the provider's default).
    `legacy` is what a ledger record with no level counts as: "none" for a
    tier that does not think, None (no level) for one that does."""
    m = catalog.model(tier)
    think = override or budgets.cls(cls).think or m.think
    return think, ("none" if m.thinking_tokens == 0 else None)


def envelope(budgets, ledger, cls, tier=None, think=None, legacy=None):
    """The envelope one run of `cls` gets. With a tier, the soft limits come
    from that (class, tier) pair's own passes (a non-thinking tier's zero
    reasoning must not size a thinking tier's cap, nor a 20 s local pass a
    cloud tier's seconds); without one, from the whole class. With a level,
    from the pair's passes at that level only (decision 14)."""
    cb = budgets.cls(cls)
    h = cb.hard
    lv = dict(think=think, legacy=legacy)
    if ledger.passes(cls, tier, **lv) < budgets.min_runs:
        return Envelope(cls, h.runs, h.calls, h.seconds, h.max_reasoning_chars, "hard")
    return Envelope(
        cls, h.runs,
        _soft(h.calls, ledger.p95(cls, "calls", tier, **lv), cb.headroom),
        _soft(h.seconds, ledger.p95(cls, "seconds", tier, **lv), cb.headroom),
        _soft(h.max_reasoning_chars, ledger.p95(cls, "reasoning_chars", tier, **lv), cb.headroom),
        "soft")


@dataclass(frozen=True)
class WindowState:
    name: str
    daily_calls: int
    daily_tokens: int
    calls_used: int
    tokens_used: int
    validator_calls: int
    validator_tokens: int
    validator_share: float      # 1 - reserve_for_data_runs
    exhausted_by_signal: bool   # a rate-limit signature was seen today

    @property
    def calls_left(self):
        return max(0, self.daily_calls - self.calls_used)

    @property
    def tokens_left(self):
        return max(0, self.daily_tokens - self.tokens_used)

    @property
    def exhausted(self):
        return self.exhausted_by_signal or self.calls_left == 0 or self.tokens_left == 0

    def open_for(self, calls, tokens, validator=False):
        """(ok, why) for a spend of `calls` and `tokens`; validators are
        also held to their share of the window."""
        if self.exhausted_by_signal:
            return False, f"window {self.name}: rate-limited today"
        if calls > self.calls_left:
            return False, f"window {self.name}: {self.calls_left} calls left, {calls} needed"
        if tokens > self.tokens_left:
            return False, f"window {self.name}: {self.tokens_left} tokens left, {tokens} needed"
        if validator:
            cap_calls = int(self.daily_calls * self.validator_share)
            cap_tokens = int(self.daily_tokens * self.validator_share)
            if self.validator_calls + calls > cap_calls or self.validator_tokens + tokens > cap_tokens:
                return False, (f"window {self.name}: validator share used "
                               f"({self.validator_calls}/{cap_calls} calls, "
                               f"{self.validator_tokens}/{cap_tokens} tokens)")
        return True, ""

    def as_dict(self):
        return {"window": self.name, "calls_used": self.calls_used, "calls_left": self.calls_left,
                "tokens_used": self.tokens_used, "tokens_left": self.tokens_left,
                "validator_calls": self.validator_calls, "validator_tokens": self.validator_tokens,
                "exhausted": self.exhausted}


def windows(budgets, catalog, ledger, day):
    """{window name: WindowState} for `day` (local calendar day)."""
    validators = set(catalog.with_role("validator"))
    out = {}
    for name, w in budgets.windows.items():
        tiers = {m.id for m in catalog.models.values() if catalog.window_of(m.id) == name}
        used = ledger.window_used(tiers, day, validators)
        signalled = any(e.get("day") == day and e["provider"] == name
                        for e in ledger.events("window.exhausted"))
        out[name] = WindowState(
            name=name, daily_calls=w.daily_calls, daily_tokens=w.daily_tokens,
            calls_used=used["calls"], tokens_used=used["tokens"],
            validator_calls=used["validator_calls"], validator_tokens=used["validator_tokens"],
            validator_share=1.0 - w.reserve_for_data_runs, exhausted_by_signal=signalled)
    return out


@dataclass(frozen=True)
class WattsState:
    daily_wh: float
    used_wh: float

    @property
    def left_wh(self):
        return max(0.0, self.daily_wh - self.used_wh)

    @property
    def exhausted(self):
        return self.left_wh <= 0.0

    def as_dict(self):
        return {"daily_wh": self.daily_wh, "used_wh": round(self.used_wh, 1),
                "left_wh": round(self.left_wh, 1), "exhausted": self.exhausted}


def watts(budgets, catalog, ledger, day):
    def local_class_of(tier):
        m = catalog.models.get(tier)
        return m.watts if m and m.local else None
    return WattsState(budgets.daily_wh, ledger.watts_used(day, budgets.draw, local_class_of))


def reservation(catalog, model_id, env):
    """Window spend to reserve for one run of `env` on the tier: every call
    may carry the whole context plus a full reply."""
    m = catalog.model(model_id)
    return env.calls, env.calls * ((m.ctx or DEFAULT_CTX) + m.max_tokens)


def tier_open(catalog, budgets, ledger, model_id, env, day, wins=None, wts=None):
    """(ok, why): may this tier take one run of `env` today? Local tiers are
    held to watts; sub-window tiers to their provider's window. A validator
    tier doing control-plane work (a spec or review class call) is held to
    the validator share; a data run on the same tier is not (design §3)."""
    m = catalog.model(model_id)
    if m.local:
        wts = wts if wts is not None else watts(budgets, catalog, ledger, day)
        if wts.exhausted:
            return False, f"watts: {wts.used_wh:.0f} of {wts.daily_wh:.0f} Wh spent today"
        return True, ""
    wins = wins if wins is not None else windows(budgets, catalog, ledger, day)
    calls, toks = reservation(catalog, model_id, env)
    validator = env.cls in spec.CALL_CLASSES and "validator" in m.roles
    return wins[catalog.window_of(model_id)].open_for(calls, toks, validator=validator)


def today(ledger):
    return L.day_of(ledger.clock())

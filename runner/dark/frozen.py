"""dark/frozen.py — a pinned envelope for a comparison set.

A comparison set is a group of rounds whose counts are meant to be read
against each other. The soft envelope is computed from the ledger's own
recent passes, and the ledger grows while the rounds run: on 2026-09-04 one
tier ran its first round cut off at 900 seconds and its third at 450, and
the difference arrived as three budget failures that looked like the model
getting worse. A frozen file takes the limit out of the ledger. It names
calls, seconds, reasoning characters and think level per class; the runner
reads it instead of computing; and every `run.start` records the set name,
the file name and the SHA-256 of the file's bytes, so a table can say which
rounds shared a limit and a reader can check that they did.

File format (TOML):

    set = "rq2-dsf"
    note = "why this set exists"          # optional

    [class.additive]
    calls = 6
    seconds = 900
    max_reasoning_chars = 40000
    think = "low"
    runs = 1                              # optional, default 1
    max_stall = 8                         # optional, default: the shift's --max-stall

`runs` is how many runs of a task the envelope allows, which is what
escalation asks before it retries on the next tier. It defaults to 1: a
frozen comparison is one tier per task, and `--no-adapt` turns escalation
off for the same reason.

`max_stall` pins the stall cut for the class (dark/agent.py: a run ends
early once a reply repeats one already seen this many times). Unset here,
the shift's own --max-stall applies; the file wins when both are given.

Every class a shift's tasks use must be in the file. A missing one is
refused by name rather than filled from the class default, because a
comparison set with a class quietly on the moving basis is the defect this
module exists to stop.
"""

import hashlib
import os
import tomllib
from dataclasses import dataclass

from .budget import Envelope

KEYS = ("calls", "seconds", "max_reasoning_chars")


class FrozenError(Exception):
    """One line naming the file and the field."""


@dataclass(frozen=True)
class Frozen:
    set_name: str
    path: str
    sha256: str
    classes: dict          # cls -> {calls, seconds, max_reasoning_chars, think, runs}
    note: str = ""

    def has(self, cls):
        return cls in self.classes

    def envelope(self, cls):
        """The envelope one run of `cls` gets, basis "frozen"."""
        c = self.classes.get(cls)
        if c is None:
            raise FrozenError(f"{self.path}: no [class.{cls}]: this comparison set does not "
                              f"cover the {cls} class, so a run of it would fall back to the "
                              "moving basis")
        return Envelope(cls, c["runs"], c["calls"], c["seconds"], c["max_reasoning_chars"], "frozen")

    def think(self, cls):
        """The thinking level pinned for `cls` (None = the provider's default)."""
        c = self.classes.get(cls)
        return c["think"] if c else None

    def max_stall(self, cls):
        """The stall cap pinned for `cls` (None = not pinned by this file;
        the shift's own --max-stall, if any, applies)."""
        c = self.classes.get(cls)
        return c["max_stall"] if c else None

    def as_dict(self):
        """What every `run.start` of a frozen shift carries."""
        return {"set": self.set_name, "file": os.path.basename(self.path), "sha256": self.sha256}


def load(path):
    """Read a frozen file. Refuses with one line on any miss."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise FrozenError(f"{path}: {e.strerror or e}") from e
    sha = hashlib.sha256(raw).hexdigest()
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise FrozenError(f"{path}: {e}") from e
    name = doc.get("set")
    if not name or not isinstance(name, str):
        raise FrozenError(f"{path}: no `set` name: a frozen envelope is named so a table can cite it")
    table = doc.get("class")
    if not isinstance(table, dict) or not table:
        raise FrozenError(f"{path}: no [class.*] section: nothing is pinned")
    classes = {}
    for cls, c in table.items():
        if not isinstance(c, dict):
            raise FrozenError(f"{path}: [class.{cls}] is not a table")
        for k in KEYS:
            v = c.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise FrozenError(f"{path}: [class.{cls}] {k}: a positive whole number is required, "
                                  f"got {c.get(k)!r}")
        runs = c.get("runs", 1)
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
            raise FrozenError(f"{path}: [class.{cls}] runs: a whole number of at least 1, got {runs!r}")
        think = c.get("think")
        if think is not None and not isinstance(think, str):
            raise FrozenError(f"{path}: [class.{cls}] think: a level name or nothing, got {think!r}")
        max_stall = c.get("max_stall")
        if max_stall is not None and (not isinstance(max_stall, int) or isinstance(max_stall, bool)
                                       or max_stall < 0):
            raise FrozenError(f"{path}: [class.{cls}] max_stall: a non-negative whole number or "
                              f"nothing, got {max_stall!r}")
        unknown = sorted(set(c) - set(KEYS) - {"runs", "think", "max_stall"})
        if unknown:
            raise FrozenError(f"{path}: [class.{cls}]: unknown key(s) {', '.join(unknown)}: a typo "
                              "must not become a limit nobody set")
        classes[cls] = {k: c[k] for k in KEYS} | {"runs": runs, "think": think, "max_stall": max_stall}
    return Frozen(set_name=name, path=os.path.abspath(path), sha256=sha, classes=classes,
                  note=doc.get("note", "") or "")


def check_levels(fr, budgets):
    """Every pinned think level must be one the budgets define. Returns a
    refusal line or ""."""
    known = set(budgets.think["levels"])
    bad = sorted({f"{cls}: {c['think']}" for cls, c in fr.classes.items()
                  if c["think"] is not None and c["think"] not in known})
    if bad:
        return (f"{fr.path}: think level(s) not in budgets [think]: {', '.join(bad)}; "
                f"known: {', '.join(sorted(known))}")
    return ""


def covers(fr, task_list):
    """The classes in `task_list` that the file does not pin, sorted."""
    return sorted({t.cls for t in task_list if not fr.has(t.cls)})

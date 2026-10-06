#!/usr/bin/env python3
"""dark critic - check one run's records before a person sees its demo.

`dark demo` cuts a video from whatever a run left behind. A missing
screenshot, or a verdict whose own note says the opposite, reaches the viewer
as a demo that looks complete. The critic is the named check that reads the
records first and refuses to let that happen: it is separate from re-planning
and separate from the demo, and every finding it returns is a plain sentence
naming a step.

A **critic** reads one run's records directory and returns findings; a
**finding** is one thing wrong, `{rule, step, detail}`, where `rule` names the
check, `step` is the numbered step it concerns (None when it is about the run
as a whole) and `detail` is one sentence a person can act on. The rules are:

- `steps`: every numbered step in `task.json` has exactly one verdict in
  `steps.jsonl`, and every verdict belongs to a numbered step.
- `screenshot`: every verdict has its `steps/<NN>.png`, and that PNG is not a
  single flat colour.
- `note`: a step whose verdict is `pass` has no note saying it failed, and a
  step whose verdict is `fail` has no note saying it passed, by the word list
  below.
- `outcome`: the outcome the records claim in `README.md` is the one the
  verdicts imply: `pass` only when every step passed, `fail` when a step
  failed, `inconclusive` otherwise. A records directory with no `README.md`
  makes no claim, so the rule does not apply.

Stdlib only, no ffmpeg, no network: the critic is a plain function the tests
drive (`check`), and `dark critic` prints one line per finding.
"""

import json
import os
import re
import struct
import zlib

TASK_FILE = "task.json"
STEPS_FILE = "steps.jsonl"
README_FILE = "README.md"
STEPS_DIR = "steps"

# the whole word list of the `note` rule: a verdict that is contradicted by its
# own note is the defect the demo shows a person, and nothing else is checked
FAIL_WORDS = ("fail", "failed", "fails", "failure", "broken")
PASS_WORDS = ("pass", "passed", "passes", "works", "worked", "success")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# the badge a person reads first: **🟢 PASS**: 3/3 steps pass
_BADGE = re.compile(r"\*\*[^*\n]*?(PASS|FAIL|INCONCLUSIVE)\*\*")


class Finding(dict):
    """One thing the critic found: `{"rule", "step", "detail"}`, with the keys
    also readable as attributes."""

    def __init__(self, rule, step, detail):
        super().__init__(rule=rule, step=step, detail=detail)

    @property
    def rule(self):
        return self["rule"]

    @property
    def step(self):
        return self["step"]

    @property
    def detail(self):
        return self["detail"]


def line(finding):
    """One finding as the one line `dark critic` prints."""
    where = f"step {finding['step']}: " if finding["step"] is not None else ""
    return f"{finding['rule']}: {where}{finding['detail']}"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _jsonl(path):
    """The file's JSON object lines, in order; a missing file is no lines and a
    line that is not a JSON object is skipped, as the arm itself skips it."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        return []
    return rows


def check(records_dir):
    """Every finding in one run's records, in rule order. An empty list is a
    records directory the demo can cut as it is."""
    findings = []
    task = _read_json(os.path.join(records_dir, TASK_FILE))
    steps = list(task.get("steps") or []) if isinstance(task, dict) else []
    verdicts = _jsonl(os.path.join(records_dir, STEPS_FILE))
    _check_steps(findings, task, steps, verdicts)
    _check_screenshots(findings, records_dir, verdicts)
    _check_notes(findings, verdicts)
    _check_outcome(findings, records_dir, verdicts)
    return findings


def _check_steps(findings, task, steps, verdicts):
    """The `steps` rule: exactly one verdict per numbered step."""
    if not isinstance(task, dict):
        findings.append(Finding("steps", None, "task.json is missing or not a JSON object"))
        return
    if not steps:
        findings.append(Finding("steps", None, "task.json names no numbered steps"))
        return
    by_step, extras = {}, []
    for v in verdicts:
        n = v.get("step")
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(steps):
            by_step.setdefault(n, []).append(v)
        else:
            # a malformed or unknown step value is a finding below; it is never
            # a dict key, because a JSON list or object is not hashable
            extras.append(v)
    for n in range(1, len(steps) + 1):
        got = by_step.get(n, [])
        if len(got) != 1:
            findings.append(Finding("steps", n,
                                    f"step {n} has {len(got)} verdicts in steps.jsonl, expected exactly one"))
    for v in extras:
        n = v.get("step")
        findings.append(Finding("steps", n if isinstance(n, int) and not isinstance(n, bool) else None,
                                f"steps.jsonl has a verdict for step {n!r}, which task.json does not number"))


def _check_screenshots(findings, records_dir, verdicts):
    """The `screenshot` rule: the file is there, and not one flat colour."""
    for v in verdicts:
        n = v.get("step")
        if not isinstance(n, int) or isinstance(n, bool):
            continue
        name = f"{n:02d}.png"
        path = os.path.join(records_dir, STEPS_DIR, name)
        if not os.path.isfile(path):
            findings.append(Finding("screenshot", n, f"no screenshot steps/{name}"))
            continue
        try:
            flat = png_is_flat(path)
        except (OSError, ValueError) as e:
            findings.append(Finding("screenshot", n, f"steps/{name} cannot be read: {e}"))
            continue
        if flat:
            findings.append(Finding("screenshot", n, f"steps/{name} is a single flat colour"))


def _word_re(words):
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.IGNORECASE)


_FAIL_RE = _word_re(FAIL_WORDS)
_PASS_RE = _word_re(PASS_WORDS)


def _check_notes(findings, verdicts):
    """The `note` rule: a verdict's own note never says the opposite."""
    for v in verdicts:
        note = str(v.get("note") or "")
        step = v.get("step")
        if v.get("verdict") == "pass" and _FAIL_RE.search(note):
            findings.append(Finding("note", step, f'a pass verdict\'s note says it failed: "{note[:160]}"'))
        elif v.get("verdict") == "fail" and _PASS_RE.search(note):
            findings.append(Finding("note", step, f'a fail verdict\'s note says it passed: "{note[:160]}"'))


def verdict_outcome(verdicts):
    """The outcome the verdicts imply: `pass` only when every step passed,
    `fail` when any step failed, else `inconclusive`."""
    vs = [v.get("verdict") for v in verdicts]
    if vs and all(v == "pass" for v in vs):
        return "pass"
    return "fail" if "fail" in vs else "inconclusive"


def claimed_outcome(records_dir):
    """The outcome `README.md` claims, lower-cased, or None when there is no
    README.md or no badge in it (no claim to check)."""
    text = None
    try:
        with open(os.path.join(records_dir, README_FILE), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = _BADGE.search(text)
    return m.group(1).lower() if m else None


def _check_outcome(findings, records_dir, verdicts):
    """The `outcome` rule: the claim the records make matches the verdicts."""
    claimed = claimed_outcome(records_dir)
    if claimed is None:
        return
    want = verdict_outcome(verdicts)
    if claimed != want:
        findings.append(Finding("outcome", None,
                                f"README.md claims {claimed}, the verdicts imply {want}"))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png_pixels(data):
    """(width, height, channels, rows) of an 8-bit, non-interlaced PNG, each
    row unfiltered; ValueError when it is not one."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    pos, idat = 8, b""
    width = height = depth = colour = interlace = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if typ == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    if width is None:
        raise ValueError("no IHDR")
    if depth != 8:
        raise ValueError(f"unsupported bit depth {depth}")
    if interlace:
        raise ValueError("interlaced PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise ValueError(f"unsupported colour type {colour}")
    try:
        raw = zlib.decompress(idat)
    except zlib.error as e:
        raise ValueError(f"bad image data: {e}")
    stride = width * channels
    if height and len(raw) < (stride + 1) * height:
        raise ValueError("truncated image data")
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(height):
        ftype = raw[p]
        p += 1
        row = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                row[i] = (row[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"unknown filter {ftype}")
        rows.append(bytes(row))
        prev = row
    return width, height, channels, rows


def png_is_flat(path):
    """True when every pixel of the PNG is the same colour; ValueError when the
    file is not a PNG this can read."""
    with open(path, "rb") as f:
        data = f.read()
    width, height, channels, rows = _png_pixels(data)
    if not rows:
        return True
    first = rows[0][:channels]
    for row in rows:
        for x in range(0, width * channels, channels):
            if row[x:x + channels] != first:
                return False
    return True

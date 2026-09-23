"""dark/review.py — the validator tier confirms that a task's hidden
acceptance follows from its spec (design §7, operator's decision 4: models
only; the frontier tier writes, the validator tier confirms). One budgeted
call per task on the validator tier, recorded as a `call` event; the
verdict is written next to the task so it ships with the bench.
"""

import os
import re
import time

from . import budget, llm

SYSTEM = """You are the validator of a coding benchmark. A task has a SPEC (all the
implementer ever sees, plus the starting files), a hidden ACCEPTANCE script
(run after the work, printing `CHECK <name> ok|fail` lines) and an ORACLE
(a reference solution, never shown to implementers). Your job: decide, for
every CHECK the acceptance can print, whether a competent engineer who
implemented the SPEC alone would pass it.

Answer in exactly this shape and nothing else:

CHECK <name>: IMPLIED | NOT IMPLIED — <one line why>
... one line per CHECK name that appears in the acceptance ...
VERDICT: OK | AMBIGUOUS — <one line>

VERDICT is OK only when every CHECK is IMPLIED by the SPEC. Be strict about
exact output formats, exit codes and file names: if the SPEC does not pin
something the acceptance tests, that CHECK is NOT IMPLIED."""


def _files(root, limit=12000):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d != ".git")
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            try:
                with open(p) as f:
                    body = f.read(limit)
            except (OSError, UnicodeDecodeError):
                continue
            out.append(f"--- {os.path.relpath(p, root)}\n{body}")
    return "\n".join(out)


def prompt(task):
    parts = [f"TASK ID: {task.id}\nCLASS: {task.cls}\nLANG: {task.lang}\nMAY EDIT: {', '.join(task.may_edit) or 'none'}",
             f"SPEC:\n{task.spec}"]
    if os.path.isdir(task.start_dir):
        parts.append(f"STARTING FILES (the implementer sees these):\n{_files(task.start_dir)}")
    parts.append(f"ACCEPTANCE (hidden):\n{_files(task.acceptance_dir)}")
    if os.path.isdir(task.oracle_dir):
        parts.append(f"ORACLE (hidden):\n{_files(task.oracle_dir)}")
    return "\n\n".join(parts)


def parse(text):
    """(verdict, [(check, implied, why)]) from the model's reply."""
    checks = []
    # `CHECK <name> ok:` too: the validator copies the acceptance's own line
    # shape (dsf on lru-cache-go wrote every name that way, 0/6 parsed)
    for m in re.finditer(r"^CHECK\s+(\S+?)(?:\s+(?:ok|fail))?:\s*(IMPLIED|NOT IMPLIED)\s*[—-]*\s*(.*)$",
                         text, re.MULTILINE | re.IGNORECASE):
        checks.append((m.group(1), m.group(2).upper() == "IMPLIED", m.group(3).strip()))
    v = re.search(r"^VERDICT:\s*(OK|AMBIGUOUS)\b(.*)$", text, re.MULTILINE | re.IGNORECASE)
    verdict = v.group(1).upper() if v else "UNPARSED"
    if verdict == "OK" and any(not ok for _, ok, _ in checks):
        verdict = "AMBIGUOUS"  # the line list wins over a careless verdict
    return verdict, checks


def review_task(task, catalog, budgets, ledger, tier, shift="adhoc", chat=llm.chat, clock=time.time):
    """One validator call; returns (verdict, checks, reply, usage)."""
    m = catalog.model(tier)
    prov = catalog.provider_of(tier)
    cap = budgets.cls("spec").hard
    text = prompt(task)
    if not m.local:
        # the validator share (design §3): this is control-plane spend. The
        # reservation is this prompt plus a full reply, not a whole context.
        day = budget.today(ledger)
        w = budget.windows(budgets, catalog, ledger, day)[catalog.window_of(tier)]
        open_ok, why = w.open_for(1, len(text) // 4 + m.max_tokens, validator="validator" in m.roles)
        if not open_ok:
            return "SKIPPED", [], f"(no call: {why})", {"tokens_in": 0, "tokens_out": 0, "reasoning_chars": 0}
    t0 = clock()
    ok = False
    try:
        # the tier's full reply budget: a validator that thinks in its content
        # (dsf did, 4096 tokens on wcl-python) must still reach its VERDICT line
        reply, usage = chat(prov.url, tier, [{"role": "system", "content": SYSTEM},
                                              {"role": "user", "content": text}],
                            m.max_tokens, m.timeout, rate_signature=m.rate_limit_signature)
        ok = usage.get("finish_reason") != "length"
    except llm.LLMError as e:
        reply, usage = f"VERDICT: UNPARSED — call failed: {e}", {"tokens_in": 0, "tokens_out": 0, "reasoning_chars": 0}
    over_cap = usage.get("reasoning_chars", 0) > cap.max_reasoning_chars
    ledger.emit("call", shift=shift, cls="spec", tier=tier, seconds=int(clock() - t0),
                tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0),
                reasoning_chars=usage.get("reasoning_chars", 0), paid=m.paid, task=task.id,
                purpose="spec-review", ok=ok and not over_cap)
    verdict, checks = parse(reply)
    if over_cap:
        reply = f"(reasoning over the spec cap: {usage['reasoning_chars']} chars; verdict discarded)\n" + reply
        verdict = "UNPARSED"
    elif usage.get("finish_reason") == "length":
        reply = "(reply cut at the tier's max_tokens; verdict discarded)\n" + reply
        verdict = "UNPARSED"
    return verdict, checks, reply, usage


def render(task, tier, verdict, checks, reply, usage, when):
    lines = [f"# spec review: {task.id}", "",
             f"validator {tier}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(when))}, "
             f"tokens {usage.get('tokens_in', 0)}/{usage.get('tokens_out', 0)}", "",
             f"**VERDICT: {verdict}**", ""]
    for name, ok, why in checks:
        lines.append(f"- {name}: {'implied' if ok else 'NOT IMPLIED'}{' — ' + why if why else ''}")
    lines += ["", "<details><summary>raw reply</summary>", "", "```", reply.strip(), "```", "", "</details>", ""]
    return "\n".join(lines)

"""dark/preflight.py — refuse the shift with one line on any miss
(design §6). Every configured model id served, tokens valid, Gitea and
the org reachable, ledger writable, templates present, Proxmox reachable
(waking it through the gate if it sleeps) and the VM template present.
Windows and watts are reported, never refused on: which of them a shift
needs depends on the tiers its tasks land on, and the shift already parks
a task whose class has no open tier. The second pilot shift (local tiers
only) was refused for a claude window the session arms had spent, and its
cloud arm behind it for the same reason. Cheap local checks run first,
the wake last.
"""

import os
import time
from dataclasses import dataclass

from . import admission, budget
from . import gitea as G
from . import llm, tasks


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self):
        return f"{'ok  ' if self.ok else 'MISS'} {self.name}" + (f": {self.detail}" if self.detail else "")


class Preflight:
    def __init__(self, catalog, budgets, host, ledger, gitea, px, clock=time.time, log=print,
                 chat=llm.chat, served=llm.served_models, shift="adhoc", sleep=time.sleep):
        self.catalog = catalog
        self.budgets = budgets
        self.host = host
        self.ledger = ledger
        self.gitea = gitea
        self.px = px
        self.clock = clock
        self.log = log
        self.chat = chat
        self.served = served
        self.shift = shift
        self.sleep = sleep

    def run(self, need_vm=True):
        """[Check], in order. Stops at the first miss of a dependency
        (no Gitea -> no org check) but otherwise reports every miss so one
        read fixes them all."""
        out = []

        def add(name, ok, detail=""):
            out.append(Check(name, bool(ok), str(detail)))
            return bool(ok)

        # 1. tokens
        add("admin token", bool(self.host.admin_token), "" if self.host.admin_token
            else f"{self.host.admin_token_file} unreadable or empty")
        add("agent token", bool(self.host.agent_token), "" if self.host.agent_token
            else f"{self.host.agent_token_file} unreadable or empty")
        # 2. ledger writable
        try:
            os.makedirs(os.path.dirname(self.host.ledger_path), exist_ok=True)
            with open(self.host.ledger_path, "a"):
                pass
            add("ledger writable", True, self.host.ledger_path)
        except OSError as e:
            add("ledger writable", False, f"{self.host.ledger_path}: {e.strerror}")
        # 3. templates
        missing = [l for l in tasks.LANGS if not os.path.isfile(
            os.path.join(self.host.templates_dir, l, ".factory", "verify.sh"))]
        add("templates", not missing, f"{self.host.templates_dir}: no verify.sh for {missing}" if missing
            else self.host.templates_dir)
        # 4. gitea + org
        try:
            v = self.gitea.version()
            who = self.gitea.whoami()
            add("gitea", True, f"{v} as {who}")
            for o in dict.fromkeys((self.host.org, self.host.work_org)):
                exists = self.gitea.org_exists(o)
                add("org", exists, f"org {o!r}" + ("" if exists else " missing (ops/gitea-bootstrap.sh)"))
                if exists:
                    # the org existing is not the same as the agent being able
                    # to push into it (dark-runs, 2026-09-03)
                    add(f"org {o} writable", *self.gitea.write_team(o, self.host.agent_user))
        except G.GiteaError as e:
            add("gitea", False, str(e))
        # 5. catalog: every configured id served by its provider
        for pname, prov in self.catalog.providers.items():
            want = self.catalog.ids_by_provider(pname)
            if not want:
                continue
            try:
                have = self.served(prov)
            except llm.LLMError as e:
                add(f"catalog {pname}", False, str(e))
                continue
            gone = sorted(want - have)
            add(f"catalog {pname}", not gone, f"not served: {gone}" if gone else f"{len(want)} ids served")
        # 6. windows and watts today: information, never a refusal (the shift
        # parks a task whose class has no open tier; a local-only shift must
        # not die for a cloud window, nor a cloud arm for the watts)
        day = budget.today(self.ledger)
        for name, w in budget.windows(self.budgets, self.catalog, self.ledger, day).items():
            add(f"window {name}", True,
                f"{w.calls_left} calls, {w.tokens_left} tokens left" if not w.exhausted
                else "exhausted today (tiers on it park)")
        wts = budget.watts(self.budgets, self.catalog, self.ledger, day)
        add("watts", True, f"{wts.left_wh:.0f} of {wts.daily_wh:.0f} Wh left"
            + (" (spent: local tiers park)" if wts.exhausted else ""))
        # 7. admission: every class has at least one admitted tier
        for cls in self.budgets.classes:
            adm = admission.admitted(self.budgets, self.catalog, self.ledger, cls)
            add(f"admission {cls}", bool(adm), ", ".join(adm) if adm else "no admitted tier")
        if not need_vm:
            return out
        # 8. proxmox reachable, waking the compute plane through the gate if needed
        up = self.px.reachable()
        if not up:
            up = self.wake()
        add("proxmox", up, self.host.proxmox if up else f"{self.host.proxmox}: unreachable after wake")
        if up:
            ok, why = self.px.template_ok()
            add("vm template", ok, why or f"VM {self.budgets.shift['vm_template']} is a template")
        return out

    def wake(self):
        """A real chat request to the cheapest local tier of a provider with
        wake = true: the gate WOL-wakes the compute plane on it. Returns the
        new reachability. A shift does this in preflight; `dark stage` does
        it on its own, since a session arm's staging can come hours after
        the last shift (2026-09-03 16:48: 24 stagings against a host the
        gate had put to sleep, every one "no route to host")."""
        wakers = [p for p in self.catalog.providers.values() if p.wake]
        if not wakers:
            return False
        prov = wakers[0]
        local = [m for m in self.catalog.models.values() if m.provider == prov.name]
        local.sort(key=lambda m: self.budgets.cost_order.index(m.rank_key))
        if not local:
            return False
        m = local[0]
        self.log(f"preflight: waking {prov.name} via {m.id} (up to {self.host.wake_timeout}s)")
        t0 = self.clock()
        ok = False
        try:
            _, usage = self.chat(prov.url, m.id, [{"role": "user", "content": "Reply with exactly: ok"}],
                                 8, self.host.wake_timeout)
            ok = True
        except llm.LLMError as e:
            usage = {"tokens_in": 0, "tokens_out": 0, "reasoning_chars": 0}
            self.log(f"preflight: wake failed: {e}")
        self.ledger.emit("call", shift=self.shift, cls="probe", tier=m.id,
                         seconds=int(self.clock() - t0), tokens_in=usage.get("tokens_in", 0),
                         tokens_out=usage.get("tokens_out", 0),
                         reasoning_chars=usage.get("reasoning_chars", 0), paid=m.paid,
                         purpose="wake", ok=ok)
        if not ok:
            return False
        for _ in range(6):
            if self.px.reachable():
                return True
            self.sleep(10)
        return self.px.reachable()

    _wake = wake


def refusal_line(checks):
    misses = [c for c in checks if not c.ok]
    if not misses:
        return ""
    first = misses[0]
    more = f" (+{len(misses) - 1} more)" if len(misses) > 1 else ""
    return f"preflight refused: {first.name}: {first.detail}{more}"

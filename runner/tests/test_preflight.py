import os
import tempfile
import unittest

from dark import config, llm, preflight
from dark import gitea as G
from dark import ledger as L
from tests import fakes
from tests.test_config import BUDGETS, MODELS, write_conf
from tests.test_ledger import run_end
from tests.test_tasks import make_templates


class FakePx:
    def __init__(self, reachable=True, template=(True, "")):
        self.up = reachable
        self.template = template

    def reachable(self):
        return self.up

    def template_ok(self):
        return self.template


class Preflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gfake = fakes.FakeGitea()
        cls.gfake.orgs.add("dark")
        # the team the executor VMs push through (ops/gitea-bootstrap.sh)
        cls.gfake.teams["dark"] = [{"id": 1, "name": "agents", "permission": "write",
                                    "includes_all_repositories": True, "members": ["dark-agent"]}]

    @classmethod
    def tearDownClass(cls):
        cls.gfake.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cat, self.bud = config.load(write_conf(self.tmp, MODELS, BUDGETS))
        self.host = config.Host(org="dark", gitea_url=self.gfake.url, state_dir=os.path.join(self.tmp, "state"),
                                templates_dir=make_templates(os.path.join(self.tmp, "templates")),
                                admin_token="good", agent_token="agent-tok", wake_timeout=5)
        self.led = L.Ledger(self.host.ledger_path)
        self.gitea = G.Gitea(self.host.gitea_url, "good")
        self.served_ids = {"gate": {"local-a", "local-b"}, "cloud": {"cloud-x"}}
        self.chats = []

    def served(self, prov):
        if prov.name not in self.served_ids:
            raise llm.LLMError("conn", f"{prov.name} down")
        return self.served_ids[prov.name]

    def chat(self, url, model, messages, max_tokens, timeout, **kw):
        self.chats.append((url, model))
        return "ok", {"tokens_in": 5, "tokens_out": 1, "reasoning_chars": 0, "seconds": 1}

    def pre(self, px=None, **kw):
        return preflight.Preflight(self.cat, self.bud, self.host, self.led, self.gitea, px or FakePx(),
                                   log=lambda *a: None, chat=self.chat, served=self.served, sleep=lambda s: None, **kw)

    def misses(self, checks):
        return [c.name for c in checks if not c.ok]

    def test_all_ok(self):
        checks = self.pre().run()
        self.assertEqual(self.misses(checks), [], [c.line() for c in checks])
        self.assertEqual(preflight.refusal_line(checks), "")
        names = [c.name for c in checks]
        for n in ("admin token", "agent token", "ledger writable", "templates", "gitea", "org", "org dark writable",
                  "catalog gate", "catalog cloud", "window cloud", "watts", "admission additive", "proxmox", "vm template"):
            self.assertIn(n, names)

    def test_missing_token_and_org(self):
        self.host.admin_token = ""
        self.gfake.orgs.discard("dark")
        try:
            checks = self.pre().run(need_vm=False)
        finally:
            self.gfake.orgs.add("dark")
        self.assertIn("admin token", self.misses(checks))
        self.assertIn("org", self.misses(checks))
        line = preflight.refusal_line(checks)
        self.assertTrue(line.startswith("preflight refused: admin token:"))
        self.assertIn("more", line)

    def test_catalog_gap_names_the_id(self):
        self.served_ids["gate"] = {"local-a"}
        checks = self.pre().run(need_vm=False)
        gap = [c for c in checks if c.name == "catalog gate"][0]
        self.assertFalse(gap.ok)
        self.assertIn("local-b", gap.detail)

    def test_provider_down(self):
        del self.served_ids["cloud"]
        checks = self.pre().run(need_vm=False)
        self.assertIn("catalog cloud", self.misses(checks))

    def test_window_exhausted_and_watts(self):
        self.led.emit("window.exhausted", provider="cloud", day=L.day_of(self.led.clock()))
        self.led.emit("run.end", **run_end(tier="local-b", watts_class="high", seconds=3600))
        checks = self.pre().run(need_vm=False)
        # windows and watts are reported, never refused on: a spent window
        # still lets the shift start
        self.assertEqual(self.misses(checks), [])
        by = {c.name: c for c in checks}
        self.assertIn("exhausted", by["window cloud"].detail)
        self.assertIn("spent", by["watts"].detail)

    def test_an_org_the_agent_cannot_push_into_is_refused(self):
        # the org exists and every other check is green, but the executor's
        # token has no write on the repos the runs create: every run would
        # get through its model calls and die on the push
        self.gfake.teams["dark"] = [{"id": 2, "name": "readers", "permission": "read",
                                     "includes_all_repositories": True, "members": ["dark-agent"]}]
        try:
            checks = self.pre().run(need_vm=False)
        finally:
            self.gfake.teams["dark"] = [{"id": 1, "name": "agents", "permission": "write",
                                         "includes_all_repositories": True, "members": ["dark-agent"]}]
        self.assertIn("org dark writable", self.misses(checks))
        self.assertIn("no team in dark", preflight.refusal_line(checks))

    def test_a_per_unit_grant_counts_as_write(self):
        # Gitea reports permission "none" for a team whose grant is per unit
        # and puts the real one in units_map; the push needs repo.code
        self.gfake.teams["dark"] = [{"id": 4, "name": "agents", "permission": "none",
                                     "units_map": {"repo.code": "write"},
                                     "includes_all_repositories": True, "members": ["dark-agent"]}]
        try:
            checks = self.pre().run(need_vm=False)
        finally:
            self.gfake.teams["dark"] = [{"id": 1, "name": "agents", "permission": "write",
                                         "includes_all_repositories": True, "members": ["dark-agent"]}]
        self.assertEqual(self.misses(checks), [])

    def test_a_team_that_does_not_cover_repos_made_later_is_refused(self):
        # work repos are created per run: a team without
        # includes_all_repositories covers today's repos and not tomorrow's
        self.gfake.teams["dark"] = [{"id": 3, "name": "agents", "permission": "write",
                                     "includes_all_repositories": False, "members": ["dark-agent"]}]
        try:
            checks = self.pre().run(need_vm=False)
        finally:
            self.gfake.teams["dark"] = [{"id": 1, "name": "agents", "permission": "write",
                                         "includes_all_repositories": True, "members": ["dark-agent"]}]
        self.assertIn("org dark writable", self.misses(checks))

    def test_templates_missing(self):
        os.remove(os.path.join(self.host.templates_dir, "go", ".dark", "verify.sh"))
        checks = self.pre().run(need_vm=False)
        t = [c for c in checks if c.name == "templates"][0]
        self.assertFalse(t.ok)
        self.assertIn("go", t.detail)

    def test_wake_succeeds(self):
        px = FakePx(reachable=False)
        pre = self.pre(px)
        original = self.chat

        def chat(*a, **kw):
            px.up = True
            return original(*a, **kw)
        pre.chat = chat
        checks = pre.run()
        self.assertEqual(self.misses(checks), [])
        call = self.led.last("call")
        self.assertEqual((call["purpose"], call["tier"], call["ok"], call["cls"]), ("wake", "local-a", True, "probe"))

    def test_wake_fails(self):
        pre = self.pre(FakePx(reachable=False))

        def chat(*a, **kw):
            raise llm.LLMError("timeout", "no reply")
        pre.chat = chat
        checks = pre.run()
        self.assertIn("proxmox", self.misses(checks))
        self.assertFalse(self.led.last("call")["ok"])

    def test_template_missing(self):
        checks = self.pre(FakePx(template=(False, "VM 9001 exists but is not a template"))).run()
        self.assertIn("vm template", self.misses(checks))

    def test_docker_preflight_names_the_image(self):
        self.host.backend = "docker"
        self.host.sandbox_image = "sandbox-img"
        checks = self.pre().run()
        by = {c.name: c for c in checks}
        self.assertIn("docker", by)
        self.assertIn("sandbox-img", by["docker"].detail)
        self.assertIn("sandbox image", by)
        self.assertIn("sandbox-img", by["sandbox image"].detail)
        self.assertNotIn("vm template", by)

    def test_lxc_preflight_reports_the_container_snapshot(self):
        self.host.backend = "lxc"
        self.host.sandbox_container = "200"
        self.host.sandbox_snapshot = "base"
        by = {c.name: c for c in self.pre().run()}
        self.assertIn("proxmox", by)
        self.assertIn("container snapshot", by)
        self.assertIn("200", by["container snapshot"].detail)
        self.assertNotIn("vm template", by)
        by = {c.name: c for c in self.pre(FakePx(template=(False, "container 200 has no snapshot 'base'"))).run()}
        self.assertFalse(by["container snapshot"].ok)

    def test_no_admitted_tier(self):
        for i in range(3):
            self.led.emit("run.end", **run_end(run=f"m{i}", cls="mechanical", tier="local-a", outcome="fail:capability"))
        checks = self.pre().run(need_vm=False)
        self.assertIn("admission mechanical", self.misses(checks))


if __name__ == "__main__":
    unittest.main()

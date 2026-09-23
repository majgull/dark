import json
import unittest

from dark import gitea as G
from dark import vm
from tests import fakes


class FakeSSH:
    def __init__(self):
        self.cmds = []
        self.status = {}      # vmid -> "running"/"stopped"
        self.template = {9001: "template: 1\nname: t\n"}
        self.stdin = {}

    def __call__(self, cmd, stdin=None, timeout=120):
        self.cmds.append(cmd)
        if stdin is not None:
            self.stdin[cmd] = stdin
        if cmd == "qm list":
            return 0, "VMID NAME\n", ""
        if cmd.startswith("qm config "):
            vmid = int(cmd.split()[2])
            return (0, self.template[vmid], "") if vmid in self.template else (2, "", "does not exist")
        if cmd.startswith("qm status "):
            vmid = int(cmd.split()[2])
            return (0, f"status: {self.status[vmid]}\n", "") if vmid in self.status else (2, "", "no such vm")
        if cmd.startswith("qm clone "):
            self.status[int(cmd.split()[3])] = "stopped"
            return 0, "", ""
        if cmd.startswith("qm start "):
            self.status[int(cmd.split()[2])] = "running"
            return 0, "", ""
        if cmd.startswith("qm stop "):
            vmid = int(cmd.split()[2])
            if vmid in self.status:
                self.status[vmid] = "stopped"
            return 0, "", ""
        if cmd.startswith("qm destroy "):
            self.status.pop(int(cmd.split()[2]), None)
            return 0, "", ""
        return 0, "", ""


class VM(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.px = vm.Proxmox("cpu-host", 9001, ssh=self.ssh, sleep=lambda s: None)

    def test_template_ok(self):
        self.assertEqual(self.px.template_ok(), (True, ""))
        self.assertFalse(vm.Proxmox("h", 9002, ssh=self.ssh).template_ok()[0])
        self.ssh.template[9003] = "name: x\n"
        ok, why = vm.Proxmox("h", 9003, ssh=self.ssh).template_ok()
        self.assertFalse(ok)
        self.assertIn("not a template", why)

    def test_spawn_sequence_and_firewall(self):
        self.px.spawn(9500, "dark-x1", "#cloud-config\n")
        cmds = self.ssh.cmds
        self.assertIn("cat > /var/lib/vz/snippets/dark-x1.yaml", cmds)
        self.assertEqual(self.ssh.stdin["cat > /var/lib/vz/snippets/dark-x1.yaml"], "#cloud-config\n")
        self.assertIn("qm clone 9001 9500 --name dark-x1", cmds)
        self.assertIn("qm set 9500 --cicustom user=local:snippets/dark-x1.yaml,network=local:snippets/dark-x1-net.yaml", cmds)
        # a fixed MAC per VM id, the template's other net0 options kept
        self.assertIn("qm set 9500 --net0 virtio=BC:24:11:DA:25:1C,bridge=vmbr0,firewall=1", cmds)
        # one DHCP identity per clone (three VMs shared 192.0.2.199 on 2026-09-02)
        self.assertIn("dhcp-identifier: mac", self.ssh.stdin["cat > /var/lib/vz/snippets/dark-x1-net.yaml"])
        self.assertIn("systemd-machine-id-setup", vm.user_data("dark-x1", {}, []))
        self.assertIn("GROUP agentfw", self.ssh.stdin["cat > /etc/pve/firewall/9500.fw"])
        self.assertEqual(cmds[-1], "qm start 9500")
        self.assertEqual(self.px.status(9500), "running")

    def test_fixed_mac_keeps_template_net_options(self):
        self.ssh.template[9001] = "template: 1\nnet0: virtio=BC:24:11:8D:50:5B,bridge=vmbr1,firewall=1\n"
        self.px.spawn(9551, "dark-s1", "#cloud-config\n")
        self.assertIn("qm set 9551 --net0 virtio=BC:24:11:DA:25:4F,bridge=vmbr1,firewall=1", self.ssh.cmds)

    def test_spawn_reaps_a_leftover_first(self):
        self.ssh.status[9500] = "running"
        self.px.spawn(9500, "dark-x1", "x")
        self.assertTrue(any(c.startswith("qm destroy 9500") for c in self.ssh.cmds))
        self.assertEqual(self.px.status(9500), "running")  # the new one

    def test_reap(self):
        self.ssh.status[9500] = "running"
        self.assertTrue(self.px.reap(9500, "dark-x1"))
        self.assertIsNone(self.px.status(9500))
        self.assertIn("rm -f /var/lib/vz/snippets/dark-x1.yaml /var/lib/vz/snippets/dark-x1-net.yaml "
                      "/etc/pve/firewall/9500.fw", self.ssh.cmds)

    def test_reap_reports_survivor(self):
        class Sticky(FakeSSH):
            def __call__(self, cmd, stdin=None, timeout=120):
                if cmd.startswith("qm destroy "):
                    return 1, "", "locked"
                return super().__call__(cmd, stdin, timeout)
        s = Sticky()
        s.status[9500] = "running"
        self.assertFalse(vm.Proxmox("h", 9001, ssh=s, sleep=lambda x: None).reap(9500, "n"))

    def test_guest_ip(self):
        answers = {}

        def agent(cmd, stdin=None, timeout=120):
            if cmd.startswith("qm guest cmd "):
                return answers.get(cmd.split()[3], (1, "", "QEMU guest agent is not running"))
            return 0, "", ""
        px = vm.Proxmox("h", 9001, ssh=agent)
        self.assertIsNone(px.guest_ip(9500))          # agent not up yet
        answers["9500"] = (0, json.dumps([
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                                              {"ip-address": "192.0.2.149", "ip-address-type": "ipv4"}]}]), "")
        self.assertEqual(px.guest_ip(9500), "192.0.2.149")
        answers["9500"] = (0, json.dumps([{"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]}]), "")
        self.assertIsNone(px.guest_ip(9500))          # booted, no DHCP address
        answers["9500"] = (0, "not json", "")
        self.assertIsNone(px.guest_ip(9500))

    def test_ssh_failure_raises(self):
        def bad(cmd, stdin=None, timeout=120):
            return 255, "", "connection refused"
        px = vm.Proxmox("h", 9001, ssh=bad)
        self.assertFalse(px.reachable())
        with self.assertRaises(vm.VMError):
            px.ssh("qm start 1")

    def test_user_data_shape(self):
        doc = vm.user_data("dark-x1", {"/opt/task.json": ('{"a": 1}', "0600"),
                                       "/opt/agent.py": ("print('x')\n  y\n", "0755")},
                           [["bash", "-lc", "python3 /opt/agent.py"]])
        self.assertTrue(doc.startswith("#cloud-config\nhostname: dark-x1\n"))
        self.assertIn("  - path: /opt/task.json\n    permissions: \"0600\"\n    content: |\n      {\"a\": 1}\n", doc)
        self.assertIn("      print('x')\n        y\n", doc)
        self.assertIn('runcmd:\n  - ["bash", "-lc", "python3 /opt/agent.py"]\n', doc)


class Gitea(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = fakes.FakeGitea()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def setUp(self):
        self.g = G.Gitea(self.fake.url, "good", lan_url="http://lan:3400")

    def test_health_and_auth(self):
        self.assertEqual(self.g.version(), "fake-1.26")
        self.assertEqual(self.g.whoami(), "factory-admin")
        with self.assertRaises(G.GiteaError) as cm:
            G.Gitea(self.fake.url, "bad").whoami()
        self.assertEqual(cm.exception.code, 401)

    def test_conn_error_is_code_zero(self):
        with self.assertRaises(G.GiteaError) as cm:
            G.Gitea("http://127.0.0.1:1", "x").version()
        self.assertEqual(cm.exception.code, 0)

    def test_orgs_repos_branches(self):
        self.assertFalse(self.g.org_exists("dark"))
        self.g.create_org("dark")
        self.assertTrue(self.g.org_exists("dark"))
        self.assertFalse(self.g.repo_exists("dark/runner"))
        self.g.create_repo("dark", "runner", description="d")
        self.assertTrue(self.g.repo_exists("dark/runner"))
        self.assertFalse(self.g.branch_exists("dark/runner", "run/r1"))
        self.fake.repos["dark/runner"]["branches"].add("run/r1")
        self.assertTrue(self.g.branch_exists("dark/runner", "run/r1"))
        self.g.delete_branch("dark/runner", "run/r1")
        self.assertFalse(self.g.branch_exists("dark/runner", "run/r1"))
        self.g.delete_branch("dark/runner", "run/r1")  # idempotent
        self.g.archive_repo("dark/runner")
        self.assertTrue(self.fake.repos["dark/runner"]["archived"])
        self.assertEqual(self.g.push_url("dark/runner"), f"http://factory-admin:good@127.0.0.1:{self.fake.server.port}/dark/runner.git")

    def test_issues_and_comments(self):
        self.fake.repos["dark/t"] = {"archived": False, "branches": set()}
        n = self.g.issue_create("dark/t", "run r1", "spec")
        self.assertEqual(n, 1)
        cid = self.g.comment("dark/t", n, "AGENT-ALIVE")
        self.g.edit_comment("dark/t", cid, "AGENT-ALIVE\nbeat")
        for i in range(60):
            self.g.comment("dark/t", n, f"c{i}")
        cs = self.g.comments("dark/t", n)
        self.assertEqual(len(cs), 61)  # paged through 50
        self.assertEqual(cs[0]["body"], "AGENT-ALIVE\nbeat")
        self.assertGreater(G.parse_time(cs[0]["updated_at"]), 0)
        self.assertEqual([i["number"] for i in self.g.open_issues("dark/t")], [1])
        self.g.issue_close("dark/t", n, comment="superseded")
        self.assertEqual(self.g.open_issues("dark/t"), [])
        self.assertEqual(self.fake.issues["dark/t"][1]["state"], "closed")

    def test_parse_time(self):
        self.assertEqual(G.parse_time("1970-01-01T00:00:10Z"), 10.0)
        self.assertEqual(G.parse_time("1970-01-01T01:00:10+01:00"), 10.0)

    def test_read_token(self):
        import os
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "tok")
        self.assertEqual(G.read_token(p), "")
        with open(p, "w") as f:
            f.write("  abc  \n")
        self.assertEqual(G.read_token(p), "abc")


if __name__ == "__main__":
    unittest.main()


class TransferAndArchive(unittest.TestCase):
    def test_work_repos_move_and_archive(self):
        from dark import gitea as G
        fake = fakes.FakeGitea()
        try:
            fake.orgs.add("dark")
            for n in ("runner", "t-a", "tl-b", "session-c"):
                fake.repos[f"dark/{n}"] = {"archived": False, "branches": {"main"}}
            fake.issues["dark/t-a"] = {1: {"title": "x", "body": "", "state": "open", "comments": []}}
            g = G.Gitea(fake.url, "good")
            self.assertEqual(sorted(g.repos_of_org("dark")), ["runner", "session-c", "t-a", "tl-b"])
            full = g.transfer_repo("dark/t-a", "dark-archive")
            g.archive_repo(full)
            self.assertEqual(full, "dark-archive/t-a")
            self.assertNotIn("dark/t-a", fake.repos)
            self.assertTrue(fake.repos["dark-archive/t-a"]["archived"])
            self.assertIn(1, fake.issues["dark-archive/t-a"])
            self.assertIn("dark/runner", fake.repos)
        finally:
            fake.close()

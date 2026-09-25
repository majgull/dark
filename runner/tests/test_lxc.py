"""The lxc backend, against a fake ssh recorder: the exact `pct` sequence for
spawn and reap, the firewall file, the refusal of a missing source or
snapshot, and a reap that reports a survivor. Nothing here runs `pct`."""

import inspect
import unittest

from dark import lxc, sandbox, vm

LISTING = ("`-> base-snap                   2026-01-01 10:00:00     no-description\n"
           " `-> current                    You are here!\n")


class FakeSSH:
    """Records every command and its stdin; answers like `pct` for a source
    container 200 carrying the snapshot base-snap."""

    def __init__(self):
        self.cmds = []
        self.stdin = {}
        self.cts = {200: "stopped"}     # vmid -> status
        self.snapshots = {200: LISTING}
        self.ips = {}
        self.destroy_leaves = False     # a destroy that does not remove

    def __call__(self, cmd, stdin=None, timeout=120):
        self.cmds.append(cmd)
        if stdin is not None:
            self.stdin[cmd] = stdin
        w = cmd.split()
        if cmd == "pct list":
            return 0, "VMID Status Lock Name\n", ""
        if w[:2] in (["pct", "config"], ["pct", "listsnapshot"], ["pct", "status"]):
            vmid = int(w[2])
            if vmid not in self.cts:
                return 2, "", f"Configuration file 'nodes/n/lxc/{vmid}.conf' does not exist\n"
            if w[1] == "config":
                return 0, "hostname: src\n", ""
            if w[1] == "listsnapshot":
                return 0, self.snapshots.get(vmid, " `-> current  You are here!\n"), ""
            return 0, f"status: {self.cts[vmid]}\n", ""
        if w[:2] == ["pct", "clone"]:
            self.cts[int(w[3])] = "stopped"
        elif w[:2] == ["pct", "start"]:
            self.cts[int(w[2])] = "running"
        elif w[:2] == ["pct", "stop"] and int(w[2]) in self.cts:
            self.cts[int(w[2])] = "stopped"
        elif w[:2] == ["pct", "destroy"] and not self.destroy_leaves:
            self.cts.pop(int(w[2]), None)
        elif w[:2] == ["pct", "exec"] and w[4:] == ["hostname", "-I"]:
            vmid = int(w[2])
            if self.cts.get(vmid) != "running":
                return 1, "", "container not running\n"
            return 0, self.ips.get(vmid, "") + "\n", ""
        return 0, "", ""


class Lxc(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.ct = lxc.Lxc("cpu-host", 200, "base-snap", bridge="vmbr9", ssh=self.ssh)

    def test_matches_the_sandbox_protocol(self):
        for name in ("reachable", "template_ok", "spawn", "guest_ip", "reap", "snapshot", "rollback"):
            with self.subTest(method=name):
                want = inspect.signature(getattr(sandbox.Sandbox, name))
                have = inspect.signature(getattr(lxc.Lxc, name))
                self.assertEqual(list(want.parameters), list(have.parameters))

    def test_reachable(self):
        self.assertTrue(self.ct.reachable())
        self.assertEqual(self.ssh.cmds, ["pct list"])
        self.assertFalse(lxc.Lxc("h", 200, "s", ssh=lambda c, i=None, t=120: (255, "", "refused")).reachable())

    def test_template_ok(self):
        self.assertEqual(self.ct.template_ok(), (True, ""))
        self.assertEqual(self.ssh.cmds, ["pct config 200", "pct listsnapshot 200"])

    def test_refuses_a_missing_source(self):
        ok, why = lxc.Lxc("h", 201, "base-snap", ssh=self.ssh).template_ok()
        self.assertFalse(ok)
        self.assertIn("201", why)
        self.assertNotIn("pct listsnapshot 201", self.ssh.cmds)

    def test_refuses_a_missing_snapshot(self):
        ok, why = lxc.Lxc("h", 200, "other-snap", ssh=self.ssh).template_ok()
        self.assertFalse(ok)
        self.assertIn("other-snap", why)
        # "current" is the listing's marker, never a snapshot to clone from
        self.assertFalse(lxc.Lxc("h", 200, "current", ssh=self.ssh).template_ok()[0])

    def test_refuses_an_unconfigured_source(self):
        ok, why = lxc.Lxc("h", "", "", ssh=self.ssh).template_ok()
        self.assertFalse(ok)
        self.assertIn("sandbox_container", why)
        self.assertEqual(self.ssh.cmds, [])

    def test_snapshot_names(self):
        self.assertEqual(lxc.Lxc.snapshot_names(
            "`-> a  2026-01-01 no-description\n  `-> b  2026-01-02 x\n   `-> current  You are here!\n"),
            ["a", "b"])

    def test_spawn_sequence(self):
        files = {"/opt/task.json": ('{"a": 1}', "0600"),
                 "/opt/work/src/m.py": ("x = 1\n", "0644")}
        self.ct.spawn(9500, "dark-x1", files, [["bash", "-lc", "python3 /opt/agent.py"]])
        self.assertEqual(self.ssh.cmds, [
            "pct status 9500",
            "pct clone 200 9500 --snapname base-snap --full 1 --hostname dark-x1",
            "pct set 9500 --net0 name=eth0,bridge=vmbr9,firewall=1,ip=dhcp",
            "cat > /etc/pve/firewall/9500.fw",
            "pct start 9500",
            "cat > /tmp/dark-push-9500",
            "pct exec 9500 -- mkdir -p /opt",
            "pct push 9500 /tmp/dark-push-9500 /opt/task.json --perms 0600",
            "rm -f /tmp/dark-push-9500",
            "cat > /tmp/dark-push-9500",
            "pct exec 9500 -- mkdir -p /opt/work/src",
            "pct push 9500 /tmp/dark-push-9500 /opt/work/src/m.py --perms 0644",
            "rm -f /tmp/dark-push-9500",
            "cat > /tmp/dark-push-9500",
            "pct exec 9500 -- mkdir -p /opt",
            "pct push 9500 /tmp/dark-push-9500 /opt/dark-start.sh --perms 0755",
            "rm -f /tmp/dark-push-9500",
            "pct exec 9500 -- /bin/sh /opt/dark-start.sh </dev/null >/dev/null 2>&1 &",
        ])
        self.assertEqual(self.ct.status(9500), "running")

    def test_spawn_pushes_contents_and_the_start_script(self):
        pushed = []

        def ssh(cmd, stdin=None, timeout=120):
            if cmd == "cat > /tmp/dark-push-9500":
                pushed.append(stdin)
            return self.ssh(cmd, stdin, timeout)
        ct = lxc.Lxc("h", 200, "base-snap", ssh=ssh)
        ct.spawn(9500, "dark-x1", {"/opt/task.json": ('{"a": 1}', "0600")},
                 [["bash", "-lc", "one"], ["python3", "/opt/two.py"]])
        self.assertEqual(pushed, ['{"a": 1}', "#!/bin/sh\nset -e\nbash -lc one\npython3 /opt/two.py\n"])

    def test_firewall_file(self):
        self.ct.spawn(9500, "dark-x1", {}, [])
        fw = self.ssh.stdin["cat > /etc/pve/firewall/9500.fw"]
        self.assertEqual(fw, vm.FIREWALL)
        self.assertEqual(fw, "[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: DROP\n"
                             "log_level_in: info\nlog_level_out: info\n\n[RULES]\nGROUP agentfw\n")

    def test_spawn_raises_when_a_step_fails(self):
        def ssh(cmd, stdin=None, timeout=120):
            if cmd.startswith("pct clone "):
                return 255, "", "snapshot feature is not available"
            return self.ssh(cmd, stdin, timeout)
        with self.assertRaises(vm.VMError):
            lxc.Lxc("h", 200, "base-snap", ssh=ssh).spawn(9500, "dark-x1", {}, [])

    def test_spawn_reaps_a_leftover_first(self):
        self.ssh.cts[9500] = "running"
        self.ct.spawn(9500, "dark-x1", {}, [])
        self.assertIn("pct destroy 9500 --purge", self.ssh.cmds)
        self.assertLess(self.ssh.cmds.index("pct destroy 9500 --purge"),
                        self.ssh.cmds.index("pct clone 200 9500 --snapname base-snap --full 1 --hostname dark-x1"))
        self.assertEqual(self.ct.status(9500), "running")

    def test_reap_sequence(self):
        self.ssh.cts[9500] = "running"
        self.assertTrue(self.ct.reap(9500, "dark-x1"))
        self.assertEqual(self.ssh.cmds, [
            "pct stop 9500",
            "pct destroy 9500 --purge",
            "rm -f /etc/pve/firewall/9500.fw",
            "pct status 9500",
        ])

    def test_reap_reports_a_survivor(self):
        self.ssh.cts[9500] = "running"
        self.ssh.destroy_leaves = True
        self.assertFalse(self.ct.reap(9500, "dark-x1"))

    def test_reap_needs_does_not_exist(self):
        # a status check that fails for another reason is not proof of absence
        def ssh(cmd, stdin=None, timeout=120):
            if cmd.startswith("pct status "):
                return 255, "", "ssh: connection refused"
            return 0, "", ""
        self.assertFalse(lxc.Lxc("h", 200, "base-snap", ssh=ssh).reap(9500, "dark-x1"))

    def test_snapshot_and_rollback(self):
        self.ct.snapshot(9500, "pre-change")
        self.ct.rollback(9500, "pre-change")
        self.assertEqual(self.ssh.cmds, ["pct snapshot 9500 pre-change", "pct rollback 9500 pre-change"])

        def bad(cmd, stdin=None, timeout=120):
            return 2, "", "snapshot 'pre-change' does not exist"
        with self.assertRaises(vm.VMError):
            lxc.Lxc("h", 200, "base-snap", ssh=bad).rollback(9500, "pre-change")

    def test_guest_ip(self):
        self.assertIsNone(self.ct.guest_ip(9500))                  # no container
        self.ssh.cts[9500] = "running"
        self.assertIsNone(self.ct.guest_ip(9500))                  # no address yet
        self.ssh.ips[9500] = "127.0.0.1 fe80::1 192.0.2.149 192.0.2.150"
        self.assertEqual(self.ct.guest_ip(9500), "192.0.2.149")
        self.assertIn("pct exec 9500 -- hostname -I", self.ssh.cmds)


if __name__ == "__main__":
    unittest.main()

"""target-net.sh against fake docker, iptables and sudo executables.

The script is run as a subprocess with PATH pointing at fakes under /tmp/fx/.
Each fake appends its argv, tab separated, to a log and answers from a state
directory of files, so a test reads back exactly what the script asked the
CLIs to do and what the chain held afterwards. The real docker and iptables
are never called.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ops", "target-net.sh")
FX = "/tmp/fx"
NET = "dark_target"
SUBNET = "172.30.41.0/24"
HOSTPORT = "192.168.1.203:631"
TEMPLATE = "{{range .IPAM.Config}}{{.Subnet}}{{end}}"

# Every fake logs one call per line: argv[0], then a tab and each later argv.
LOGGER = ('{ printf \'%s\' "${1:-}"; for a in "${@:2}"; do printf \'\\t%s\' "$a";'
          ' done; printf \'\\n\'; } >> "$FX_@@LOG@@_LOG"')


def logger(name):
    return LOGGER.replace("@@LOG@@", name)


FAKE_DOCKER = r'''#!/bin/bash
# a fake docker: one log line per argv, network state in $FX_STATE/net/<name>
''' + logger("DOCKER") + r'''
case "${1:-} ${2:-}" in
  "network inspect")
    name=${!#}
    [ -f "$FX_STATE/net/$name" ] || exit 1
    cat "$FX_STATE/net/$name"
    exit 0 ;;
  "network create")
    name=${!#} subnet="" prev=""
    for a in "$@"; do
      if [ "$prev" = "--subnet" ]; then subnet=$a; fi
      prev=$a
    done
    mkdir -p "$FX_STATE/net"
    printf '%s\n' "$subnet" > "$FX_STATE/net/$name"
    exit 0 ;;
  "network rm"|"network remove")
    rm -f "$FX_STATE/net/${!#}"
    exit 0 ;;
esac
exit 0
'''

FAKE_IPTABLES = r'''#!/bin/bash
# a fake iptables: one log line per argv, one chain kept as -A lines in
# $FX_STATE/chain/<name>
''' + logger("IPT") + r'''
op=${1:-} chain=${2:-} f="$FX_STATE/chain/$chain"
case "$op" in
  -S)
    [ -f "$f" ] || exit 1
    cat "$f"
    exit 0 ;;
  -I)
    pos=$3; shift 3
    mkdir -p "$FX_STATE/chain"
    [ -f "$f" ] || : > "$f"
    { head -n $((pos-1)) "$f"
      printf -- '-A %s' "$chain"; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'
      tail -n +$pos "$f"; } > "$f.new"
    mv "$f.new" "$f"
    exit 0 ;;
  -D)
    shift 2
    line="-A $chain"; for a in "$@"; do line="$line $a"; done
    seen=0
    while IFS= read -r l; do
      if [ "$seen" -eq 0 ] && [ "$l" = "$line" ]; then seen=1; continue; fi
      printf '%s\n' "$l"
    done < "$f" > "$f.new"
    mv "$f.new" "$f"
    exit 0 ;;
esac
exit 0
'''

FAKE_SUDO = r'''#!/bin/bash
# a fake sudo: log the call, then run it with this PATH so iptables is the fake
''' + logger("SUDO") + r'''
exec "$@"
'''


class TargetNet(unittest.TestCase):
    def setUp(self):
        os.makedirs(FX, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="target-net-", dir=FX)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(os.path.join(self.state, "chain"))
        os.makedirs(os.path.join(self.state, "net"))
        self.docker_log = os.path.join(self.dir, "docker.log")
        self.ipt_log = os.path.join(self.dir, "iptables.log")
        self.sudo_log = os.path.join(self.dir, "sudo.log")
        self.fake("docker", FAKE_DOCKER)
        self.fake("iptables", FAKE_IPTABLES)
        self.fake("sudo", FAKE_SUDO)
        self.chain_file = os.path.join(self.state, "chain", "DOCKER-USER")
        self.write_chain(["-A DOCKER-USER -j RETURN"])

    # --- the fixture -------------------------------------------------------

    def fake(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def env(self):
        e = dict(os.environ)
        e["PATH"] = self.dir + ":/usr/bin:/bin"
        e["FX_DOCKER_LOG"] = self.docker_log
        e["FX_IPT_LOG"] = self.ipt_log
        e["FX_SUDO_LOG"] = self.sudo_log
        e["FX_STATE"] = self.state
        return e

    def write_chain(self, lines):
        with open(self.chain_file, "w") as f:
            f.write("\n".join(lines) + "\n")

    def read_chain(self):
        with open(self.chain_file) as f:
            return [line for line in f.read().splitlines() if line]

    def net_exists(self):
        return os.path.exists(os.path.join(self.state, "net", NET))

    def calls(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line.rstrip("\n").split("\t") for line in f if line.strip()]

    def clear(self):
        for path in (self.docker_log, self.ipt_log, self.sudo_log):
            if os.path.exists(path):
                os.remove(path)

    # --- running -----------------------------------------------------------

    def run_script(self, *args):
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True,
                              text=True, env=self.env())

    def run_ok(self, *args):
        r = self.run_script(*args)
        self.assertEqual(r.returncode, 0, "%r: rc=%d\n%s" % (args, r.returncode, r.stderr))
        return r

    def run_fail(self, *args):
        r = self.run_script(*args)
        self.assertNotEqual(r.returncode, 0, "%r: expected a refusal\n%s" % (args, r.stdout))
        return r

    def assert_iptables(self, expected):
        # each expected call is the argv after the executable name. The fake
        # iptables logs its own argv, so that is the command verbatim; the
        # fake sudo logs the argv it was handed, which starts with iptables.
        self.assertEqual(self.calls(self.ipt_log), expected, "iptables calls")
        if os.geteuid() == 0:
            self.assertEqual(self.calls(self.sudo_log), [], "sudo must not be used as root")
        else:
            self.assertEqual(self.calls(self.sudo_log),
                             [["iptables", *c] for c in expected], "sudo calls")

    def ipt(self, *args):
        return list(args)

    # --- the tests ---------------------------------------------------------

    def test_first_apply_creates_the_network_and_the_rules(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.assertEqual(self.calls(self.docker_log), [
            ["network", "inspect", "-f", TEMPLATE, NET],
            ["network", "create", "--driver", "bridge", "--subnet", SUBNET, NET],
        ])
        self.assert_iptables([
            self.ipt("-S", "DOCKER-USER"),
            self.ipt("-I", "DOCKER-USER", "1", "-s", SUBNET, "-j", "DROP"),
            self.ipt("-I", "DOCKER-USER", "1", "-s", SUBNET, "-d", "192.168.1.203/32",
                     "-p", "tcp", "--dport", "631", "-j", "ACCEPT"),
        ])
        self.assertEqual(self.read_chain(), [
            "-A DOCKER-USER -s %s -d 192.168.1.203/32 -p tcp --dport 631 -j ACCEPT" % SUBNET,
            "-A DOCKER-USER -s %s -j DROP" % SUBNET,
            "-A DOCKER-USER -j RETURN",
        ])
        self.assertTrue(self.net_exists())

    def test_second_apply_changes_nothing(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        before = self.read_chain()
        self.clear()
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.assertEqual(self.calls(self.docker_log),
                         [["network", "inspect", "-f", TEMPLATE, NET]])
        self.assert_iptables([self.ipt("-S", "DOCKER-USER")])
        self.assertEqual(self.read_chain(), before)

    def test_apply_takes_out_an_accept_no_longer_named(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT, "10.0.0.5:8080")
        self.clear()
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.assertEqual(self.calls(self.docker_log),
                         [["network", "inspect", "-f", TEMPLATE, NET]])
        self.assert_iptables([
            self.ipt("-S", "DOCKER-USER"),
            self.ipt("-D", "DOCKER-USER", "-s", SUBNET, "-d", "192.168.1.203/32",
                     "-p", "tcp", "--dport", "631", "-j", "ACCEPT"),
            self.ipt("-D", "DOCKER-USER", "-s", SUBNET, "-d", "10.0.0.5/32",
                     "-p", "tcp", "--dport", "8080", "-j", "ACCEPT"),
            self.ipt("-D", "DOCKER-USER", "-s", SUBNET, "-j", "DROP"),
            self.ipt("-I", "DOCKER-USER", "1", "-s", SUBNET, "-j", "DROP"),
            self.ipt("-I", "DOCKER-USER", "1", "-s", SUBNET, "-d", "192.168.1.203/32",
                     "-p", "tcp", "--dport", "631", "-j", "ACCEPT"),
        ])
        self.assertEqual(self.read_chain(), [
            "-A DOCKER-USER -s %s -d 192.168.1.203/32 -p tcp --dport 631 -j ACCEPT" % SUBNET,
            "-A DOCKER-USER -s %s -j DROP" % SUBNET,
            "-A DOCKER-USER -j RETURN",
        ])

    def test_apply_leaves_foreign_rules_alone(self):
        self.write_chain([
            "-A DOCKER-USER -s 10.9.9.0/24 -j DROP",
            "-A DOCKER-USER -j RETURN",
        ])
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.assertEqual(self.read_chain(), [
            "-A DOCKER-USER -s %s -d 192.168.1.203/32 -p tcp --dport 631 -j ACCEPT" % SUBNET,
            "-A DOCKER-USER -s %s -j DROP" % SUBNET,
            "-A DOCKER-USER -s 10.9.9.0/24 -j DROP",
            "-A DOCKER-USER -j RETURN",
        ])

    def test_apply_reads_through_the_module_iptables_adds(self):
        # what a real `iptables -S` prints for the rule this script inserts
        self.write_chain([
            "-A DOCKER-USER -s %s -d 192.168.1.203/32 -p tcp -m tcp --dport 631 -j ACCEPT" % SUBNET,
            "-A DOCKER-USER -s %s -j DROP" % SUBNET,
            "-A DOCKER-USER -j RETURN",
        ])
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.assert_iptables([self.ipt("-S", "DOCKER-USER")])

    def test_apply_refuses_a_network_with_another_subnet(self):
        with open(os.path.join(self.state, "net", NET), "w") as f:
            f.write("10.0.0.0/24\n")
        self.run_fail("apply", NET, SUBNET, HOSTPORT)
        self.assertEqual(self.calls(self.docker_log),
                         [["network", "inspect", "-f", TEMPLATE, NET]])
        self.assert_iptables([])

    def test_bad_arguments_touch_nothing(self):
        bad = [
            ("apply",),
            ("apply", NET),
            ("apply", NET, SUBNET, "192.168.1.203:0"),
            ("apply", NET, SUBNET, "192.168.1.203:65536"),
            ("apply", NET, SUBNET, "192.168.1.999:631"),
            ("apply", NET, SUBNET, "192.168.1.203"),
            ("apply", NET, SUBNET, "1.2.3.4:80", "1.2.3.4:80"),
            ("apply", NET, "172.30.41.0/33", HOSTPORT),
            ("apply", NET, "172.30.41.0", HOSTPORT),
            ("apply", "bad/name", SUBNET, HOSTPORT),
            ("show", NET, SUBNET, "extra"),
            ("show", "has space", SUBNET),
            ("remove", NET, "not.a.cidr"),
            ("nonsense", NET, SUBNET),
        ]
        for args in bad:
            with self.subTest(args=args):
                self.run_fail(*args)
                self.assertEqual(self.calls(self.docker_log), [], "docker calls")
                self.assertEqual(self.calls(self.ipt_log), [], "iptables calls")
                self.assertEqual(self.calls(self.sudo_log), [], "sudo calls")

    def test_remove_takes_out_the_rules_and_the_network(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.clear()
        self.run_ok("remove", NET, SUBNET)
        self.assertEqual(self.calls(self.docker_log), [
            ["network", "inspect", "-f", TEMPLATE, NET],
            ["network", "rm", NET],
        ])
        self.assert_iptables([
            self.ipt("-S", "DOCKER-USER"),
            self.ipt("-D", "DOCKER-USER", "-s", SUBNET, "-d", "192.168.1.203/32",
                     "-p", "tcp", "--dport", "631", "-j", "ACCEPT"),
            self.ipt("-D", "DOCKER-USER", "-s", SUBNET, "-j", "DROP"),
        ])
        self.assertEqual(self.read_chain(), ["-A DOCKER-USER -j RETURN"])
        self.assertFalse(self.net_exists())

    def test_remove_twice_changes_nothing(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.run_ok("remove", NET, SUBNET)
        self.clear()
        self.run_ok("remove", NET, SUBNET)
        self.assertEqual(self.calls(self.docker_log),
                         [["network", "inspect", "-f", TEMPLATE, NET]])
        self.assert_iptables([self.ipt("-S", "DOCKER-USER")])

    def test_show_prints_the_network_and_the_subnet_lines(self):
        self.run_ok("apply", NET, SUBNET, HOSTPORT)
        self.clear()
        r = self.run_ok("show", NET, SUBNET)
        self.assertIn(SUBNET, r.stdout)
        self.assertIn("192.168.1.203", r.stdout)
        self.assertEqual(self.calls(self.docker_log), [["network", "inspect", NET]])
        self.assert_iptables([self.ipt("-S", "DOCKER-USER")])


if __name__ == "__main__":
    unittest.main()

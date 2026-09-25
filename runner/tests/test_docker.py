"""The docker backend, against a fake `docker` CLI on PATH. The fake records
every argv and keeps a state directory of container names, so the tests read
what the backend actually asked the CLI to do."""

import inspect
import os
import shutil
import tempfile
import unittest
from unittest import mock

from dark import docker as D
from dark import sandbox

FAKE = r'''#!/usr/bin/env bash
# A fake docker CLI: records argv to $FAKE_DOCKER_LOG and keeps one state
# file per container under $FAKE_DOCKER_STATE.
log="${FAKE_DOCKER_LOG:?}"
state="${FAKE_DOCKER_STATE:?}"
mkdir -p "$state"
args=("$@")
{ printf '%s' "${args[0]}"; for a in "${args[@]:1}"; do printf '\t%s' "$a"; done; printf '\n'; } >> "$log"
cmd="${args[0]}"
set -- "${args[@]:1}"
case "$cmd" in
  info) exit "${FAKE_DOCKER_INFO_RC:-0}" ;;
  image)
    [ "$1" = inspect ] && [ "$2" = "${FAKE_DOCKER_IMAGE:?}" ] && exit 0
    exit 1 ;;
  create)
    name=""
    while [ $# -gt 0 ]; do
      case "$1" in --name) name="$2"; shift 2 ;; *) shift ;; esac
    done
    : > "$state/$name"
    exit 0 ;;
  cp)
    mkdir -p "${FAKE_DOCKER_CP:?}"
    cp -a "$1" "$FAKE_DOCKER_CP/"
    exit 0 ;;
  start)
    [ -f "$state/$1" ] && echo running > "$state/$1"
    exit 0 ;;
  inspect)
    if [ "$1" = "-f" ]; then
      name="$3"
      [ -f "$state/$name" ] || exit 1
      grep -q running "$state/$name" && echo "${FAKE_DOCKER_IP:-172.18.0.7}"
      exit 0
    fi
    [ -f "$state/$1" ] || exit 1
    echo "{}"
    exit 0 ;;
  rm)
    name=""
    while [ $# -gt 0 ]; do
      case "$1" in -f) shift ;; *) name="$1"; shift ;; esac
    done
    if [ -f "$state/$name" ]; then rm -f "$state/$name"; exit 0; fi
    echo "No such container: $name" >&2
    exit 1 ;;
esac
exit 0
'''

IMAGE = "dark-sandbox-test"
NET = "dark-test-net"


class FakeCLI:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="fake-docker-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.state = os.path.join(self.dir, "state")
        self.cp = os.path.join(self.dir, "cp")
        self.log = os.path.join(self.dir, "argv.log")
        exe = os.path.join(self.bin, "docker")
        with open(exe, "w") as f:
            f.write(FAKE)
        os.chmod(exe, 0o755)

    def env(self):
        return {"PATH": self.bin + os.pathsep + os.environ["PATH"],
                "FAKE_DOCKER_LOG": self.log, "FAKE_DOCKER_STATE": self.state,
                "FAKE_DOCKER_CP": self.cp, "FAKE_DOCKER_IMAGE": IMAGE}

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [line.split("\t") for line in f.read().splitlines()]

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class DockerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCLI()
        self.addCleanup(self.fake.close)
        patcher = mock.patch.dict(os.environ, self.fake.env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.d = D.Docker(IMAGE, network=NET, cpus="2", memory="2g", pids=512)

    def files(self):
        return {"/opt/task.json": ('{"a": 1}', "0600"),
                "/opt/agent.py": ("print('x')\n", "0755")}

    def runcmd(self):
        return [["bash", "-lc", "python3 /opt/agent.py"]]

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def test_matches_the_sandbox_protocol(self):
        # the protocol's parameters first, in order; anything after them has a
        # default, so a caller of the seam never has to know it is there
        for name in ("reachable", "template_ok", "spawn", "guest_ip", "reap", "snapshot", "rollback"):
            with self.subTest(method=name):
                want = list(inspect.signature(getattr(sandbox.Sandbox, name)).parameters)
                have = inspect.signature(getattr(D.Docker, name)).parameters
                self.assertEqual(list(have)[:len(want)], want)
                for extra in list(have)[len(want):]:
                    self.assertIsNot(have[extra].default, inspect.Parameter.empty, extra)

    def test_spawn_argv(self):
        self.d.spawn(9500, "dark-x1", self.files(), self.runcmd())
        lines = self.fake.lines()
        create = [l for l in lines if l[0] == "create"][0]
        self.assertEqual(create[1:], [
            "--name", "dark-x1", "--network", NET, "--label", "dark.vmid=9500",
            "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
            IMAGE, "/bin/sh", "/opt/dark-start.sh"])
        cp = [l for l in lines if l[0] == "cp"][0]
        self.assertEqual(cp[2], "dark-x1:/")
        self.assertEqual(lines[-1], ["start", "dark-x1"])

    def test_spawn_takes_the_image_per_spawn(self):
        self.d.spawn(9500, "dark-x1", {}, [], image="dark-sandbox-browser")
        self.d.spawn(9501, "dark-x2", {}, [])
        creates = [l for l in self.fake.lines() if l[0] == "create"]
        self.assertEqual(creates[0][-3:], ["dark-sandbox-browser", "/bin/sh", "/opt/dark-start.sh"])
        # no image given: the backend's own, host.sandbox_image
        self.assertEqual(creates[1][-3:], [IMAGE, "/bin/sh", "/opt/dark-start.sh"])

    def test_a_user_sandbox_joins_the_target_network_before_it_starts(self):
        d = D.Docker(IMAGE, network=NET, target_network="dark-target")
        d.spawn(9500, "dark-x1", {}, [], cls="user")
        lines = self.fake.lines()
        self.assertIn(["network", "connect", "dark-target", "dark-x1"], lines)
        self.assertLess(lines.index(["network", "connect", "dark-target", "dark-x1"]),
                        lines.index(["start", "dark-x1"]))
        # created on the internal network like every sandbox; the target is a second one
        create = [l for l in lines if l[0] == "create"][0]
        self.assertEqual(create[create.index("--network") + 1], NET)

    def test_no_other_sandbox_joins_the_target_network(self):
        d = D.Docker(IMAGE, network=NET, target_network="dark-target")
        d.spawn(9500, "dark-x1", {}, [])
        d.spawn(9501, "dark-x2", {}, [], cls="additive")
        # and a user sandbox with no target network configured joins nothing more
        D.Docker(IMAGE, network=NET).spawn(9502, "dark-x3", {}, [], cls="user")
        self.assertFalse([l for l in self.fake.lines() if l[0] == "network"])

    def test_spawn_without_limits_omits_the_flags(self):
        D.Docker(IMAGE, network=NET).spawn(9500, "dark-x1", {}, [])
        create = [l for l in self.fake.lines() if l[0] == "create"][0]
        for flag in ("--cpus", "--memory", "--pids-limit"):
            self.assertNotIn(flag, create)

    def test_spawn_writes_files_and_modes(self):
        self.d.spawn(9500, "dark-x1", self.files(), self.runcmd())
        task = os.path.join(self.fake.cp, "opt", "task.json")
        agent = os.path.join(self.fake.cp, "opt", "agent.py")
        self.assertEqual(self.read(task), '{"a": 1}')
        self.assertEqual(self.read(agent), "print('x')\n")
        self.assertEqual(oct(os.stat(task).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(agent).st_mode & 0o777), "0o755")

    def test_start_script_runs_the_commands_in_order(self):
        self.d.spawn(9500, "dark-x1", {}, [["bash", "-lc", "one"],
                                           ["python3", "/opt/two.py"]])
        body = self.read(os.path.join(self.fake.cp, "opt", "dark-start.sh"))
        self.assertEqual(body, "#!/bin/sh\nset -e\nbash -lc one\npython3 /opt/two.py\n")
        self.assertEqual(oct(os.stat(os.path.join(self.fake.cp, "opt", "dark-start.sh")).st_mode & 0o777),
                         "0o755")

    def test_spawn_reaps_a_leftover_first(self):
        self.d.spawn(9500, "dark-x1", {}, [])
        self.d.spawn(9500, "dark-x1", {}, [])
        lines = self.fake.lines()
        self.assertIn(["rm", "-f", "dark-x1"], lines)
        self.assertEqual(len([l for l in lines if l[0] == "create"]), 2)

    def test_guest_ip_running_and_absent(self):
        self.d.spawn(9500, "dark-x1", {}, [])
        self.assertEqual(self.d.guest_ip(9500), "172.18.0.7")
        self.assertIsNone(self.d.guest_ip(1234))  # no container for this vmid

    def test_guest_ip_none_after_reap(self):
        self.d.spawn(9500, "dark-x1", {}, [])
        self.assertTrue(self.d.reap(9500, "dark-x1"))
        self.assertIsNone(self.d.guest_ip(9500))

    def test_reap_confirms_removal(self):
        self.d.spawn(9500, "dark-x1", {}, [])
        self.assertTrue(self.d.reap(9500, "dark-x1"))
        self.assertIn(["rm", "-f", "dark-x1"], self.fake.lines())
        self.assertEqual(os.listdir(self.fake.state), [])

    def test_reap_of_an_already_absent_container(self):
        self.assertTrue(self.d.reap(9500, "dark-x1"))
        self.assertIn(["rm", "-f", "dark-x1"], self.fake.lines())

    def test_snapshot_and_rollback_are_refused(self):
        for method in (self.d.snapshot, self.d.rollback):
            with self.subTest(method=method.__name__):
                with self.assertRaises(NotImplementedError) as cm:
                    method(9500, "pre-change")
                self.assertEqual(str(cm.exception), "docker sandboxes have no snapshots")
        self.assertEqual(self.fake.lines(), [])  # the CLI is never asked

    def test_reachability_and_template(self):
        self.assertTrue(self.d.reachable())
        self.assertEqual(self.d.template_ok(), (True, ""))
        ok, why = D.Docker("other-image").template_ok()
        self.assertFalse(ok)
        self.assertIn("other-image", why)

    def test_unreachable(self):
        patch = mock.patch.dict(os.environ, {"FAKE_DOCKER_INFO_RC": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertFalse(self.d.reachable())


if __name__ == "__main__":
    unittest.main()

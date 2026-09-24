"""A live docker-backend test: a real daemon, the sandbox image and an
internal test network. Skipped unless `docker info` answers, so the gate
stays green on a host without docker. Run it explicitly:

    python3 -m pytest runner/tests/test_docker_live.py -v
"""

import os
import subprocess
import time
import unittest

from dark import docker as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = os.environ.get("DARK_SANDBOX_IMAGE", "dark-sandbox")
NET = "dark-sandbox-test"


def docker(*args, check=True):
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} rc={r.returncode}: {r.stderr.strip()}")
    return r


def docker_ok():
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def wait(fn, timeout=30, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


@unittest.skipUnless(docker_ok(), "docker daemon not reachable")
class LiveDocker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if docker("image", "inspect", IMAGE, check=False).returncode != 0:
            r = subprocess.run(["docker", "build", "-t", IMAGE, os.path.join(ROOT, "sandbox")],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise unittest.SkipTest(f"cannot build {IMAGE}: {r.stderr.strip()[-300:]}")
        docker("network", "rm", NET, check=False)
        docker("network", "create", "--internal", NET)

    @classmethod
    def tearDownClass(cls):
        docker("network", "rm", NET, check=False)

    def setUp(self):
        self.vmid, self.name = 9500, "dark-live-x0"
        self.d = D.Docker(IMAGE, network=NET, cpus="1", memory="512m", pids=128)
        self.d.reap(self.vmid, self.name)  # a leftover from an interrupted run
        self.addCleanup(self.d.reap, self.vmid, self.name)

    def test_spawn_marker_and_reap(self):
        self.d.spawn(self.vmid, self.name,
                     {"/opt/marker-task.json": ('{"live": true}', "0600")},
                     [["bash", "-lc", "echo live-marker > /opt/marker.txt; sleep 300"]])
        self.assertTrue(wait(lambda: self.d.guest_ip(self.vmid)), "container holds no address")

        def marker():
            r = docker("exec", self.name, "cat", "/opt/marker.txt", check=False)
            return r.stdout if "live-marker" in r.stdout else None

        # the runcmd ran, and the copied file kept its mode
        self.assertIn("live-marker", wait(marker))
        stat = docker("exec", self.name, "stat", "-c", "%a", "/opt/marker-task.json")
        self.assertEqual(stat.stdout.strip(), "600")
        self.assertTrue(self.d.reap(self.vmid, self.name))
        ps = docker("ps", "-a", "--filter", f"label=dark.vmid={self.vmid}", "--format", "{{.Names}}")
        self.assertEqual(ps.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

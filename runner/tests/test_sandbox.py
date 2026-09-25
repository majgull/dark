"""The Sandbox seam: the Proxmox backend matches the protocol, and a backend
this build does not implement is refused at config load and at the factory."""

import inspect
import os
import tempfile
import unittest

from dark import config, lxc, sandbox, vm
from dark import docker as docker_mod

METHODS = ("reachable", "template_ok", "spawn", "guest_ip", "reap", "snapshot", "rollback")


class ProtocolConformance(unittest.TestCase):
    def test_proxmox_has_every_sandbox_method_with_the_same_parameters(self):
        for name in METHODS:
            with self.subTest(method=name):
                want = inspect.signature(getattr(sandbox.Sandbox, name))
                have = inspect.signature(getattr(vm.Proxmox, name))
                self.assertEqual([p for p in want.parameters], [p for p in have.parameters],
                                 f"{name} parameters differ")

    def test_make_builds_proxmox(self):
        px = sandbox.make(config.Host(proxmox="cpu-host"), 9001)
        self.assertIsInstance(px, vm.Proxmox)
        self.assertEqual(px.template, 9001)

    def test_make_builds_docker_from_the_host(self):
        host = config.Host(backend="docker", sandbox_image="sandbox-img", sandbox_network="sandbox-net",
                           sandbox_cpus="2", sandbox_memory="1g", sandbox_pids=256)
        d = sandbox.make(host, 9001)
        self.assertIsInstance(d, docker_mod.Docker)
        self.assertEqual((d.image, d.network, d.cpus, d.memory, d.pids),
                         ("sandbox-img", "sandbox-net", "2", "1g", 256))

    def test_make_builds_lxc_from_the_host(self):
        host = config.Host(backend="lxc", proxmox="cpu-host", sandbox_container="200",
                           sandbox_snapshot="base", sandbox_bridge="vmbr9")
        ct = sandbox.make(host, 9001)
        self.assertIsInstance(ct, lxc.Lxc)
        self.assertEqual((ct.host, ct.source, ct.snapname, ct.bridge),
                         ("cpu-host", "200", "base", "vmbr9"))

    def test_lxc_is_a_backend(self):
        self.assertIn("lxc", config.BACKENDS)
        config.check_backend("lxc")


class BackendRefusal(unittest.TestCase):
    def load_host(self, body, environ=None):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "host.toml")
        with open(p, "w") as f:
            f.write(body)
        return config.load_host(p, environ={} if environ is None else environ)

    def test_default_is_proxmox(self):
        self.assertEqual(config.Host().backend, "proxmox")

    def test_file_value_is_read(self):
        self.assertEqual(self.load_host('[host]\nbackend = "proxmox"\n').backend, "proxmox")

    def test_unknown_backend_is_refused_at_load(self):
        for where, loader in (("file", lambda: self.load_host('[host]\nbackend = "nomad"\n')),
                              ("env", lambda: self.load_host("[host]\n",
                                                             environ={"DARK_BACKEND": "nomad"}))):
            with self.subTest(where=where):
                with self.assertRaises(config.ConfigError) as cm:
                    loader()
                self.assertIn("nomad", str(cm.exception))
                self.assertIn("proxmox", str(cm.exception))
                self.assertIn("docker", str(cm.exception))
                self.assertIn("lxc", str(cm.exception))
                self.assertNotIn("\n", str(cm.exception))

    def test_lxc_is_accepted_at_load(self):
        for where, loader in (("file", lambda: self.load_host('[host]\nbackend = "lxc"\n')),
                              ("env", lambda: self.load_host("[host]\n", environ={"DARK_BACKEND": "lxc"}))):
            with self.subTest(where=where):
                self.assertEqual(loader().backend, "lxc")

    def test_factory_refuses_an_unknown_backend(self):
        host = config.Host()
        host.backend = "nomad"
        with self.assertRaises(config.ConfigError) as cm:
            sandbox.make(host, 9001)
        self.assertIn("nomad", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

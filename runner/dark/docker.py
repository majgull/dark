"""dark/docker.py - throwaway containers over the docker CLI.

One container per executor run and one per staging run, created from the
configured image on an internal network, with the files written in and the
command run as its entrypoint, and removed with force afterwards. Every
docker call is bounded; a reap that leaves the container behind is
reported, never assumed.

The docker CLI is called over subprocess: the package keeps no runtime
dependency on a docker SDK.
"""

import os
import shlex
import shutil
import subprocess
import tempfile

from . import vm

START_SCRIPT = "/opt/dark-start.sh"


class Docker:
    def __init__(self, image, network="dark", cpus=None, memory=None, pids=None,
                 cli="docker", timeout=120):
        self.image = image
        self.network = network
        self.cpus = cpus
        self.memory = memory
        self.pids = pids
        self.cli = cli
        self.timeout = timeout
        self._names = {}  # vmid -> container name, filled by spawn

    def run(self, args, check=True, timeout=None):
        cmd = [self.cli, *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise vm.VMError(f"docker {args!r}: no answer in {timeout or self.timeout}s") from None
        if check and r.returncode != 0:
            raise vm.VMError(f"docker {' '.join(args)} rc={r.returncode}: {r.stderr.strip()[-400:]}")
        return r.returncode, r.stdout, r.stderr

    def reachable(self):
        try:
            return self.run(["info"], check=False, timeout=30)[0] == 0
        except vm.VMError:
            return False

    def template_ok(self):
        rc, _, err = self.run(["image", "inspect", self.image], check=False, timeout=30)
        if rc != 0:
            return False, f"image {self.image}: {err.strip()[-200:] or 'rc ' + str(rc)}"
        return True, ""

    def _exists(self, name):
        return self.run(["inspect", name], check=False, timeout=30)[0] == 0

    @staticmethod
    def start_script(runcmd):
        """A shell script that runs each argv list in order; the container's
        command, so spawn injects nothing that depends on the image's own
        entrypoint."""
        out = ["#!/bin/sh", "set -e"]
        out += [" ".join(shlex.quote(a) for a in cmd) for cmd in runcmd]
        return "\n".join(out) + "\n"

    def spawn(self, vmid, name, files, runcmd, image=None):
        """Create the container, write `files` in (modes honoured), copy in a
        start script built from `runcmd` and start it. A container of the same
        name left over from an earlier run is removed first. `image` is the
        image for this one sandbox (a user-arm run's browser image); None is
        the backend's own, host.sandbox_image."""
        if self._exists(name):
            self.reap(vmid, name)
        self._names[vmid] = name
        tmp = tempfile.mkdtemp(prefix="dark-docker-")
        try:
            for path, (content, mode) in files.items():
                dest = os.path.join(tmp, path.lstrip("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as f:
                    f.write(content)
                os.chmod(dest, int(mode, 8))
            start = os.path.join(tmp, START_SCRIPT.lstrip("/"))
            os.makedirs(os.path.dirname(start), exist_ok=True)
            with open(start, "w") as f:
                f.write(self.start_script(runcmd))
            os.chmod(start, 0o755)
            args = ["create", "--name", name, "--network", self.network,
                    "--label", f"dark.vmid={vmid}"]
            if self.cpus not in (None, ""):
                args += ["--cpus", str(self.cpus)]
            if self.memory not in (None, ""):
                args += ["--memory", str(self.memory)]
            if self.pids:
                args += ["--pids-limit", str(self.pids)]
            args += [image or self.image, "/bin/sh", START_SCRIPT]
            self.run(args, timeout=300)
            self.run(["cp", tmp + "/.", f"{name}:/"], timeout=120)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.run(["start", name], timeout=120)

    def guest_ip(self, vmid):
        """The container's address on its network, or None while it is not
        running or holds none."""
        name = self._names.get(vmid)
        if not name:
            return None
        rc, out, _ = self.run(
            ["inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name],
            check=False, timeout=30)
        if rc != 0:
            return None
        for ip in out.split():
            return ip
        return None

    def snapshot(self, vmid, name):
        raise NotImplementedError("docker sandboxes have no snapshots")

    def rollback(self, vmid, name):
        raise NotImplementedError("docker sandboxes have no snapshots")

    def reap(self, vmid, name):
        """Force-remove the container, then confirm it is gone. True iff gone."""
        self._names.pop(vmid, None)
        self.run(["rm", "-f", name], check=False, timeout=120)
        return not self._exists(name)

"""dark/lxc.py - throwaway Proxmox containers over ssh, cloned from a real one.

One container per executor run and one per staging run, a full clone of a
named snapshot of a source container (the one that runs the real service),
on a configured bridge, fenced by the same default-drop firewall file as a
VM, and destroyed with --purge afterwards. There is no cloud-init: the files
are pushed in with `pct push` and the command runs from a start script, as
in the docker backend. Every ssh call is bounded; a reap that leaves the
container behind is reported, never assumed.
"""

import os
import re
import subprocess

from . import docker, vm

START_SCRIPT = docker.START_SCRIPT

# one host's sandbox_allow_in entry: a dotted IPv4 address and a tcp port
ALLOW_IN = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$")


def allow_in_rules(spec):
    """The firewall lines that open a clone to the clients in `spec`, the
    host's sandbox_allow_in: "<ipv4>:<tcp port>" entries joined with commas.
    One `IN ACCEPT` line per entry, appended to the default-drop file after
    GROUP agentfw, so a user-arm browser on another host can reach a
    rehearsal clone and nothing else. An entry that is not a dotted IPv4
    address and a port 1..65535 raises VMError naming it; "" is no line."""
    lines = []
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        m = ALLOW_IN.match(entry)
        if not m or any(int(o) > 255 for o in m.groups()[:4]) or not 1 <= int(m.group(5)) <= 65535:
            raise vm.VMError(f"sandbox_allow_in {entry!r}: not <ipv4>:<port> "
                             "(a dotted IPv4 address and a tcp port 1..65535)")
        ip = ".".join(str(int(o)) for o in m.groups()[:4])
        lines.append(f"IN ACCEPT -source {ip} -p tcp -dport {int(m.group(5))} -log nolog\n")
    return "".join(lines)


class Lxc:
    def __init__(self, host, source, snapshot, bridge="vmbr0", pool="", allow_in="", ssh=None):
        self.host = host
        self.source = str(source)
        self.snapname = str(snapshot)
        self.bridge = bridge
        # the Proxmox pool the clone is placed in; "" = no pool
        self.pool = pool
        # the clients the clone's firewall lets in, besides the service host
        self.allow_in = allow_in
        self._ssh = ssh or (lambda cmd, stdin=None, timeout=120: vm._ssh(host, cmd, stdin, timeout))

    def ssh(self, cmd, stdin=None, check=True, timeout=120):
        try:
            rc, out, err = self._ssh(cmd, stdin, timeout)
        except subprocess.TimeoutExpired:
            raise vm.VMError(f"ssh {cmd!r}: no answer in {timeout}s") from None
        if check and rc != 0:
            raise vm.VMError(f"ssh {cmd!r} rc={rc}: {err[-400:]}")
        return rc, out, err

    def reachable(self):
        try:
            return self.ssh("pct list", check=False, timeout=30)[0] == 0
        except vm.VMError:
            return False

    @staticmethod
    def snapshot_names(listing):
        """The snapshot names in `pct listsnapshot` output, whose lines read
        "`-> <name> <date> <description>"; the "current" marker is not one."""
        names = []
        for line in listing.splitlines():
            m = re.search(r"->\s*(\S+)", line)
            if m and m.group(1) != "current":
                names.append(m.group(1))
        return names

    def template_ok(self):
        if not self.source or not self.snapname:
            return False, "lxc backend needs sandbox_container and sandbox_snapshot"
        rc, _, err = self.ssh(f"pct config {self.source}", check=False, timeout=30)
        if rc != 0:
            return False, f"pct config {self.source}: {err.strip()[-200:] or 'rc ' + str(rc)}"
        rc, out, err = self.ssh(f"pct listsnapshot {self.source}", check=False, timeout=30)
        if rc != 0:
            return False, f"pct listsnapshot {self.source}: {err.strip()[-200:] or 'rc ' + str(rc)}"
        if self.snapname not in self.snapshot_names(out):
            return False, f"container {self.source} has no snapshot {self.snapname!r}"
        return True, ""

    def status(self, vmid):
        rc, out, _ = self.ssh(f"pct status {vmid}", check=False, timeout=30)
        if rc != 0:
            return None
        return out.strip().split(":", 1)[-1].strip() if ":" in out else out.strip()

    def guest_ip(self, vmid):
        """The first non-loopback IPv4 `hostname -I` reports inside the
        container, or None while it is not running or holds no address."""
        rc, out, _ = self.ssh(f"pct exec {vmid} -- hostname -I", check=False, timeout=30)
        if rc != 0:
            return None
        for ip in out.split():
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) and not ip.startswith("127."):
                return ip
        return None

    def push(self, vmid, path, content, mode):
        """Write `content` to `path` inside the container with `mode`, through
        a file on the Proxmox host that is removed afterwards."""
        tmp = f"/tmp/dark-push-{vmid}"
        self.ssh(f"cat > {tmp}", stdin=content)
        self.ssh(f"pct exec {vmid} -- mkdir -p {os.path.dirname(path)}")
        self.ssh(f"pct push {vmid} {tmp} {path} --perms {mode}")
        self.ssh(f"rm -f {tmp}", check=False)

    def spawn(self, vmid, name, files, runcmd, cls=None):
        """Full-clone the source container from its snapshot, put it on the
        bridge behind the firewall, place it in the pool when one is set,
        clear onboot, start it, push `files` in (modes honoured) and run a
        start script built from `runcmd` in the background. The firewall file
        is the default-drop one plus one IN ACCEPT line per sandbox_allow_in
        entry. A container left over under the same id is reaped first.
        A "user" sandbox is refused before anything is cloned: a clone of a
        service container is not a browser sandbox, and this backend has no
        target rule to let it out to the application."""
        if cls == "user":
            raise vm.VMError("the lxc backend has no user-arm sandbox (no browser image, no target "
                             "rule); run user tasks on the docker or proxmox backend")
        # every entry is validated before anything is cloned: a malformed one
        # must not leave a half-built clone behind
        allow_in = allow_in_rules(self.allow_in)
        if self.status(vmid) is not None:
            self.reap(vmid, name)
        pool = f" --pool {self.pool}" if self.pool else ""
        self.ssh(f"pct clone {self.source} {vmid} --snapname {self.snapname} --full 1 "
                 f"--hostname {name}{pool}", timeout=600)
        self.ssh(f"pct set {vmid} --net0 name=eth0,bridge={self.bridge},firewall=1,ip=dhcp")
        # a full clone inherits the source's onboot, which is usually 1: a
        # throwaway must not come up with the host (found on a real host,
        # 2026-09-25). pct set takes this while the container is stopped.
        self.ssh(f"pct set {vmid} --onboot 0")
        self.ssh(f"cat > /etc/pve/firewall/{vmid}.fw", stdin=vm.FIREWALL + allow_in)
        self.ssh(f"pct start {vmid}", timeout=120)
        for path, (content, mode) in files.items():
            self.push(vmid, path, content, mode)
        self.push(vmid, START_SCRIPT, docker.Docker.start_script(runcmd), "0755")
        # detached on the host, so the ssh call returns while the script runs
        self.ssh(f"pct exec {vmid} -- /bin/sh {START_SCRIPT} </dev/null >/dev/null 2>&1 &")

    def snapshot(self, vmid, name):
        self.ssh(f"pct snapshot {vmid} {name}", timeout=300)

    def rollback(self, vmid, name):
        self.ssh(f"pct rollback {vmid} {name}", timeout=300)

    def reap(self, vmid, name):
        """Stop, destroy, remove the firewall file. True iff `pct status`
        answers that the container does not exist."""
        self.ssh(f"pct stop {vmid}", check=False)
        self.ssh(f"pct destroy {vmid} --purge", check=False, timeout=180)
        self.ssh(f"rm -f /etc/pve/firewall/{vmid}.fw", check=False)
        rc, out, err = self.ssh(f"pct status {vmid}", check=False, timeout=30)
        return rc != 0 and "does not exist" in (out + err)

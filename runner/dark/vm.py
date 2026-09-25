"""dark/vm.py - throwaway Proxmox VMs over ssh.

One VM per executor run and one per staging run, cloned from the template,
configured by a cloud-init snippet that carries the task and the injected
script, fenced by a firewall rule that allows the service host only, and
destroyed with --purge afterwards. Every ssh call is bounded; a reap that
leaves the VM behind is reported, never assumed.
"""

import json
import re
import subprocess
import textwrap
import time


class VMError(Exception):
    pass


def _ssh(host, cmd, stdin=None, timeout=120):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


NETWORK_CONFIG = """version: 2
ethernets:
  lan:
    match:
      name: "e*"
    dhcp4: true
    dhcp-identifier: mac
"""

# The per-guest firewall file: drop both ways, then the agentfw group's
# rules (the service host only). Shared with the lxc backend.
FIREWALL = ("[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: DROP\n"
            "log_level_in: info\nlog_level_out: info\n\n[RULES]\nGROUP agentfw\n")


def user_data(hostname, files, runcmd):
    """A #cloud-config document: `files` is {path: (content, mode)}."""
    ind = lambda s: textwrap.indent(s, "      ")  # noqa: E731
    # Every clone of the template boots with the template's /etc/machine-id,
    # and systemd-networkd derives its DHCP identifier from it, so VMs with
    # distinct MACs can still collide on one address. A fresh machine-id
    # before networking (bootcmd runs in cloud-init's local stage) plus
    # dhcp-identifier: mac in the network config (spawn) make the lease
    # per VM.
    out = ["#cloud-config", f"hostname: {hostname}", "manage_etc_hosts: true",
           "bootcmd:",
           "  - [sh, -c, \"rm -f /etc/machine-id /var/lib/dbus/machine-id && systemd-machine-id-setup\"]",
           "write_files:"]
    for path, (content, mode) in files.items():
        if not content.endswith("\n"):
            content += "\n"
        out.append(f"  - path: {path}\n    permissions: \"{mode}\"\n    content: |\n{ind(content)}")
    out.append("runcmd:")
    for c in runcmd:
        out.append(f"  - {json.dumps(c)}")
    return "\n".join(out) + "\n"


class Proxmox:
    def __init__(self, host, template, ssh=None, sleep=time.sleep):
        self.host = host
        self.template = int(template)
        self._ssh = ssh or (lambda cmd, stdin=None, timeout=120: _ssh(host, cmd, stdin, timeout))
        self.sleep = sleep

    def ssh(self, cmd, stdin=None, check=True, timeout=120):
        try:
            rc, out, err = self._ssh(cmd, stdin, timeout)
        except subprocess.TimeoutExpired:
            raise VMError(f"ssh {cmd!r}: no answer in {timeout}s") from None
        if check and rc != 0:
            raise VMError(f"ssh {cmd!r} rc={rc}: {err[-400:]}")
        return rc, out, err

    def reachable(self):
        try:
            return self.ssh("qm list", check=False, timeout=30)[0] == 0
        except VMError:
            return False

    def template_ok(self):
        rc, out, err = self.ssh(f"qm config {self.template}", check=False, timeout=30)
        if rc != 0:
            return False, f"qm config {self.template}: {err.strip()[-200:] or 'rc ' + str(rc)}"
        if "template: 1" not in out:
            return False, f"VM {self.template} exists but is not a template"
        return True, ""

    def status(self, vmid):
        rc, out, _ = self.ssh(f"qm status {vmid}", check=False, timeout=30)
        if rc != 0:
            return None
        return out.strip().split(":", 1)[-1].strip() if ":" in out else out.strip()

    def guest_ip(self, vmid):
        """The first non-loopback IPv4 the guest agent reports, or None while
        the agent is not up yet or the guest holds no address."""
        rc, out, _ = self.ssh(f"qm guest cmd {vmid} network-get-interfaces", check=False, timeout=30)
        if rc != 0:
            return None
        try:
            ifaces = json.loads(out)
        except ValueError:
            return None
        for i in ifaces:
            for a in i.get("ip-addresses") or []:
                ip = str(a.get("ip-address", ""))
                if a.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                    return ip
        return None

    @staticmethod
    def mac_for(vmid):
        """A fixed MAC per VM id. `qm clone` invents a MAC per clone, and with
        the MAC as DHCP identifier every boot takes a fresh lease, so the
        table fills with stale addresses. One lease per VM id instead."""
        return f"BC:24:11:DA:{vmid >> 8 & 0xFF:02X}:{vmid & 0xFF:02X}"

    def net0_for(self, vmid):
        """The template's net0 options with the fixed MAC swapped in."""
        rc, out, _ = self.ssh(f"qm config {self.template}", check=False, timeout=30)
        m = re.search(r"^net0:\s*(\S+)", out, re.M) if rc == 0 else None
        if m and "virtio=" in m.group(1):
            return re.sub(r"virtio=[0-9A-Fa-f:]+", f"virtio={self.mac_for(vmid)}", m.group(1))
        return f"virtio={self.mac_for(vmid)},bridge=vmbr0,firewall=1"

    def spawn(self, vmid, name, files, runcmd):
        """Clone, configure and start a VM. `files` is {path: (content, mode)}
        and `runcmd` a list of argv lists; the cloud-init document is rendered
        here, so callers of the seam never build one."""
        cloud_config = user_data(name, files, runcmd)
        if self.status(vmid) is not None:
            self.reap(vmid, name)
        snippet = f"/var/lib/vz/snippets/{name}.yaml"
        self.ssh(f"cat > {snippet}", stdin=cloud_config)
        # network config v2 with the MAC as DHCP identifier (see user_data)
        self.ssh(f"cat > /var/lib/vz/snippets/{name}-net.yaml", stdin=NETWORK_CONFIG)
        self.ssh(f"qm clone {self.template} {vmid} --name {name}", timeout=300)
        self.ssh(f"qm set {vmid} --net0 {self.net0_for(vmid)}")
        self.ssh(f"qm set {vmid} --cicustom user=local:snippets/{name}.yaml,network=local:snippets/{name}-net.yaml")
        self.ssh(f"cat > /etc/pve/firewall/{vmid}.fw", stdin=FIREWALL)
        self.ssh(f"qm start {vmid}", timeout=120)

    def reap(self, vmid, name):
        """Stop, destroy, remove snippet and firewall file. True iff gone."""
        self.ssh(f"qm stop {vmid} --skiplock 1", check=False)
        for _ in range(12):
            st = self.status(vmid)
            if st is None or st == "stopped":
                break
            self.sleep(5)
        self.ssh(f"qm destroy {vmid} --purge --skiplock 1", check=False, timeout=180)
        self.ssh(f"rm -f /var/lib/vz/snippets/{name}.yaml /var/lib/vz/snippets/{name}-net.yaml "
                 f"/etc/pve/firewall/{vmid}.fw", check=False)
        return self.status(vmid) is None

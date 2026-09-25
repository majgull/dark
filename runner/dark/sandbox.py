"""dark/sandbox.py - the seam between the runner and the compute plane.

A sandbox is one throwaway machine the runner injects a script into and
discards afterwards: today a Proxmox VM, later also a container. The runner
uses only the first five methods below, so a new backend implements them and
the callers do not change. `snapshot` and `rollback` are on the seam for the
applier that will rehearse a change on a clone; the runner does not call
them. `vm.Proxmox`, `docker.Docker` and `lxc.Lxc` are the
backends today.

`spawn` takes the files to write and the command to run, not a rendered
cloud-init document: rendering is Proxmox's business, and a backend without
cloud-init would have no use for one.

A backend's `spawn` may take keyword parameters beyond the protocol's, each
with a default that keeps the protocol's behaviour: `docker.Docker` takes
the image per spawn. A caller that passes one knows which backend it has.
"""

from typing import Protocol

from . import config
from . import docker
from . import lxc
from . import vm


class Sandbox(Protocol):
    """What the runner needs from the compute plane. Each implementation
    documents the contract of its own methods."""

    def reachable(self) -> bool:
        """Is the backend usable right now?"""
        ...

    def template_ok(self) -> tuple[bool, str]:
        """(ok, why) for the image a sandbox is cloned from."""
        ...

    def spawn(self, vmid, name, files, runcmd, cls=None) -> None:
        """Start a sandbox for `vmid`/`name`; `files` is {path: (content, mode)}
        and `runcmd` is a list of argv lists. `cls` is the run's class when
        the class changes the sandbox (a "user" sandbox may reach the
        application it checks); None for every other run. Raise on failure."""
        ...

    def guest_ip(self, vmid) -> str | None:
        """The sandbox's address, or None while it holds none."""
        ...

    def reap(self, vmid, name) -> bool:
        """Stop and remove the sandbox. True iff it is gone."""
        ...

    def snapshot(self, vmid, name) -> None:
        """Take a snapshot `name` of the sandbox. Raise on failure, or with
        NotImplementedError where the backend has no snapshots."""
        ...

    def rollback(self, vmid, name) -> None:
        """Roll the sandbox back to its snapshot `name`. Raise on failure, or
        with NotImplementedError where the backend has no snapshots."""
        ...


def make(host, template):
    """The one place the runner builds a backend object, chosen by
    host.backend. A name this build does not implement is refused by
    config.check_backend, never guessed. `template` names the VM a Proxmox
    sandbox is cloned from; docker sandboxes come from host.sandbox_image and
    lxc sandboxes from host.sandbox_container at host.sandbox_snapshot."""
    config.check_backend(host.backend)
    if host.backend == "docker":
        return docker.Docker(host.sandbox_image, network=host.sandbox_network,
                             cpus=host.sandbox_cpus, memory=host.sandbox_memory,
                             pids=host.sandbox_pids, target_network=host.target_network)
    if host.backend == "lxc":
        return lxc.Lxc(host.proxmox, host.sandbox_container, host.sandbox_snapshot,
                       bridge=host.sandbox_bridge, pool=host.sandbox_pool,
                       allow_in=host.sandbox_allow_in)
    return vm.Proxmox(host.proxmox, template, target_host=host.target_host)

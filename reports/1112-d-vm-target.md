# Report: 1112-d, the desktop arm's VM target (task 5, the dark side)

Two brief items: task 5 of `specs/desktop-arm/tasks.md` and the words for it.
Both are committed on this worktree's branch (`feat/desktop-arm`); nothing is
pushed, and `CHANGELOG.md` has no new line yet. Every command below ran in this
worktree.

## Terms, once

- **VM sandbox**: the desktop image booted as a virtual machine, as opposed to
  the **lab sandbox** (the same image run as a docker container). The spec
  names both; the plan's lab path is task 4 and this one is task 5.
- **template**: a Proxmox virtual machine marked (`template: 1`) so it cannot
  start itself but can be cloned into fresh VMs. dark never builds one; it
  clones one.
- **Proxmox backend**: `runner/dark/vm.py`'s `Proxmox`, the object that runs
  `qm` commands over ssh on the Proxmox host and gives the runner a
  throwaway VM (`spawn`, `guest_ip`, `reap`, `template_ok`).
- **cloud-init**: first-boot provisioning. The backend renders it into
  snippets that carry the injected files, the run command, the network config
  and a fresh `/etc/machine-id` (see `user_data`).
- **qemu-guest-agent**: the daemon inside the guest whose QEMU channel
  Proxmox commands with `qm guest cmd`; dark learns the guest's IP through it.
- **virtio-gl**: Proxmox's display device that gives the guest a GL-capable
  render node (`qm create --vga virtio-gl`), so a Wayland session has a GPU to
  draw on and `grim` can read a frame.
- **`desktop_vm_templates`**: the new host key: a table from a desktop task's
  `image` string to the template VM id to clone for it. TOML form is a
  `[host.desktop_vm_templates]` table; the env form is
  `DARK_DESKTOP_VM_TEMPLATES` with `<image>=<vmid>` pairs joined by commas,
  like the other host lists.
- **preflight**: `runner/dark/preflight.py`, the checks a shift runs before it
  starts (`template_ok` among them) and reports as `ok`/`MISS` lines.
- **fakes**: the in-process stand-ins the tests use instead of real machines.
  Here: a Proxmox `FakeSSH` (a scripted answer per `qm` command) and a Runner
  whose `launch` is replaced, so no ssh and no Proxmox are touched.
- **gate**: `cd runner && bash verify.sh`, which runs the tests, checks the
  configuration and refuses binaries; its last line is `verify OK`.

## Base state of the gate

Before any edit, from `runner/`: `bash verify.sh` printed
`Ran 880 tests in 210.9s` / `OK (skipped=2)` and ended `verify OK`.

The three test modules named by item 1 on the base: `python3 -m unittest
tests.test_run tests.test_desktop tests.test_preflight -q` was not run
separately on the base; the full gate covered them (880 tests, OK). After the
work the full gate printed `Ran 886 tests` / `OK (skipped=2)` / `verify OK`:
six new tests, none removed, nothing else moved.

## Item 1, `--target vm`

Commit `bde6b06 feat(desktop): run a desktop task in a Proxmox VM`.

What changed, file by file:

- `runner/dark/config.py`: `Host.desktop_vm_templates`, a dict, defaulting to
  empty. The value is read like the other host keys: `HOST_ENV` names
  `DARK_DESKTOP_VM_TEMPLATES`, and `__post_init__` parses a comma-joined
  `<image>=<vmid>` string from the environment (a table from `host.toml`
  passes through as the dict it is). Nothing about the existing keys moved.
- `runner/dark/__main__.py`, `cmd_desktop`: `--target vm` builds the Proxmox
  backend for the task's image from the table (`sandbox.make(host, template)`)
  instead of using the `_ctx` backend built from the user arm's
  `[shift].vm_template`. When the table has no entry for the task's image,
  the command prints one line naming the image and the key and exits 2 before
  `Runner.desktop` is reached, so no VM is built or spawned. `--target live`
  is still refused as its own task (task 6); the `--target` help text now
  says vm is built. The `Preflight` it constructs is given `template=`, so a
  desktop run reports the VM its backend came from.
- `runner/dark/preflight.py`: `Preflight` takes an optional `template` and the
  `vm template` check names it when present, else falls back to
  `[shift].vm_template` exactly as before. `template_ok()` itself is
  unchanged and still the only thing asked of the backend; only the id the
  line reports is new.
- `runner/dark/run.py`, `desktop_task` / `desktop` / `_desktop`: `target`
  threads from the command's argument through to `task.json`, which now
  carries `"target": "vm"` (or `"lab"`). The spawn keywords are the class
  alone for a VM (the Proxmox `spawn` signature), and the task's image plus
  the host's render devices only for a docker lab run; the condition is
  `target != "vm" and host.backend == "docker"`. The VM path already went
  through `_launch_networked`, so the cloud-init injection, the wait for
  `guest_ip`, the retry-on-silent-boot and the reap are the user arm's,
  unchanged. `desktop.py` needed no change: it reads `start` from `task.json`
  and runs the start script for both targets (see item 2's wording).
- New tests: `runner/tests/test_run.py` (the target and files reach the
  spawn; the table picks the template; a missing entry refuses before
  `Runner.desktop`), `runner/tests/test_preflight.py` (the desktop template
  is the one named; without one the shift template is), and
  `runner/tests/test_desktop.py` (the executor still runs the start script
  when `task.json` says `target = "vm"`).

Every acceptance clause and where it is proven:

| Clause | Test |
|---|---|
| the template comes from the table | `test_run.DesktopVmCommand.test_the_template_comes_from_the_table`: a `[host.desktop_vm_templates]` table naming `9101`; `sandbox.make` is patched to record the template it is handed, and the test asserts `9101` is in the recorded list and the run got `target="vm"` |
| a missing entry is refused before anything is spawned | `test_run.DesktopVmCommand.test_a_missing_entry_is_refused_before_anything_is_spawned`: rc 2, one line naming the image and the key, and `Runner.desktop` was never called |
| the files and the target reach the spawn | `test_run.DesktopArm.test_desktop_vm_target_sets_the_target_and_uses_the_proxmox_spawn`: the spawn keywords are `{"cls": "desktop"}`, and the injected `task.json` holds `target = "vm"`, the start script, the image and the steps |
| the start script runs either way | `test_desktop.DesktopExecutor.test_desktop_main_runs_the_start_script_for_a_vm_target` |
| preflight names the desktop template | `test_preflight.Preflight.test_a_desktop_run_names_the_desktop_template` (and the fallback keeps the old name) |

The named scope ran green:

```
python3 -m unittest tests.test_run tests.test_desktop tests.test_preflight -q
Ran 143 tests in 89.4s
OK
```

The fakes are the ones already in the tree: `test_run.py`'s `LocalRunner`
(a `Runner` whose `launch` runs nothing and whose `net_ip` is fixed), its
patched `sandbox.make`, and `test_preflight.py`'s `FakePx` (its
`template_ok` is canned). No ssh call and no Proxmox call was made by any
test or by this session.

### Decisions worth naming

- **Where the template is chosen.** The command chooses and refuses; the run
  uses the backend it was handed. Putting the lookup in the run would have
  made the refusal a run outcome instead of a config line, and the brief asks
  for the command to refuse. The cost is one extra `sandbox.make` for the
  shift template before the desktop one is built; `make` builds an object and
  runs no command, so nothing is spawned either time.
- **`sandbox.make`, not `vm.Proxmox(...)` directly.** `make` is the one place
  a backend is built from `host.backend`, so a `--target vm` run on a
  docker host gets `Docker` and the preflight line says so, rather than a
  Proxmox object the host never asked for.
- **`Preflight(template=)` rather than a second `template_ok` call.** The
  backend already knows its template; the check only needed the id for its
  human line.

## Item 2, the words for the VM target

Commit `5affcf5 docs(desktop): say what the VM target's template must carry`.

`specs/desktop-arm/plan.md` gained a short `## VM target` section between
"Where it lives" and "Why these choices". It says dark does not build the
template (bootc-image-builder makes the qcow2, `qm create` and `qm template`
make the template) and names the four things the template must carry:
cloud-init, qemu-guest-agent, a GL-capable display (`virtio-gl`), and the
session started by the image. It cites `rave
reports/1112-c2-vm-sandbox-reading.md`, items 1 and 2, as its source, and
notes the image-side gap the reading found (the tvbox image has no cloud-init
today). `tasks.md` item 5 was left exactly as it was: unticked, its acceptance
still the one real run, which a fakes-only session cannot honestly claim.

```
grep -q 'desktop_vm_templates' specs/desktop-arm/plan.md   # exit 0
cd runner && bash verify.sh                                 # verify OK
```

## Out of scope, reported not fixed

- **The real VM run.** tasks.md item 5's acceptance is one real run of
  `examples/desktop` with `--target vm`. That needs a Proxmox host, a built
  template and the image's cloud-init added; this brief says fakes only and
  no Proxmox calls, so the box stays unticked. The code path is covered by
  tests, not by a boot.
- **`examples/desktop/` is absent from the worktree.** `plan.md` names it and
  tasks.md item 4's acceptance runs it, but the directory does not exist here
  (task 4's own report may carry that). Not touched.
- **The reading's change 5 (an `ExecChannel` that ssh's to `guest_ip`) is not
  implemented.** The executor (and so `ExecChannel` and `desktop.py`) runs
  *inside* the sandbox; in a VM the sandbox is the guest, so its local
  `setpriv` channel is already the right one and no second hop is needed. The
  reading's change 6 (`users:` in `user_data` for an injected ssh key) is for
  that same channel and is likewise not needed for this path.
- **Nothing in `host.toml`.** The brief names only `config.py` for the key, so
  the checked-in example host config was not edited; the key works from a
  `host.toml` table or from `DARK_DESKTOP_VM_TEMPLATES`.

## Commits on `feat/desktop-arm` (this session)

```
5affcf5 docs(desktop): say what the VM target's template must carry
bde6b06 feat(desktop): run a desktop task in a Proxmox VM
```

Both authored as the branch's other commits (`majgull
<9626688+majgull@users.noreply.github.com>`). Not pushed.

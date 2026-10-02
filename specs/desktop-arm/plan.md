# Plan: the desktop arm

This is the plan for the feature in `spec.md`; `tasks.md` is its checklist. Terms are those of `spec.md`. The seams below are the ones `rave reports/1094-d-dark-user-arm.md` section 2 found in commit `3814307`; line numbers move, names do not.

## Where it lives

- `runner/dark/user.py`: `run_steps` takes the system prompt and the action-target function from the page object (`page.SYSTEM`, `page.action_target`), falling back to today's browser ones, so the browser path does not change.
- `runner/dark/desktop.py` (new): the executor inside the sandbox, beside `user.py` and `session.py`, whose reporting it shares. `ExecChannel` runs a command as the session user with the variables of `/run/dark-desktop-session` and returns rc and output; `DesktopPage(channel)` implements `goto` (launch the task's first application, optional), `url` (the focused window's application id and title), `snapshot` (windows from the compositor, then OCR lines from `tesseract` on a `grim` frame), `act` (the action set of spec requirement 2), `screenshot` (`grim`), `close`; `drain_requests` returns `[]`. The compositor is reached through a small table of commands per compositor (`mango`: `mmsg get all-clients`, `mmsg dispatch`; `niri`: `niri msg --json windows`), chosen from the env file. `run_checks(checks, channel_root, records_dir)` writes `checks.jsonl`.
- `runner/dark/tasks.py`: `_load_desktop_task` for class `desktop`: `image`, `start` (a file beside the task), `spec`, `steps`, optional `checks` (list of tables `id`, `command`, `timeout`), optional `live` (`host`, `user`).
- `runner/dark/spec.py`: `DESKTOP_CLASSES = ("desktop",)`, the `checks` fail kind in `FAIL_KIND_OUTCOME`.
- `runner/dark/run.py`: `Runner.desktop` like `Runner.user`: spawns the sandbox from `task.image` with the render device (`desktop_devices` in config, default `/dev/dri`), copies the start script and the executor in, runs the start script, then the executor as the session user; maps the tag. `--target vm` picks the Proxmox backend with a template built from the image; `--target live` uses an ssh channel to `live.host`.
- `runner/dark/__main__.py`: `cmd_desktop`; the MCP server gets `dark_desktop` like `dark_user`.
- `runner/budgets.toml`: `[class.desktop]`, like `[class.user]` with more seconds (OCR and launches are slower than a page).
- `examples/desktop/`: a small example desktop image (Fedora, a headless sway or mango, foot) with one task, so the arm is shown without the owner's TV box.

## VM target

`--target vm` boots the task's image as a VM. dark does not build the
template: `bootc-image-builder` turns the image into a qcow2, `qm create`
then makes a VM from it and `qm template` promotes it. That pipeline is
`rave reports/1112-c2-vm-sandbox-reading.md`, item 2; the list below is what
the resulting template must carry for dark's existing Proxmox backend to
work.

- **cloud-init**. Every clone is provisioned through cloud-init snippets
  (`user_data` in `runner/dark/vm.py`, attached with `--cicustom`): the task
  files, the run command, the network config and a fresh `/etc/machine-id`
  all arrive that way. The tvbox image carries no cloud-init today and must
  add the package; with a datasource present it is inert on the real box.
- **qemu-guest-agent**. `guest_ip` learns the guest's address through
  `qm guest cmd network-get-interfaces`, and `_launch_networked` waits on it,
  so the agent must answer early in boot.
- a **GL-capable display**, so the Wayland session has a render node to draw
  on and `grim` can read a frame: Proxmox's `virtio-gl` (`qm create --vga
  virtio-gl`). A plain `virtio` device gives the guest a GPU but no GL; the
  reading's `--vga virtio` is the headless lab default, not this one.
- the **session started by the image**. The box autologins into its desktop
  through its own units, so the task's start script in a VM only has to wait
  for that session; it is the same task file that runs in the lab and live,
  and telling the targets apart is the start script's business, not dark's.

The template's id is named per image in `host.desktop_vm_templates` (a table
from the task's `image` string to the template VM id, or the
`DARK_DESKTOP_VM_TEMPLATES` env form); a `--target vm` run whose image has no
entry is refused before any VM is built or spawned.

## Why these choices

- No VNC and no remote frame grabbing: the desktop image already has its compositor's tools, `grim` and `wtype`; doing everything inside the guest keeps the lab, the VM and the live target on one code path, with only the channel (container exec, VM ssh, live ssh) different.
- OCR instead of an accessibility tree: Wayland shells rarely export one; OCR lines plus the compositor's window list are text the quote rule can check.
- Checks run by the executor, not the model: they are the hidden tests (constitution: the model never sees the judge's material).

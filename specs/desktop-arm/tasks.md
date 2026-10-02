# Tasks: the desktop arm

This is the task list for the feature in `spec.md`, following `plan.md`. Do them in order, one commit each, and tick a box only when its acceptance command passes; run each from `runner/`. Every commit keeps `bash verify.sh` printing `verify OK`.

- [ ] 1. **The step loop takes its prompt and targets from the page.** `run_steps` reads `page.SYSTEM` and `page.action_target` when present, else the browser ones. Acceptance: `python3 -m unittest tests.test_user -q` unchanged, plus one test with a fake page that brings its own prompt.
- [ ] 2. **The desktop driver.** `runner/dark/desktop.py` with `ExecChannel`, `DesktopPage` (spec requirements 2 and 3) and the per-compositor command table, against a fake channel. Acceptance: `python3 -m unittest tests.test_desktop -q`.
- [ ] 3. **Desktop tasks and checks.** `_load_desktop_task`, `DESKTOP_CLASSES`, `[class.desktop]`, `run_checks` and the `checks` fail kind (spec requirements 4 and 8). Acceptance: `python3 -m unittest tests.test_tasks tests.test_desktop -q`.
- [ ] 4. **The lab sandbox and the command.** `Runner.desktop` on the docker backend, `cmd_desktop`, `dark_desktop` in the MCP server (spec requirement 5). Acceptance: `python3 -m unittest tests.test_run -q -k desktop`, then one real run of `examples/desktop` on a docker host.
- [ ] 5. **The VM sandbox.** A Proxmox template from the image (bootc image builder), `--target vm` (spec requirement 6). Acceptance: one real run of `examples/desktop` with `--target vm`.
- [ ] 6. **The live target.** `--target live` over ssh, refused without a `live` table (spec requirement 7). Acceptance: `python3 -m unittest tests.test_run -q -k live`.
- [ ] 7. **Words.** `docs/concepts.md`, `README.md`, `CHANGELOG.md` (spec requirement 9). Acceptance: `grep -c -E 'desktop (task|driver|snapshot)' ../docs/concepts.md` prints 3 or more.

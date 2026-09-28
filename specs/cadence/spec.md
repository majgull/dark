# Spec: small tasks, frequent patch releases

Read `AGENTS.md` and `memory/constitution.md` first. The development cycle stays tight when each task is small enough for a short, focused review, and when merged work ships as a patch release (`0.X.Y` to `0.X.Y+1`) soon after, instead of accumulating into a minor bump. These rules are enforced by the tools in this repository, not by the habits of whoever drives them.

## Requirements

1. **Bounded task.** A task's accepted change is bounded: the stager refuses a diff above the class's `max_diff_lines` and `max_files` in `budgets.toml`, and the refusal names the bound so the task gets split, not the bound raised. Test: a fixture diff one line over the bound is refused with that message.
2. **Short review packet.** The review packet for one task (diff, test output, verdict) fits the bound in item 1 plus a fixed header. Test: the packet of the largest allowed diff stays under the limit.
3. **Patch by default.** The release step proposes `0.X.(Y+1)` whenever `## [Unreleased]` has entries; it proposes `0.(X+1).0` only when an entry is marked `BREAKING`. Test: an Unreleased list without the marker yields a patch version.
4. **Release pressure.** The gate (`runner/verify.sh`) warns when `## [Unreleased]` holds more than 5 entries, and fails above 10, so merged work does not wait for a large release. Test: 6 entries print the warning, 11 fail the gate.

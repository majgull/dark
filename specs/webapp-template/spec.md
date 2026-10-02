# Spec: the web app template

Read `AGENTS.md`, `memory/constitution.md`, `specs/presentation/spec.md` and `specs/cadence/spec.md` first. `dark new webapp <name>` writes a new web application repository that an agent can build feature by feature from specs, with the checks that keep it from drifting built in rather than described. The lessons behind it, from five existing apps, are in rave `reports/1076-web-app-lessons.md`.

## Terms

- **scaffold**: the files `dark new webapp` writes.
- **tokens file**: the one stylesheet that defines every colour, font, type size, spacing step and radius the app may use.
- **check**: the one command that decides whether a change is done.

## Requirements

1. **Stack.** Vite, React and TypeScript with `strict` and `noUncheckedIndexedAccess`; Tailwind configured from the tokens file only; Hono, SQLite, Drizzle and Zod for the backend; Biome; Vitest; Playwright. Exact versions and a committed lock file. Test: the scaffold's `package.json` has no `^` or `~` range and the lock file exists.
2. **One check.** `npm run check` runs typecheck, lint, unit tests, the token gate and the build, and is the only definition of done in the scaffold's `AGENTS.md`. It needs no browser; the browser leg is a separate `npm run e2e` (requirement 10). Test: a scaffold with one type error, one lint error or one failing test makes `check` exit non-zero.
3. **Typed contract.** The frontend calls the backend through Hono's RPC client, so a changed response type is a compile error in the page. Test: renaming a field in the example route breaks `tsc`.
4. **Tokens enforced.** `DESIGN.md` states feel, references by name, a "don't" list (no gradients, no emoji icons, no hero section, no placeholder data) and copy rules; the token gate fails on a raw colour, size or radius outside the tokens file. Test: a component with `#7c3aed` fails `check`.
5. **Specs first.** `specs/_example/` holds `spec.md`, `plan.md` and `tasks.md` in dark's shape; each requirement names its test and its user steps. Test: `dark` loads a user task generated from the example spec's user steps.
6. **Stranger and demo.** The generated user task runs under the user arm against the dev server, and its records cut into a presentation video under `specs/presentation`. Test: an end-to-end run on the example app yields a pass outcome and a video.
7. **Small footprint.** The built frontend is static files; the backend idles under 100 MB resident. Test: a measurement script in the scaffold prints the resident size after start and fails above the bound.
8. **One shell.** One shell file owns the layout and mounts every panel with the same props; a panel lane writes only its panel file and its tests, never the shell. Test: `src/rules.test.ts` fails when a panel module is imported anywhere but the shell.
9. **A role for a test.** The app can be started with a role chosen for a test through a query parameter read only in a test build, and the example's browser test runs one step as a lower role. Test: `src/role.test.ts` fails when a test build ignores the query or a production build reads it.
10. **A separate browser leg.** `npm run e2e` runs Playwright on its own, taking its port from `PORT` so several worktrees run it side by side, and `npm run check` needs no browser. Test: `src/rules.test.ts` fails when playwright is added to `check`, when `e2e` drops Playwright or its test-mode build, or when the config stops reading `PORT`.
11. **Time in tests.** Unit tests have a 30 s timeout, and no test measures wall time without the marker `timing`. Test: `src/rules.test.ts` fails when the timeout moves or when a test measures wall time without the marker.

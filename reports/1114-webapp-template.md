# Web app template files (thread 1114, findings 1 and 2)

This branch gives dark's web app template its files. `specs/webapp-template/spec.md` was text with all 7 tasks unchecked and `templates/` held only `go` and `python`; `templates/webapp/` now holds the frontend half of requirements 1, 2 and 4 as runnable files, and requirements 8 to 11 write the four panel-lane rules of rave report `1114-tooling-gaps-from-989.md` finding 2 with a test each. The Hono backend of requirement 3 and tasks 5 to 7 stay unchecked. Nothing is merged or pushed by this lane.

Commits on the branch:

- `cb52416` `feat(templates): add the webapp frontend scaffold` (item 1: the files, the one check, the example, the dark test for the four kinds of drift).
- `1653bf4` `feat(templates): add the four panel-lane rules to the webapp scaffold` (item 2: requirements 8 to 11, the role override, the shell and time rules, the browser leg, and their tests).

## What `templates/webapp/` holds

The scaffold is Vite, React and TypeScript with `strict` and `noUncheckedIndexedAccess`, Tailwind themed from `src/tokens.css` only, Biome, Vitest and Playwright, with exact versions and a committed lock file. `npm run check` is typecheck, lint, unit tests, the token gate and the build, and needs no browser. `npm run e2e` builds in test mode and runs Playwright on `PORT`, so several worktrees run side by side. `DESIGN.md` states the feel, references by name, the don't list (no gradients, no emoji icons, no hero section, no placeholder data) and the copy rules. `AGENTS.md` states the one check and the panel-lane rules.

The example is one list of notes with add and remove. Its `Routes` and view types come from `contract/app.schema.json` through `tools/gen_types.py` (taken from station, with its `--check`), so the scaffold shows a typed contract for a backend in any language; the Hono backend of requirement 3 stays unchecked. `tools/stub-server.mjs` uses Node's own `http` and serves the two routes for the browser test. `src/ui` is `Button` (a required `title` tooltip), `Field` and `Notice`; `src/panels/Notes.tsx` has its Vitest file and its Playwright file, which presses with `mouse.down`, 150 ms, `mouse.up`.

## The four rules, each with its test

Requirement 8 (one shell): `src/App.tsx` owns the layout and mounts every panel with the same `PanelProps`; `src/rules.test.ts` fails when a panel module is imported anywhere but the shell, and also when the shell does not mount every panel with those props.

Requirement 9 (a role for a test): `src/role.tsx` reads `?role=listener|member|admin` only when `import.meta.env.MODE` is `test`, which `npm run e2e` builds; a production build falls back to the view's role. `src/role.test.ts` covers it, and `e2e/notes.spec.ts` runs one step as a listener that sees the list and no add control.

Requirement 10 (a separate browser leg): `npm run e2e` is `vite build --mode test && playwright test`, the config takes its port from `process.env.PORT`, and `npm run check` has no playwright in it. Requirement 2's text now says the one check needs no browser. `src/rules.test.ts` fails when playwright is added to `check`, when `e2e` drops Playwright or its test-mode build, or when the config stops reading `PORT`.

Requirement 11 (time in tests): `vite.config.ts` sets `testTimeout: 30_000`, and no test measures wall time without the marker `timing`. `src/rules.test.ts` fails when the timeout moves or when a test measures wall time without the marker.

## Files taken from `~/main/projects/station/webapp/`, and what made them neutral

- `package.json`: name `station-app` became `notes-app`; the stack and every exact version are the same; `e2e` now builds in test mode, so the browser leg reads `?role=`.
- `package-lock.json`: copied from the same dependency graph; the two `name` fields became `notes-app`.
- `tsconfig.json`: same options; added `vite/client` to `types`, which `role.tsx` needs for `import.meta.env.MODE`.
- `biome.json`: same schema, includes, formatter and linter; the `Field.tsx` override and the `useValidAriaRole` options are kept.
- `vite.config.ts`: dropped station's `/app/` base and its station comment; kept jsdom, the setup file and the test include; added the 30 s timeout.
- `playwright.config.ts`: dropped the control.sh comment and spawn notes; the config now reads `PORT` and sets `baseURL`; tests run headless, one worker.
- `tools/check-tokens.mjs`: the same rules and scan; the comment no longer names station.
- `src/tokens.css`: station's station palette became a small neutral notes palette (ink, muted, line, panel, field, wash, accent, on-accent, warn; three type sizes; three spacing steps; three radii); no station names remain.
- `src/index.css`: the same shape (Tailwind, then the tokens, then the body); comments neutral.
- `src/api.ts`: station's typed `Send` and `Read` over the generated `Routes`; "control" became "the server", and the sample routes and comments are the notes app's.
- `src/validate.ts`: the same JSON Schema subset validator; `parsePos` and the station event types are gone, so only `parseView` remains; comments neutral.
- `src/store.ts`: station's SSE store became a small store that loads the view once through `read`, guards it with `parseView`, and keeps selectors (`useStore`, `useView`); no EventSource, backoff or stream state.
- `src/ui/Button.tsx`: station's Button without the ARIA/data/role props the notes example does not use; the required `title`, the pending state and `run`/`onResult` stay.
- `src/ui/Field.tsx` and `src/ui/Notice.tsx`: the same shared shapes; the Notice message "Control refused that." became "The server refused that.".
- `src/main.tsx` and `src/App.tsx`: station's entry and shell reduced to the notes app; `App.tsx` resolves the role through `role.tsx`.
- `src/panels/Now.tsx` and `src/panels/Now.test.tsx`: became `src/panels/Notes.tsx` and `src/panels/Notes.test.tsx`, the notes list with add and remove instead of the on-air player; the panel structure, `useView`, `Button` and `Notice` are the same; the station Profiler and position-event tests are dropped and a listener case is added.
- `e2e/now.spec.ts`: became `e2e/notes.spec.ts`; it starts the stub server on `PORT` instead of `tools/control.sh`, and presses with `mouse.down`, 150 ms, `mouse.up`; the station skip-count assertions became a note-list check plus a listener step.
- `AGENTS.md`: station's panel rules rewritten for the notes example; no station, control, song or panel-lane list remains.
- `tools/gen_types.py`: copied with the paths moved to `contract/app.schema.json`, `src/contract.gen.ts` and `src/contract.schema.gen.ts`, and the header and run command changed to `python3 tools/gen_types.py`; the generator logic is the same.

Written new, modelled on `templates/go` and `templates/python` where a template needs it: `.dark/verify.sh`, `.gitea/workflows/ci.yml`, `.gitignore`, `.editorconfig`, `README.md`. Written new for the example: `contract/app.schema.json`, `tools/stub-server.mjs`, `DESIGN.md`, `src/role.tsx`, `src/role.test.ts`, `src/rules.test.ts`. `src/contract.gen.ts` and `src/contract.schema.gen.ts` are generated by the generator.

## Acceptance evidence

Item 1, fresh copy outside the repo (`cp -a` to a scratch dir, then `npm ci`, then the two legs). `npm ci` printed `added 124 packages in 5s`, and `npm run check` ended:

```
 Test Files  7 passed (7)
      Tests  25 passed (25)
tokens: 24 files, nothing raw outside src/tokens.css
✓ built in 1.55s
```

`PORT=8811 npm run e2e` ended:

```
Running 2 tests using 1 worker
stub server on http://127.0.0.1:8811
  ✓  1 e2e/notes.spec.ts:43:1 › a held press on Add note reaches the server and the list (1.6s)
  ✓  2 e2e/notes.spec.ts:57:1 › a listener sees the list but no control it may not use (780ms)
  2 passed (8.0s)
```

`grep -c '[\^~]' templates/webapp/package.json` printed `0`.

The dark test `runner/tests/test_template_webapp.py` proves, on a fresh copy with one `npm ci`, that each of requirement 2's and 4's four kinds of drift makes `npm run check` non-zero: a type error (`TS`), a lint error (`lint/`), a failing unit test (`deliberately fails`) and a raw colour `#7c3aed` (`outside tokens.css`). `python3 -m unittest tests.test_template_webapp` ran `Ran 10 tests` and `OK`.

Item 2, one failure line per rule when the rule is broken in the scratch copy:

```
(a) AssertionError: a panel is imported outside the shell: expected [ 'src/leak.ts' ] to deeply equal []
(b) AssertionError: a test build reads the role query: expected null to be 'listener' // Object.is equality
(c) AssertionError: npm run check must not run playwright: expected 'tsc --noEmit && biome check . && vite…' not to contain 'playwright'
(d) AssertionError: a test measures wall time without the marker timing: expected [ 'src/validate.test.ts' ] to deeply equal []
```

The four rules' tests pass in `npm run check` (25 tests, 7 files), and `npm run check` and `PORT=8811 npm run e2e` pass in the fresh copy shown above.

Whole acceptance: `tasks.md` has tasks 1, 2 and 4 checked with their commits and tasks 3, 5, 6 and 7 unchecked. dark's own gate for the files touched was `cd runner && bash verify.sh`, which ended `Ran 847 tests in 423.442s`, `OK (skipped=2)`, then `verify OK`.

## Not done

- The CHANGELOG's `## [Unreleased]` was not touched, because this lane's write list is `templates/webapp/**`, the two webapp-template spec files, the one dark test, and this report. Rule 27 of the constitution asks for an entry on a user-visible change; the entry for `templates/webapp/` still has to be added by whoever owns `CHANGELOG.md`.
- The Hono backend of requirement 3 has no files: no `src/server.ts`, no SQLite, Drizzle or Zod, and no RPC client. Task 3 is unchecked and its test does not exist.
- Tasks 5, 6 and 7 are untouched: no `specs/_example/`, no `dark new webapp <name>`, no footprint measurement.
- `npm run e2e` needs a Playwright browser. This host had one cached, so the e2e leg ran; a host with no browser must run `npx playwright install chromium` first, and the template's `AGENTS.md` does not say so yet.
- The template's `.dark/verify.sh` runs typecheck, lint, unit tests, the token gate and the build, but not the browser leg, so an executor VM without a browser can still call the gate green while the e2e file is stale.
- The four rule tests read `package.json`, `vite.config.ts`, `playwright.config.ts` and the source tree by path; a file moved without its test updated would fail the check, which is intended, but the failure names a path rather than a rule.
- `runner/tests/test_template_webapp.py` installs with `npm ci` and runs `npm run check` ten times, about three and a half minutes on this host, so `cd runner && bash verify.sh` grew by that much. A host without node or npm skips the class rather than failing it.

# The notes app

Vite, React and TypeScript with `strict` and `noUncheckedIndexedAccess`, Tailwind themed from `src/tokens.css` only, Biome, Vitest and Playwright. `npm ci` installs exact versions from `package-lock.json`; the versions in `package.json` carry no range.

`npm run check` is the only definition of done. It runs, in order: `tsc --noEmit`, `biome check .`, `vitest run`, the token gate (`node tools/check-tokens.mjs`) and `vite build`. It needs no browser. `npm run e2e` is the browser check: it builds the app in test mode, starts `tools/stub-server.mjs` on `PORT` and runs Playwright. The two legs are separate so several worktrees can run side by side.

Every colour, font size and radius a component names must come from `src/tokens.css`; the token gate fails a raw one. `src/contract.gen.ts` and `src/contract.schema.gen.ts` are generated from `contract/app.schema.json` by `tools/gen_types.py` and are never edited by hand; Biome leaves `*.gen.ts` out of formatting and linting. `python3 tools/gen_types.py --check` fails when they are stale.

## Writing a panel

A panel lane writes `src/panels/<Name>.tsx`, that panel's Vitest file and its Playwright file, and nothing else: never `store.ts`, `api.ts`, `validate.ts`, `App.tsx` or `src/ui`. The shell mounts every panel with the same `PanelProps`; the store already holds the whole typed view.

- One shell file (`src/App.tsx`) owns the layout and mounts every panel with the same props; a panel lane never edits it, and `src/rules.test.ts` fails when a panel module is imported anywhere but the shell.
- Read the view only through `useView(store, selector)` with a selector narrow enough that an update redraws only what changed.
- Every action is `send(route, body)`, typed by `src/contract.gen.ts`; a refusal is a value, drawn with `Notice`, never thrown.
- Every control comes from `src/ui` (`Button`, `Field`, `Notice`) with its label and its `title` tooltip; never rebuild one.
- A build started for a test (`--mode test`, which `npm run e2e` runs) reads `?role=listener|member|admin`; a production build ignores it. Draw a control only at its role's floor (`roleAtLeast`), and let the test run one step as a lower role.
- Key each `Button` by what it does, not by its place, so a view update never remounts it mid-press.
- Colours, sizes, spacing and radii come from `src/tokens.css` names only; the token gate fails a raw one.
- Unit tests have a 30 s timeout; a test that measures wall time carries the marker `timing`, and `src/rules.test.ts` fails one that does not.
- Its Vitest file covers one render, one action and one refusal; its e2e file presses with `mouse.down`, a hold, then `mouse.up`.

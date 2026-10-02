# webapp template (dark/templates)

The starting tree every web app task repo is materialised from: Vite, React and strict TypeScript, Tailwind themed from `src/tokens.css`, Biome, Vitest and Playwright, with the checks that keep the app from drifting built in. `npm run check` is the one definition of done and needs no browser; `npm run e2e` is the browser leg and takes its port from `PORT`. The task's `start/` overlay is applied on top; hidden acceptance tests never live here.

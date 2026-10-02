// The browser check beside the unit tests. It is not part of `npm run check`
// (which needs no browser); `npm run e2e` builds the app in test mode and runs
// it. The port comes from PORT, so several worktrees can run it side by side.
import { defineConfig } from "@playwright/test";

const PORT = Number(process.env.PORT ?? "8117");

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { headless: true, baseURL: `http://127.0.0.1:${PORT}` },
});

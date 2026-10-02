// The panel's Playwright file: the page in a real browser, driven over a real
// mouse. `npm run e2e` builds the app in test mode first, then this starts
// tools/stub-server.mjs on PORT itself, the way a worktree runs it beside
// another. A held press (down, 150 ms, up) is the whole interaction.
import { type ChildProcess, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const PORT = Number(process.env.PORT ?? "8117");
const BASE = `http://127.0.0.1:${PORT}`;
const HOLD_MS = 150;

let server: ChildProcess | null = null;

async function waitForServer(): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${BASE}/api/notes`);
      if (response.ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`the stub server did not answer on ${BASE}`);
}

test.beforeAll(async () => {
  server = spawn(process.execPath, ["tools/stub-server.mjs"], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(PORT) },
    stdio: ["ignore", "inherit", "inherit"],
  });
  await waitForServer();
});

test.afterAll(() => {
  if (server !== null) server.kill();
});

test("a held press on Add note reaches the server and the list", async ({ page }) => {
  await page.goto(BASE);
  await expect(page.getByText("Buy milk")).toBeVisible();

  await page.getByLabel("New note").fill("Buy eggs");
  const add = page.getByRole("button", { name: "Add note" });
  await add.hover();
  await page.mouse.down();
  await page.waitForTimeout(HOLD_MS);
  await page.mouse.up();

  await expect(page.getByText("Buy eggs")).toBeVisible();
});

test("a listener sees the list but no control it may not use", async ({ page }) => {
  await page.goto(`${BASE}/?role=listener`);
  await expect(page.getByText("Buy milk")).toBeVisible();
  await expect(page.getByRole("button", { name: "Add note" })).toHaveCount(0);
});

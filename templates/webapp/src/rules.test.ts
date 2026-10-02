// The four lane rules that keep parallel work off shared files, each checked
// from the repository itself rather than trusted. A rule test fails when the
// rule is broken, so `npm run check` is the one gate for all four.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = process.cwd();

function walk(dir: string): string[] {
  const found: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) found.push(...walk(path));
    else found.push(path);
  }
  return found;
}

function read(relativePath: string): string {
  return readFileSync(join(ROOT, relativePath), "utf8");
}

const PANEL_IMPORT = /^\s*import\b[^\n]*["'][^"']*\/panels\//m;

describe("the shell owns the panels", () => {
  it("mounts every panel only from the shell", () => {
    const sources = walk(join(ROOT, "src")).filter((path) => /\.tsx?$/.test(path));
    const offenders = sources
      .filter((path) => relative(ROOT, path) !== "src/App.tsx")
      .filter((path) => PANEL_IMPORT.test(readFileSync(path, "utf8")))
      .map((path) => relative(ROOT, path));
    expect(offenders, "a panel is imported outside the shell").toEqual([]);

    const shell = read("src/App.tsx");
    const panels = readdirSync(join(ROOT, "src", "panels"))
      .filter((name) => name.endsWith(".tsx") && !name.endsWith(".test.tsx"))
      .map((name) => name.slice(0, -4));
    for (const name of panels) {
      expect(shell, `the shell does not import ${name}`).toContain(`from "./panels/${name}"`);
      expect(shell, `the shell does not mount ${name} with the panel props`).toContain(
        `<${name} store={store} send={send}`,
      );
    }
  });
});

describe("the browser leg", () => {
  it("is separate and takes its port from PORT", () => {
    const packageText = read("package.json");
    const scripts = (JSON.parse(packageText) as { scripts: Record<string, string> }).scripts;
    expect(scripts.check, "npm run check must not run playwright").not.toContain("playwright");
    expect(scripts.e2e, "npm run e2e must run playwright").toContain("playwright");
    expect(scripts.e2e, "npm run e2e must build in test mode").toContain("--mode test");
    expect(read("playwright.config.ts"), "the browser leg must take its port from PORT").toContain(
      "process.env.PORT",
    );
  });
});

describe("time in tests", () => {
  it("gives unit tests 30 s and forbids unmarked wall-clock timing", () => {
    expect(read("vite.config.ts"), "unit tests must have a 30 s timeout").toMatch(
      /testTimeout:\s*30_?000/,
    );
    const measures = /\b(Date\.now|performance\.now|console\.time|process\.hrtime)\b/;
    const offenders = walk(join(ROOT, "src"))
      .filter((path) => /\.test\.tsx?$/.test(path))
      .filter((path) => {
        const text = readFileSync(path, "utf8");
        return measures.test(text) && !/\btiming\b/.test(text);
      })
      .map((path) => relative(ROOT, path));
    expect(offenders, "a test measures wall time without the marker timing").toEqual([]);
  });
});

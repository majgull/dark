#!/usr/bin/env node
// The token gate: every colour, font size and radius comes from src/tokens.css,
// so the theme stays in the one file the design names. Part of `npm run check`.
// Scans src/**.{ts,tsx,css} and index.html; src/tokens.css is the one exemption,
// and a style value that is `var(--...)` is a token.
//
// A raw value in a component (a hex colour, a px font size, a radius that is not
// a var) is a finding; the file, line and value are printed and the exit is 1.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL("..", import.meta.url));
const TOKENS = join(HERE, "src", "tokens.css");

const RULES = [
  { what: "a colour outside tokens.css", re: /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/ },
  { what: "a px font size outside tokens.css", re: /\bfont-size\s*:[^;{}]*\d+(\.\d+)?px/ },
  {
    what: "a px font size outside tokens.css",
    re: /\bfontSize\s*[:=]\s*["'`][^"'`]*\d+(\.\d+)?px/,
  },
  { what: "a px font size outside tokens.css", re: /\btext-\[[^\]]*\d+(\.\d+)?px[^\]]*\]/ },
  {
    what: "a radius outside tokens.css",
    re: /\b(?:border-radius|borderRadius)\s*[:=]\s*["'`]?(?!var\()/,
  },
  { what: "a radius outside tokens.css", re: /\brounded-\[[^\]]+\]/ },
];

function walk(dir) {
  const files = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) files.push(...walk(path));
    else files.push(path);
  }
  return files;
}

function main() {
  const targets = [...walk(join(HERE, "src")), join(HERE, "index.html")];
  const findings = [];
  for (const file of targets) {
    if (file === TOKENS || file.endsWith(".gen.ts")) continue;
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      for (const rule of RULES) {
        const found = line.match(rule.re);
        if (found !== null)
          findings.push(`${relative(HERE, file)}:${index + 1}: ${rule.what}: ${found[0]}`);
      }
    });
  }
  if (findings.length > 0) {
    console.error(findings.join("\n"));
    process.exit(1);
  }
  console.log(`tokens: ${targets.length} files, nothing raw outside src/tokens.css`);
}

main();

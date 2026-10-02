#!/usr/bin/env node
// A stub server for the example, with no dependency: Node's own `http`. It
// serves the built page from dist/ and the two contract routes, so the browser
// test (`npm run e2e`) needs no backend. A real backend of any language replaces
// it; the contract in contract/app.schema.json is what both sides agree on.
import { readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const DIST = join(ROOT, "dist");
const PORT = Number(process.env.PORT ?? "8117");
const ROLE = "member";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

let notes = [{ id: "one", text: "Buy milk" }];
let next = 1;

function json(response, status, body) {
  const text = JSON.stringify(body);
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(text);
}

async function body(request) {
  let text = "";
  for await (const chunk of request) text += chunk;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function onNotes(request, response) {
  if (request.method === "GET") return json(response, 200, { notes, role: ROLE });
  if (request.method !== "POST") return json(response, 405, { message: "only GET and POST" });
  const command = await body(request);
  if (command === null || typeof command !== "object") {
    return json(response, 400, { ok: false, notes, message: "a command must be JSON" });
  }
  if (command.kind === "add" && typeof command.text === "string" && command.text !== "") {
    notes = [...notes, { id: `n${next++}`, text: command.text }];
    return json(response, 200, { ok: true, notes, message: "" });
  }
  if (command.kind === "remove" && typeof command.id === "string") {
    const kept = notes.filter((note) => note.id !== command.id);
    if (kept.length === notes.length) {
      return json(response, 400, { ok: false, notes, message: "no note with that id" });
    }
    notes = kept;
    return json(response, 200, { ok: true, notes, message: "" });
  }
  return json(response, 400, { ok: false, notes, message: "unknown command" });
}

function onStatic(request, response) {
  const url = new URL(request.url ?? "/", "http://localhost");
  const wanted = url.pathname === "/" ? "/index.html" : url.pathname;
  const path = normalize(join(DIST, wanted));
  if (!path.startsWith(DIST)) {
    response.writeHead(403);
    response.end("forbidden");
    return;
  }
  let file = path;
  try {
    if (statSync(file).isDirectory()) file = join(file, "index.html");
  } catch {
    response.writeHead(404);
    response.end("not found");
    return;
  }
  try {
    const data = readFileSync(file);
    response.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
    response.end(data);
  } catch {
    response.writeHead(404);
    response.end("not found");
  }
}

createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://localhost");
  if (url.pathname === "/api/notes") {
    void onNotes(request, response);
    return;
  }
  onStatic(request, response);
}).listen(PORT, "127.0.0.1", () => {
  console.log(`stub server on http://127.0.0.1:${PORT}`);
});

// The page's one read and send, at run time: the URL `read` builds from a
// route's path parameters and query, and the body `send` posts. The calls are
// typed at compile time by the generated Routes table; this file checks the
// string and the body that reach `fetch`.
import { describe, expect, it } from "vitest";
import { createApi } from "./api";

type Call = { readonly url: string; readonly init?: RequestInit };

/** A fetch that records each call and answers an empty JSON object. */
function capture(calls: Call[]): typeof fetch {
  return (input, init) => {
    calls.push(init === undefined ? { url: String(input) } : { url: String(input), init });
    return Promise.resolve(new Response("{}", { status: 200 }));
  };
}

describe("read", () => {
  it("gets the route's path", async () => {
    const calls: Call[] = [];
    const api = createApi("", capture(calls));
    await api.read("GET /api/notes");
    expect(calls.map((call) => call.url)).toEqual(["/api/notes"]);
  });
});

describe("send", () => {
  it("posts the body to the route's path", async () => {
    const calls: Call[] = [];
    const api = createApi("", capture(calls));
    await api.send("POST /api/notes", { kind: "add", text: "Buy milk" });
    expect(calls[0]?.url).toBe("/api/notes");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ kind: "add", text: "Buy milk" }));
  });

  it("answers a refusal as a value, not a thrown error", async () => {
    const api = createApi("", () =>
      Promise.resolve(new Response(JSON.stringify({ message: "not now" }), { status: 400 })),
    );
    const result = await api.send("POST /api/notes", { kind: "remove", id: "one" });
    expect(result).toEqual({ ok: false, status: 400, message: "not now" });
  });
});

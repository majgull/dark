// The store: it loads the view through `read`, guards it, and reports a refused
// or malformed answer in `lastError` without dropping the last good view.
import { describe, expect, it } from "vitest";
import { createApi } from "./api";
import type { Note } from "./contract.gen";
import { createStore } from "./store";

const NOTE: Note = { id: "one", text: "Buy milk" };

function apiAnswering(body: unknown, status = 200) {
  return createApi("", () => Promise.resolve(new Response(JSON.stringify(body), { status })));
}

describe("createStore", () => {
  it("loads the view and answers a selector", async () => {
    const api = apiAnswering({ notes: [NOTE], role: "member" });
    const store = createStore(api.read);
    await store.load();
    expect(store.getSnapshot().loaded).toBe(true);
    expect(store.getSnapshot().view?.notes).toEqual([NOTE]);
    expect(store.getSnapshot().lastError).toBeNull();
  });

  it("keeps a refusal as lastError and no view", async () => {
    const api = apiAnswering({ message: "not now" }, 503);
    const store = createStore(api.read);
    await store.load();
    expect(store.getSnapshot().view).toBeNull();
    expect(store.getSnapshot().lastError).toBe("not now");
  });

  it("names the path of a malformed view", async () => {
    const api = apiAnswering({ notes: [] });
    const store = createStore(api.read);
    await store.load();
    expect(store.getSnapshot().lastError).toContain("role");
  });

  it("replaces the notes with what a command answered", async () => {
    const api = apiAnswering({ notes: [NOTE], role: "member" });
    const store = createStore(api.read);
    await store.load();
    store.apply([]);
    expect(store.getSnapshot().view?.notes).toEqual([]);
  });
});

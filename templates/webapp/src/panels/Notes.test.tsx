// The panel's Vitest file: one render, one action and one refusal, plus what a
// lower role sees. It stands in its own `fetch`, so it needs no server, and it
// drives the panel through the same store, send and role the shell would mount
// it with.
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { type Api, createApi } from "../api";
import type { Note } from "../contract.gen";
import { type Role, RoleProvider } from "../role";
import { createStore, type Store } from "../store";
import { Notes } from "./Notes";

const MILK: Note = { id: "one", text: "Buy milk" };
const EGGS: Note = { id: "two", text: "Buy eggs" };

/** An api whose GET always answers `notes` and whose POST answers `answer`. */
function apiFor(notes: readonly Note[], answer: unknown, status = 200): Api {
  return createApi("", (_input, init) => {
    const isPost = init?.method === "POST";
    const body = isPost ? answer : { notes, role: "member" };
    return Promise.resolve(new Response(JSON.stringify(body), { status: isPost ? status : 200 }));
  });
}

/** storeOn(api) -> a store that has already loaded what the api's GET answers. */
async function storeOn(api: Api): Promise<Store> {
  const store = createStore(api.read);
  await store.load();
  return store;
}

/** panel(store, api, role) -> the panel as the shell mounts it. */
function panel(store: Store, api: Api, role: Role = "member"): ReactElement {
  return (
    <RoleProvider role={role}>
      <Notes store={store} send={api.send} />
    </RoleProvider>
  );
}

describe("Notes", () => {
  it("draws every note it was given", async () => {
    const api = apiFor([MILK], {});
    const store = await storeOn(api);
    render(panel(store, api));
    expect(screen.getByText("Buy milk")).toBeInTheDocument();
    expect(screen.getByLabelText("New note")).toBeInTheDocument();
  });

  it("sends an add command and draws the answered list", async () => {
    const api = apiFor([MILK], { ok: true, notes: [MILK, EGGS], message: "" });
    const store = await storeOn(api);
    render(panel(store, api));
    fireEvent.change(screen.getByLabelText("New note"), { target: { value: "Buy eggs" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add note" }));
    });
    expect(screen.getByText("Buy eggs")).toBeInTheDocument();
  });

  it("draws a refusal with the shared Notice", async () => {
    const api = apiFor([MILK], { ok: false, notes: [MILK], message: "not now" }, 400);
    const store = await storeOn(api);
    render(panel(store, api));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add note" }));
    });
    expect(await screen.findByRole("status")).toHaveTextContent("not now");
  });

  it("gives a listener the list but no add or remove control", async () => {
    const api = apiFor([MILK], {});
    const store = await storeOn(api);
    render(panel(store, api, "listener"));
    expect(screen.getByText("Buy milk")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add note" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });
});

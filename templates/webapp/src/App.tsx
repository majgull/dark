// The page's shell: it owns the layout and mounts every panel with the same
// props. A panel lane writes src/panels/<Name>.tsx, that panel's Vitest file and
// its Playwright file, and never this file. One shell means two lanes never edit
// one file, and the role and its floors come from role.ts.
import type { Send } from "./api";
import { Notes } from "./panels/Notes";
import { RoleProvider, roleFor } from "./role";
import { type Store, useView } from "./store";

export function App({ store, send }: { store: Store; send: Send }) {
  const view = useView(store, (value) => value);
  const role = roleFor(window.location.search, import.meta.env.MODE === "test", view);
  return (
    <RoleProvider role={role}>
      <div className="mx-auto max-w-2xl p-4">
        <header className="border-b border-line pb-pad">
          <h1 className="m-0 text-title font-semibold">Notes</h1>
        </header>
        <main>
          <Notes store={store} send={send} />
        </main>
      </div>
    </RoleProvider>
  );
}

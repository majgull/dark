// The page's one store. It holds the whole typed view, loaded once from
// GET /api/notes and checked against contract.schema.gen.ts before it is kept,
// so a panel reads any field it needs without the store naming it first. Every
// update replaces the view through `apply`, and a panel reads it through
// `useView` selectors, so a store update redraws only the components that select
// what changed.
//
// A refused answer leaves the last good view in place and names the fault, its
// path and its reason, in `lastError`, which a panel can draw with `Notice`. The
// store takes `read` as an argument, so a test can stand in its own without a
// server.
import { useSyncExternalStore } from "react";
import type { Read } from "./api";
import type { Note, NotesView } from "./contract.gen";
import { parseView } from "./validate";

export type State = {
  readonly view: NotesView | null;
  readonly loaded: boolean;
  readonly lastError: string | null;
};

export type Store = {
  subscribe(listener: () => void): () => void;
  getSnapshot(): State;
  load(): Promise<void>;
  apply(notes: readonly Note[]): void;
};

export function createStore(read: Read): Store {
  let state: State = { view: null, loaded: false, lastError: null };
  const listeners = new Set<() => void>();

  function emit(next: State): void {
    state = next;
    for (const listener of listeners) listener();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot() {
      return state;
    },
    load() {
      return read("GET /api/notes").then((result) => {
        if (!result.ok) {
          emit({ ...state, lastError: result.message });
          return;
        }
        const parsed = parseView(result.value);
        if (!parsed.ok) {
          emit({ ...state, lastError: `${parsed.path}: ${parsed.why}` });
          return;
        }
        emit({ ...state, view: parsed.value, loaded: true, lastError: null });
      });
    },
    apply(notes) {
      if (state.view === null) return;
      emit({ ...state, view: { ...state.view, notes } });
    },
  };
}

/** useStore(store, select) -> the selected slice, redrawn only when it changes. */
export function useStore<T>(store: Store, select: (state: State) => T): T {
  return useSyncExternalStore(store.subscribe, () => select(store.getSnapshot()));
}

/** useView(store, select) -> a slice of the view; the panels' only way in. */
export function useView<T>(store: Store, select: (view: NotesView | null) => T): T {
  return useStore(store, (state) => select(state.view));
}

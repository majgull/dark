// Every panel is mounted by the shell (src/App.tsx) with exactly these props.
// A panel lane writes its own file and its tests and never edits the shell, so
// two lanes never touch one file.
import type { Send } from "../api";
import type { Store } from "../store";

export type PanelProps = { readonly store: Store; readonly send: Send };

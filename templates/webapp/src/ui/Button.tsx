// The page's one button. A panel passes its label, the tooltip the props type
// requires, and the action to run; while that action is in flight the button is
// disabled and aria-busy, so a second press cannot double a command and the
// person can see it is working. The caller keys it by what it does (add, remove
// this note), never by its place, so a view update never remounts it mid-press.
import { useState } from "react";
import type { Result } from "../api";

export type ButtonProps = {
  readonly label: string;
  readonly title: string;
  readonly run: () => Promise<Result<unknown>>;
  readonly onResult?: (result: Result<unknown>) => void;
};

const BUTTON =
  "min-h-tap rounded-control border border-accent bg-accent px-3 py-2 text-on-accent " +
  "hover:brightness-110 disabled:cursor-progress disabled:opacity-50";

export function Button({ label, title, run, onResult }: ButtonProps) {
  const [pending, setPending] = useState(false);
  return (
    <button
      type="button"
      title={title}
      aria-busy={pending}
      disabled={pending}
      className={BUTTON}
      onClick={() => {
        setPending(true);
        void run()
          .then((result) => {
            if (onResult !== undefined) onResult(result);
          })
          .finally(() => setPending(false));
      }}
    >
      {label}
    </button>
  );
}

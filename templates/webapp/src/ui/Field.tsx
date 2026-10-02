// One labelled control in a panel. The label is visible and the hint is the
// control's tooltip, so every field a panel draws carries its name and its
// explanation.
import type { ReactNode } from "react";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-small">
      <span className="text-muted" title={hint}>
        {label}
      </span>
      {children}
    </label>
  );
}

// The one panel of the example: the list of notes, a field and a button to add
// one, and a button on each row to remove it. Every control is the shared
// `Button` (src/ui); a refusal comes back as a value and is drawn with the
// shared `Notice`. A panel reads the view only through `useView` and acts only
// through `send`, so it never reaches into the store's shape.
import { useState } from "react";
import type { Result } from "../api";
import type { Answer, Note } from "../contract.gen";
import { roleAtLeast, useRole } from "../role";
import { useView } from "../store";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { Notice } from "../ui/Notice";
import type { PanelProps } from "./panel";

const NO_NOTES: readonly Note[] = [];

export function Notes({ store, send }: PanelProps) {
  const view = useView(store, (value) => value);
  const notes = view?.notes ?? NO_NOTES;
  const { role } = useRole();
  const mayEdit = roleAtLeast(role, "member");
  const [text, setText] = useState("");
  const [refusal, setRefusal] = useState<Result<unknown> | null>(null);

  function settle(result: Result<Answer>): Result<Answer> {
    setRefusal(result.ok ? null : result);
    if (result.ok) store.apply(result.value.notes);
    return result;
  }

  function add(): Promise<Result<Answer>> {
    return send("POST /api/notes", { kind: "add", text }).then((result) => {
      if (result.ok) setText("");
      return settle(result);
    });
  }

  function remove(id: string): Promise<Result<Answer>> {
    return send("POST /api/notes", { kind: "remove", id }).then(settle);
  }

  return (
    <section className="rounded-panel border border-line bg-panel p-pad" aria-label="Notes">
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {notes.map((note) => (
          <li key={note.id} className="flex items-center justify-between gap-2">
            <span className="text-body">{note.text}</span>
            {mayEdit ? (
              <Button
                label="Remove"
                title={`Remove the note ${note.text}`}
                run={() => remove(note.id)}
              />
            ) : null}
          </li>
        ))}
      </ul>
      {mayEdit ? (
        <form
          className="mt-pad flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void add();
          }}
        >
          <Field label="New note" hint="What to remember">
            <input
              className="rounded-control border border-line bg-field px-2 py-1 text-body"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </Field>
          <Button label="Add note" title="Adds the note to the list" run={add} />
        </form>
      ) : (
        <p className="mt-pad text-small text-muted">Ask a member to add notes.</p>
      )}
      <Notice result={refusal} />
    </section>
  );
}

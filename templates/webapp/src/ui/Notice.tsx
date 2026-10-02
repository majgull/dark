// A refusal is a value (see api.ts's Result), and this is the one place the page
// draws one. A refusal with no sentence of its own still says something a person
// can act on. It is a `status`, so a reader hears it when it appears.
import type { Result } from "../api";

const NO_WORDS = "The server refused that.";

export function Notice({ result }: { result: Result<unknown> | null }) {
  if (result === null || result.ok) return null;
  return (
    <p role="status" className="m-0 text-small text-warn">
      {result.message === "" ? NO_WORDS : result.message}
    </p>
  );
}

// The runtime guard the store trusts the view with. It checks the same schema
// the types come from, so a server in another language cannot send a shape the
// page was not compiled for.
import { describe, expect, it } from "vitest";
import { parseView, validate } from "./validate";

const VIEW = { notes: [{ id: "one", text: "Buy milk" }], role: "member" };

describe("parseView", () => {
  it("accepts the whole view and answers its generated type", () => {
    const result = parseView(VIEW);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.notes).toHaveLength(1);
  });

  it("refuses a view missing a required field and names its path", () => {
    const result = parseView({ notes: [] });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.path).toBe("role");
      expect(result.why).toContain("required property");
    }
  });

  it("refuses a field the schema does not name", () => {
    const result = parseView({ ...VIEW, extra: 1 });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.path).toBe("extra");
  });
});

describe("validate", () => {
  it("accepts exactly one branch of a oneOf command", () => {
    expect(validate("NoteCommand", { kind: "add", text: "Buy milk" })).toEqual({ ok: true });
    expect(validate("NoteCommand", { kind: "remove", id: "one" })).toEqual({ ok: true });
    expect(validate("NoteCommand", { kind: "add" }).ok).toBe(false);
    expect(validate("NoteCommand", { kind: "add", text: "x", id: "y" }).ok).toBe(false);
  });
});

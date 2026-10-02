import { describe, expect, it } from "vitest";
import type { NotesView } from "./contract.gen";
import { roleAtLeast, roleFor, roleFromSearch, viewRole } from "./role";

const VIEW: NotesView = { notes: [], role: "member" };

describe("roleFromSearch", () => {
  it("reads a role from the query only in a test build", () => {
    expect(roleFromSearch("?role=listener", true), "a test build reads the role query").toBe(
      "listener",
    );
    expect(roleFromSearch("?role=admin", true), "a test build reads the role query").toBe("admin");
    expect(
      roleFromSearch("?role=listener", false),
      "a non-test build ignores the role query",
    ).toBeNull();
    expect(roleFromSearch("?role=nobody", true), "an unknown role is ignored").toBeNull();
  });
});

describe("roleFor", () => {
  it("lets the test role stand in for the view's role", () => {
    expect(roleFor("?role=listener", true, VIEW)).toBe("listener");
    expect(roleFor("?role=listener", false, VIEW)).toBe("member");
    expect(roleFor("", true, VIEW)).toBe("member");
    expect(roleFor("", false, null)).toBe("listener");
  });
});

describe("roleAtLeast", () => {
  it("ranks listener below member below admin", () => {
    expect(roleAtLeast("member", "member")).toBe(true);
    expect(roleAtLeast("listener", "member")).toBe(false);
    expect(roleAtLeast("admin", "member")).toBe(true);
  });
});

describe("viewRole", () => {
  it("reads the role the view names, and a listener by default", () => {
    expect(viewRole(VIEW)).toBe("member");
    expect(viewRole(null)).toBe("listener");
  });
});

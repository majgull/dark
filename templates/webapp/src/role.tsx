// Roles on the page. The server names a role in the view, and a build started
// for a test (mode "test") may override it with `?role=`; everything else falls
// back to the view. The page only draws what that role may use, and the server
// refuses anyway. `roleFromSearch` is the only place the query is read, and it
// is read only when `isTest` is true, so a production build cannot be talked
// into a role.
import { createContext, type ReactNode, useContext } from "react";
import type { NotesView } from "./contract.gen";

export const ROLE_RANK = { listener: 0, member: 1, admin: 2 } as const;

export type Role = keyof typeof ROLE_RANK;

/** roleAtLeast(role, floor) -> whether the role may use what the floor names. */
export function roleAtLeast(role: Role, floor: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[floor];
}

/** viewRole(view) -> the role the view names; anything else is a listener. */
export function viewRole(view: NotesView | null): Role {
  return view?.role ?? "listener";
}

/** roleFromSearch(search, isTest) -> the role a test asked for, or null. */
export function roleFromSearch(search: string, isTest: boolean): Role | null {
  if (!isTest) return null;
  const value = new URLSearchParams(search).get("role");
  return value === "listener" || value === "member" || value === "admin" ? value : null;
}

/** roleFor(search, isTest, view) -> the role the page draws with. */
export function roleFor(search: string, isTest: boolean, view: NotesView | null): Role {
  return roleFromSearch(search, isTest) ?? viewRole(view);
}

export type RoleState = { readonly role: Role };

const RoleContext = createContext<RoleState>({ role: "listener" });

/** RoleProvider gives the tree the role the shell resolved. */
export function RoleProvider({ role, children }: { role: Role; children: ReactNode }) {
  return <RoleContext.Provider value={{ role }}>{children}</RoleContext.Provider>;
}

/** useRole() -> the role the shell resolved. */
export function useRole(): RoleState {
  return useContext(RoleContext);
}

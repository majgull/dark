// The page's requests to the server. Every route the page may call is typed by
// the generated `Routes` table (contract.gen.ts), so a wrong route name, a
// missing body or a body of the wrong shape is a compile error, not a 404 at
// run time.
//
// A refusal is a value, never a thrown error: `send` and `read` answer
// `Result<T>`, `{ok: true, value}` when the server answered, `{ok: false, status,
// message}` when it refused or could not be reached, which is what a `Notice`
// draws. The POST answers are trusted as the schema types them; the GET view,
// which no HTTP check can cover, is guarded in store.ts.
//
// `read` is typed by the route's `query` and by the `{name}` placeholders of its
// path: a route with a path parameter takes it as a typed object first, and the
// query follows. A route that answers bytes (a file, a download) has `bytes:
// true` in the table and is not a `read` route at all.
import type { Routes } from "./contract.gen";

/** Result<T> -> the server's answer, or the reason there is none. */
export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly status: number; readonly message: string };

/** The GET routes that answer JSON; the routes that answer bytes are left out. */
export type GetRoute = {
  [K in keyof Routes]: K extends `GET ${string}`
    ? Routes[K] extends { readonly bytes: true }
      ? never
      : K
    : never;
}[keyof Routes];

/** A route's query object, or null when it takes none. */
type QueryOf<K extends GetRoute> = Routes[K]["query"];

/** A route's path parameters, or null when its path names no `{name}`. */
type ParamsOf<K extends GetRoute> = Routes[K]["params"];

/** Whether T has a field that must be given (no field of it allows undefined). */
type HasRequired<T> = [keyof T] extends [never]
  ? false
  : { [K in keyof T]-?: undefined extends T[K] ? never : K }[keyof T] extends never
    ? false
    : true;

/** The query argument: absent with no query, required when one field must be given. */
type QueryArgs<Q> = [Q] extends [null]
  ? []
  : HasRequired<Q> extends true
    ? [query: Q]
    : [query?: Q];

/** The arguments `read` takes: its path parameters first, then its query. */
export type ReadArgs<K extends GetRoute> =
  ParamsOf<K> extends null
    ? QueryArgs<QueryOf<K>>
    : QueryOf<K> extends null
      ? [params: ParamsOf<K>]
      : HasRequired<QueryOf<K>> extends true
        ? [params: ParamsOf<K>, query: QueryOf<K>]
        : [params: ParamsOf<K>, query?: QueryOf<K>];

/** send(route, body) -> POST the body, answer with the server's typed response. */
export type Send = <K extends keyof Routes>(
  route: K,
  body: Routes[K]["body"],
) => Promise<Result<Routes[K]["response"]>>;

/** read(route, ...) -> GET a typed read route and answer with its response. */
export type Read = <K extends GetRoute>(
  route: K,
  ...args: ReadArgs<K>
) => Promise<Result<Routes[K]["response"]>>;

export type Api = { readonly send: Send; readonly read: Read };

const UNREACHABLE = "Cannot reach the server. Try again.";
const UNREADABLE = "The server sent an answer the page could not read.";

/**
 * createApi(base, doFetch) -> the page's two calls over one `fetch`. `base` is
 * empty in the page (the server serves it) and `doFetch` is injectable, so a
 * test can stand in its own without a server.
 */
export function createApi(base = "", doFetch: typeof fetch = fetch): Api {
  const send: Send = (route, body) => request("POST", route, body, base, doFetch);
  function read<K extends GetRoute>(
    route: K,
    ...args: ReadArgs<K>
  ): Promise<Result<Routes[K]["response"]>> {
    return request("GET", route, null, base, doFetch, readPath(route, args));
  }
  return { send, read };
}

/** The page's one send, on the page's own server. A test uses createApi. */
export const send: Send = createApi().send;

/** The page's one read, on the page's own server. A test uses createApi. */
export const read: Read = createApi().read;

async function request<K extends keyof Routes>(
  method: "GET" | "POST",
  route: K,
  body: Routes[K]["body"] | null,
  base: string,
  doFetch: typeof fetch,
  urlPath?: string,
): Promise<Result<Routes[K]["response"]>> {
  const init: RequestInit = {
    method,
    headers: { accept: "application/json", "content-type": "application/json" },
  };
  if (method === "POST") init.body = JSON.stringify(body);
  let response: Response;
  try {
    response = await doFetch(base + (urlPath ?? pathOf(route)), init);
  } catch {
    return { ok: false, status: 0, message: UNREACHABLE };
  }
  if (!response.ok) {
    let raw: unknown = null;
    try {
      raw = await response.json();
    } catch {
      raw = null;
    }
    return { ok: false, status: response.status, message: messageOf(raw) };
  }
  let value: Routes[K]["response"];
  try {
    value = await response.json();
  } catch {
    return { ok: false, status: response.status, message: UNREADABLE };
  }
  return { ok: true, value };
}

/** pathOf("POST /api/notes") -> "/api/notes". */
function pathOf(route: string): string {
  const space = route.indexOf(" ");
  return space < 0 ? route : route.slice(space + 1);
}

/**
 * readPath(route, args) -> the route's path with its captured `{name}` values
 * filled in, then the query string. The args are typed by the route's
 * `ReadArgs`; here they are the runtime values.
 */
function readPath(route: string, args: readonly unknown[]): string {
  const path = pathOf(route);
  if (!path.includes("{")) return path + querySuffix(args[0]);
  return fillParams(path, args[0]) + querySuffix(args[1]);
}

/** fillParams("/api/notes/{id}", {id}) -> "/api/notes/one". */
function fillParams(path: string, raw: unknown): string {
  if (!isRecord(raw)) return path;
  return path.replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (found) => {
    const name = found.slice(1, -1);
    return encodeURIComponent(stringOf(raw[name]));
  });
}

/** querySuffix({find, limit}) -> "?find=one&limit=5", an empty string with none. */
function querySuffix(raw: unknown): string {
  if (!isRecord(raw)) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(raw)) {
    if (value === undefined || value === null) continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(stringOf(value))}`);
  }
  return parts.length > 0 ? `?${parts.join("&")}` : "";
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null;
}

function stringOf(value: unknown): string {
  return String(value);
}

function messageOf(value: unknown): string {
  if (typeof value !== "object" || value === null || !("message" in value)) return "";
  const said = value.message;
  return typeof said === "string" ? said : "";
}

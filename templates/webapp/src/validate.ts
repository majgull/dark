// One validator for the subset of JSON Schema that contract/app.schema.json
// uses: `$ref` into `#/$defs`, `type` (a name or a list, so nullable by type
// list), `properties`, `required`, `additionalProperties` (false or a schema),
// `items`, `enum` and `oneOf`. It mirrors what a server in another language
// would check, so the page and the server accept and refuse the same values.
//
// The defs come from contract.schema.gen.ts, which tools/gen_types.py writes
// from the schema, so a schema change moves the guard with it. The store calls
// parseView, not validate: a valid answer is returned as its generated type, and
// a refused one carries the path and the reason, which the store draws with
// `Notice`. The one `as` cast in the page is in parse, after the value has been
// checked.

import type { NotesView } from "./contract.gen";
import { DEFS } from "./contract.schema.gen";

/** A refusal: where the value failed (`notes[0].text`) and why. */
export type Validation =
  | { readonly ok: true }
  | { readonly ok: false; readonly path: string; readonly why: string };

/** Result<T> -> a checked value, or the path and reason a value was refused. */
export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly path: string; readonly why: string };

type Problem = { readonly path: string; readonly why: string };

const TABLE: Readonly<Record<string, unknown>> = DEFS;
const PREFIX = "#/$defs/";
const TYPE_NAMES = new Set(["object", "array", "string", "number", "integer", "boolean", "null"]);

/** validate(defName, value) -> whether value fits `$defs/<defName>`. */
export function validate(defName: string, value: unknown): Validation {
  const first = checkNode({ $ref: `${PREFIX}${defName}` }, value, "")[0];
  if (first === undefined) return { ok: true };
  return { ok: false, path: first.path, why: first.why };
}

/** parseView(value) -> the whole view, or the path and reason it was refused. */
export function parseView(value: unknown): Result<NotesView> {
  return parse<NotesView>("NotesView", value);
}

function parse<T>(defName: string, value: unknown): Result<T> {
  const verdict = validate(defName, value);
  if (!verdict.ok) return verdict;
  return { ok: true, value: value as T };
}

/** checkNode(node, value, path) -> every way value fails the node; empty when it fits. */
function checkNode(node: unknown, value: unknown, path: string): Problem[] {
  if (!isRecord(node)) return [{ path, why: "the schema node is not an object" }];
  if ("$ref" in node) {
    const target = resolve(node.$ref);
    if (target === null) {
      return [
        {
          path,
          why: `no $defs entry named ${JSON.stringify(String(node.$ref).slice(PREFIX.length))}`,
        },
      ];
    }
    return checkNode(target, value, path);
  }
  if ("oneOf" in node) return checkOneOf(node.oneOf, value, path);
  if ("enum" in node) {
    const options = node.enum;
    if (!Array.isArray(options) || !options.includes(value)) {
      return [{ path, why: `${JSON.stringify(value)} is not one of ${JSON.stringify(options)}` }];
    }
    return [];
  }
  const problems = checkType(node.type, value, path);
  if (problems.length > 0) return problems;
  if (isRecord(value)) problems.push(...checkObject(node, value, path));
  if (Array.isArray(value)) problems.push(...checkArray(node, value, path));
  return problems;
}

/** checkOneOf(branches, value, path) -> exactly one branch must fit. */
function checkOneOf(branches: unknown, value: unknown, path: string): Problem[] {
  if (!Array.isArray(branches)) return [{ path, why: "oneOf is not a list" }];
  let passing = 0;
  for (const branch of branches) {
    if (checkNode(branch, value, path).length === 0) passing += 1;
  }
  if (passing !== 1) {
    return [{ path, why: `matches ${passing} of ${branches.length} oneOf branches, not one` }];
  }
  return [];
}

/** checkType(types, value, path) -> empty when the value has one of the names. */
function checkType(types: unknown, value: unknown, path: string): Problem[] {
  if (types === undefined) return [];
  const names = Array.isArray(types) ? types : [types];
  if (names.some((name) => isType(value, name))) return [];
  return [{ path, why: `${JSON.stringify(value)} is not of type ${JSON.stringify(types)}` }];
}

/** checkObject(node, value, path) -> the required, the types and the extras. */
function checkObject(
  node: Readonly<Record<string, unknown>>,
  value: Record<string, unknown>,
  path: string,
): Problem[] {
  const known = isRecord(node.properties) ? node.properties : {};
  const required = Array.isArray(node.required) ? node.required : [];
  const extra = node.additionalProperties ?? true;
  const problems: Problem[] = [];
  for (const key of required) {
    if (typeof key === "string" && !Object.hasOwn(value, key)) {
      problems.push({
        path: child(path, key),
        why: `required property ${JSON.stringify(key)} is missing`,
      });
    }
  }
  for (const [key, item] of Object.entries(value)) {
    const childPath = child(path, key);
    if (Object.hasOwn(known, key)) {
      problems.push(...checkNode(known[key], item, childPath));
    } else if (extra === false) {
      problems.push({ path: childPath, why: "is not a property of this object" });
    } else if (isRecord(extra)) {
      problems.push(...checkNode(extra, item, childPath));
    }
  }
  return problems;
}

/** checkArray(node, value, path) -> every item against the items schema. */
function checkArray(
  node: Readonly<Record<string, unknown>>,
  value: unknown[],
  path: string,
): Problem[] {
  const items = node.items;
  if (!isRecord(items)) return [];
  const problems: Problem[] = [];
  value.forEach((item, index) => {
    problems.push(...checkNode(items, item, indexPath(path, index)));
  });
  return problems;
}

/** resolve(ref) -> the `#/$defs/<name>` schema, or null when it names none. */
function resolve(ref: unknown): unknown | null {
  const text = String(ref);
  if (!text.startsWith(PREFIX)) return null;
  const name = text.slice(PREFIX.length);
  return Object.hasOwn(TABLE, name) ? TABLE[name] : null;
}

/** isType(value, name) -> whether the value is of the JSON Schema type. */
function isType(value: unknown, name: unknown): boolean {
  if (typeof name !== "string" || !TYPE_NAMES.has(name)) return false;
  switch (name) {
    case "object":
      return isRecord(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "boolean":
      return typeof value === "boolean";
    case "number":
      return typeof value === "number";
    case "integer":
      return Number.isInteger(value);
    case "null":
      return value === null;
    default:
      return false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** child(path, key) -> the dotted path of an object field. */
function child(path: string, key: string): string {
  return path === "" ? key : `${path}.${key}`;
}

/** indexPath(path, index) -> the bracketed path of an array item. */
function indexPath(path: string, index: number): string {
  return `${path}[${index}]`;
}

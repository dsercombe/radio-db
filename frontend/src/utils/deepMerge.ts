type JsonValue = null | boolean | number | string | JsonObject | JsonArray;
type JsonObject = { [key: string]: JsonValue };
type JsonArray = JsonValue[];

function isPlainObject(value: unknown): value is JsonObject {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/**
 * Deep merge `patch` onto `base` without mutating either.
 * - objects: recursive merge
 * - arrays: replaced (not merged)
 * - primitives: patch wins
 */
export function deepMerge<T extends JsonValue>(base: T, patch: T): T {
  if (patch === undefined) {
    return base;
  }
  if (Array.isArray(base) && Array.isArray(patch)) {
    return patch as T;
  }
  if (isPlainObject(base) && isPlainObject(patch)) {
    const out: JsonObject = { ...(base as JsonObject) };
    Object.keys(patch).forEach((key) => {
      const b = (base as JsonObject)[key];
      const p = (patch as JsonObject)[key];
      out[key] = deepMerge(b as any, p as any) as any;
    });
    return out as T;
  }
  return patch as T;
}


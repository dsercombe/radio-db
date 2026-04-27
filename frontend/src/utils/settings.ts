export type SettingsValue = string | number | boolean | null | Record<string, unknown> | unknown[];

export interface SettingFieldInfo {
  path: string;
  displayName: string;
  tooltip?: string;
}

const ALLOWED_ROOTS = new Set(["scheduler", "strategy", "strategy_pipeline", "kraken"]);

function titleize(segment: string): string {
  if (!segment) return "";
  if (/^\d+$/.test(segment)) {
    return `Item ${Number(segment) + 1}`;
  }
  return segment
    .replace(/\[\]/g, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function generalizePath(path: string): string {
  return path.replace(/\.\d+/g, ".[]");
}

function shouldInclude(pathParts: string[]): boolean {
  if (pathParts.length === 0) return false;
  const root = pathParts[0];
  if (root === "scheduler") {
    return pathParts.length === 2;
  }
  if (root === "strategy") {
    return pathParts.length === 2;
  }
  if (root === "strategy_pipeline") {
    if (pathParts.length === 3 && pathParts[2] === "enabled") {
      return true;
    }
    const paramsIdx = pathParts.indexOf("params");
    return paramsIdx !== -1 && pathParts.length > paramsIdx + 1;
  }
  if (root === "kraken") {
    return pathParts.length === 2 && pathParts[1] === "demo_execution_mode";
  }
  return false;
}

function getGroupLabel(root: string, alias?: string): string {
  if (root === "scheduler") return "Scheduler";
  if (root === "strategy") return "Strategy";
  if (root === "strategy_pipeline") {
    return alias ? `Strategy · ${alias}` : "Strategy · Pipeline";
  }
  if (root === "kraken") {
    return "Kraken";
  }
  return titleize(root);
}

export function getNestedValue(source: Record<string, unknown> | null, path: string): SettingsValue {
  if (!source) return undefined;
  return path.split(".").reduce<SettingsValue>((acc, segment) => {
    if (acc === null || typeof acc !== "object") {
      return undefined;
    }
    return (acc as Record<string, unknown>)[segment] as SettingsValue;
  }, source as SettingsValue);
}

export function setNestedValueImmutable(
  source: Record<string, unknown> | null,
  path: string,
  value: SettingsValue
): Record<string, unknown> {
  const base = source ?? {};
  const clone: Record<string, unknown> = Array.isArray(base) ? [...base] : { ...base };
  const segments = path.split(".");
  let cursorOriginal: SettingsValue = base;
  let cursorClone: Record<string, unknown> | unknown[] = clone;

  segments.forEach((segment, index) => {
    const isLeaf = index === segments.length - 1;
    if (isLeaf) {
      if (Array.isArray(cursorClone)) {
        (cursorClone as unknown[])[Number(segment)] = value;
      } else {
        (cursorClone as Record<string, unknown>)[segment] = value as unknown;
      }
      return;
    }
    const nextOriginal =
      cursorOriginal && typeof cursorOriginal === "object"
        ? (cursorOriginal as Record<string, unknown>)[segment]
        : undefined;
    let nextClone: Record<string, unknown> | unknown[];
    if (Array.isArray(nextOriginal)) {
      nextClone = [...nextOriginal];
    } else if (nextOriginal && typeof nextOriginal === "object") {
      nextClone = { ...(nextOriginal as Record<string, unknown>) };
    } else {
      nextClone = {};
    }
    if (Array.isArray(cursorClone)) {
      (cursorClone as unknown[])[Number(segment)] = nextClone;
    } else {
      (cursorClone as Record<string, unknown>)[segment] = nextClone;
    }
    cursorOriginal = nextOriginal as SettingsValue;
    cursorClone = nextClone;
  });
  return clone;
}

export function extractEditableFields(
  settings: Record<string, unknown> | null,
  tooltips: Record<string, string> = {}
): SettingFieldInfo[] {
  if (!settings) return [];
  const result: SettingFieldInfo[] = [];

  const visit = (
    value: SettingsValue,
    pathParts: string[],
    context: { alias?: string } = {}
  ): void => {
    if (pathParts.length === 0) return;
    const root = pathParts[0];
    if (!ALLOWED_ROOTS.has(root)) {
      return;
    }

    if (
      value === null ||
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      if (!shouldInclude(pathParts)) {
        return;
      }
      const label = titleize(pathParts[pathParts.length - 1] ?? "");
      const groupAlias =
        root === "strategy_pipeline" && context.alias ? context.alias : undefined;
      const displayName =
        root === "strategy_pipeline" && groupAlias
          ? `${getGroupLabel(root, groupAlias)} · ${label}`
          : `${getGroupLabel(root, undefined)} · ${label}`;
      const path = pathParts.join(".");
      const generalized = generalizePath(path);
      const tooltip = tooltips[path] ?? tooltips[generalized];
      result.push({
        path,
        displayName,
        tooltip
      });
      return;
    }

    if (Array.isArray(value)) {
      value.forEach((item, index) => {
        const newPath = [...pathParts, String(index)];
        if (
          pathParts.length === 1 &&
          pathParts[0] === "strategy_pipeline" &&
          item &&
          typeof item === "object"
        ) {
          const entry = item as Record<string, unknown>;
          const params = entry["params"];
          const alias =
            (params && typeof params === "object" && (params as Record<string, unknown>).alias
              ? String((params as Record<string, unknown>).alias)
              : undefined) ?? (entry["alias"] ? String(entry["alias"]) : undefined);
          visit(item as SettingsValue, newPath, { alias });
        } else {
          visit(item as SettingsValue, newPath, context);
        }
      });
      return;
    }

    if (value && typeof value === "object") {
      if (
        root === "strategy" &&
        pathParts.length === 2 &&
        pathParts[1] === "entry_tightness_baselines"
      ) {
        const label = titleize(pathParts[pathParts.length - 1] ?? "");
        const displayName = `${getGroupLabel(root, undefined)} · ${label}`;
        const path = pathParts.join(".");
        const generalized = generalizePath(path);
        const tooltip = tooltips[path] ?? tooltips[generalized];
        result.push({
          path,
          displayName,
          tooltip
        });
        return;
      }
      Object.entries(value as Record<string, unknown>).forEach(([key, nested]) => {
        visit(nested as SettingsValue, [...pathParts, key], context);
      });
    }
  };

  Object.entries(settings).forEach(([key, value]) => {
    if (!ALLOWED_ROOTS.has(key)) {
      return;
    }
    visit(value as SettingsValue, [key]);
  });

  result.sort((a, b) => a.displayName.localeCompare(b.displayName));
  return result;
}

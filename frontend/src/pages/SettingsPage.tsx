import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  fetchCurrentSettings,
  fetchCurrentDataHealth,
  fetchSettingsSchema,
  fetchTemplates,
  fetchVisibleSettingsFields,
  updateVisibleSettingsFields,
  fetchSshAllowlist,
  updateSshAllowlist,
  fetchModeState,
  setGlobalModeAuto,
  forceAllTradeModesToTest,
  setManualEnabled,
  setManualMode,
  setAutoEnabled,
  updateModeControlConfig,
  updateAdaptiveEntryOverride,
  updateDisabledBranch,
  fetchCurrentStrategyStats,
  fetchCpuStats,
  deploySettings,
  type TemplateMeta,
  type ModeControlConfig,
  type ModeThresholdsConfig,
  type ModeStateSnapshot,
  type ModePathState,
  type RuntimeStrategyStat,
  type CpuStats,
  type SshAllowlistResponse,
  type DataHealthResponse
} from "../api/client";
import { extractEditableFields, setNestedValueImmutable } from "../utils/settings";

interface SchemaNode {
  name: string;
  path: string;
  description?: string;
}

function flattenSchema(schema: Record<string, unknown>, prefix = ""): SchemaNode[] {
  const nodes: SchemaNode[] = [];
  const properties = (schema.properties ?? {}) as Record<string, Record<string, unknown>>;
  for (const [key, value] of Object.entries(properties)) {
    const path = prefix ? `${prefix}.${key}` : key;
    const title = (value.title as string) ?? key;
    nodes.push({ name: title, path });
    if (value.type === "object" && value.properties) {
      nodes.push(...flattenSchema(value as Record<string, unknown>, path));
    }
  }
  return nodes;
}

function numberOrFallback(value: unknown, fallback: number, { round = false }: { round?: boolean } = {}): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return round ? Math.round(value) : value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (!Number.isNaN(parsed)) {
      return round ? Math.round(parsed) : parsed;
    }
  }
  return fallback;
}

function booleanOrFallback(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off"].includes(normalized)) return false;
  }
  return fallback;
}

function extractModeControlConfig(settings: Record<string, unknown> | null): ModeControlConfig {
  const strategyCfg = (settings?.strategy ?? {}) as Record<string, unknown>;
  const raw = (strategyCfg?.mode_control ?? {}) as Record<string, unknown>;
  const thresholdsRaw = (raw?.thresholds ?? {}) as Record<string, unknown>;
  const config: ModeControlConfig = {
    enabled: booleanOrFallback(raw?.enabled, true),
    default_mode: raw?.default_mode === "test" ? "test" : "live",
    auto_enabled_by_default: booleanOrFallback(raw?.auto_enabled_by_default, true),
    thresholds: {
      cooldown_minutes: Math.max(0, numberOrFallback(thresholdsRaw?.cooldown_minutes, 2, { round: true })),
      max_auto_switches_per_day: Math.max(0, numberOrFallback(thresholdsRaw?.max_auto_switches_per_day, 12, { round: true })),
      ema_period: Math.max(1, numberOrFallback(thresholdsRaw?.ema_period, 1, { round: true })),
      ema_threshold_pct: numberOrFallback(thresholdsRaw?.ema_threshold_pct, 0.05),
      noise_threshold_pct: numberOrFallback(thresholdsRaw?.noise_threshold_pct, 0.05),
      lookback_trades: Math.max(1, numberOrFallback(thresholdsRaw?.lookback_trades, 3, { round: true }))
    }
  };
  return config;
}

function extractAdaptiveEntryConfig(settings: Record<string, unknown> | null): {
  globalEnabled: boolean;
  overrides: Record<string, boolean>;
} {
  const strategyCfg = (settings?.strategy ?? {}) as Record<string, unknown>;
  const globalEnabled = booleanOrFallback(strategyCfg?.adaptive_entry_enabled, true);
  const overridesRaw = strategyCfg?.adaptive_entry_overrides;
  const overrides: Record<string, boolean> = {};
  if (overridesRaw && typeof overridesRaw === "object") {
    Object.entries(overridesRaw as Record<string, unknown>).forEach(([key, value]) => {
      if (typeof value === "boolean") {
        overrides[key] = value;
      }
    });
  }
  return { globalEnabled, overrides };
}

function extractDisabledBranches(settings: Record<string, unknown> | null): Set<string> {
  const disabled = new Set<string>();
  const strategyCfg = (settings?.strategy ?? {}) as Record<string, unknown>;
  const values = strategyCfg?.disabled_branches;
  const pushKey = (value: string) => {
    if (!value) return;
    const parts = value.split("|");
    if (parts.length !== 3) return;
    const directionRaw = parts[2].trim().toLowerCase();
    const direction = directionRaw === "short" || directionRaw === "sell" ? "short" : "long";
    disabled.add(`${parts[0].trim().toLowerCase()}|${parts[1].trim().toUpperCase()}|${direction}`);
  };
  if (Array.isArray(values)) {
    values.forEach((value) => {
      if (typeof value === "string") pushKey(value);
    });
  } else if (values && typeof values === "object") {
    Object.entries(values as Record<string, unknown>).forEach(([key, value]) => {
      if (value === true) pushKey(key);
    });
  } else if (typeof values === "string") {
    pushKey(values);
  }
  return disabled;
}

function extractNoMcBranches(settings: Record<string, unknown> | null): Set<string> {
  const marked = new Set<string>();
  const strategyCfg = (settings?.strategy ?? {}) as Record<string, unknown>;
  const values = strategyCfg?.nomc_branches;
  const pushKey = (value: string) => {
    if (!value) return;
    const parts = value.split("|");
    if (parts.length !== 3) return;
    const directionRaw = parts[2].trim().toLowerCase();
    const direction = directionRaw === "short" || directionRaw === "sell" ? "short" : "long";
    marked.add(`${parts[0].trim().toLowerCase()}|${parts[1].trim().toUpperCase()}|${direction}`);
  };
  if (Array.isArray(values)) {
    values.forEach((value) => {
      if (typeof value === "string") pushKey(value);
    });
  } else if (typeof values === "string") {
    pushKey(values);
  }
  return marked;
}

interface StrategyVariant {
  strategy: string;
  symbol: string;
}

function extractStrategyVariants(settings: Record<string, unknown> | null): StrategyVariant[] {
  const variants: StrategyVariant[] = [];
  if (!settings) return variants;
  const pipeline = Array.isArray(settings.strategy_pipeline)
    ? (settings.strategy_pipeline as unknown[])
    : [];
  pipeline.forEach((entry) => {
    if (!entry || typeof entry !== "object") return;
    const definition = entry as Record<string, unknown>;
    const enabled = definition.enabled !== false;
    if (!enabled) return;
    const name = typeof definition.name === "string" ? definition.name : "strategy";
    const baseParams = (definition.params ?? {}) as Record<string, unknown>;
    const baseAlias =
      typeof baseParams.alias === "string"
        ? baseParams.alias
        : typeof baseParams.name_override === "string"
          ? baseParams.name_override
          : typeof baseParams.label === "string"
            ? baseParams.label
            : undefined;
    const perSymbolRaw = (definition.per_symbol_params ?? {}) as Record<string, unknown>;
    let symbols: string[] = [];
    if (Array.isArray(definition.symbols)) {
      symbols = (definition.symbols as unknown[]).map((sym) => String(sym));
    }
    if (!symbols.length) {
      symbols = Object.keys(perSymbolRaw);
    }
    if (!symbols.length) {
      return;
    }
    const multipleSymbols = symbols.length > 1;
    symbols.forEach((symbol) => {
      const symbolParamsRaw = perSymbolRaw?.[symbol];
      const symbolParams =
        symbolParamsRaw && typeof symbolParamsRaw === "object"
          ? (symbolParamsRaw as Record<string, unknown>)
          : {};
      const aliasOverride =
        typeof symbolParams.alias === "string" ? symbolParams.alias : undefined;
      let alias: string;
      if (aliasOverride) {
        alias = aliasOverride;
      } else if (baseAlias) {
        alias = multipleSymbols ? `${baseAlias}_${symbol.toLowerCase()}` : baseAlias;
      } else {
        alias = `${name}_${symbol.toLowerCase()}`;
      }
      variants.push({ strategy: alias, symbol });
    });
  });
  return variants;
}

interface ModePathRow {
  key: string;
  strategy: string;
  symbol: string;
  direction: "long" | "short";
  state: ModePathState;
  stats?: RuntimeStrategyStat;
}

const THRESHOLD_FIELD_META: Array<{
  key: keyof ModeThresholdsConfig;
  label: string;
  step?: string;
  hint?: string;
}> = [
  // EMA-basierte Switch-Thresholds (Haupt-Logik für Auto-Switching)
  { key: "ema_period", label: "EMA Periode (Trades)", step: "1", hint: "Anzahl Trades für EMA-Berechnung (je kleiner, desto sensibler)" },
  { key: "ema_threshold_pct", label: "EMA Threshold (%)", step: "0.1", hint: "Abstand zum EMA für Switch (LIVE wenn > EMA + Threshold, TEST wenn < EMA - Threshold)" },
  { key: "noise_threshold_pct", label: "Noise Threshold (%)", step: "0.1", hint: "Trades unter diesem Wert werden für EMA ignoriert" },
  { key: "lookback_trades", label: "Lookback Trades", step: "1", hint: "Anzahl Trades die für Analyse verwendet werden" },
  // Cooldown & Limits
  { key: "cooldown_minutes", label: "Cooldown (Minuten)", step: "1", hint: "Min. Zeit zwischen Auto-Switches" },
  { key: "max_auto_switches_per_day", label: "Max Switches/Tag", step: "1", hint: "Maximale Anzahl Auto-Switches pro Tag" }
];

export function SettingsPage(): JSX.Element {
  const [schemaNodes, setSchemaNodes] = useState<SchemaNode[]>([]);
  const [tooltips, setTooltips] = useState<Record<string, string>>({});
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [currentSettings, setCurrentSettings] = useState<Record<string, unknown> | null>(null);
  const [hiddenFields, setHiddenFields] = useState<Set<string> | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [visibilitySaving, setVisibilitySaving] = useState<boolean>(false);
  const [visibilityError, setVisibilityError] = useState<string | null>(null);
  const [visibilitySearch, setVisibilitySearch] = useState<string>("");
  const [modeConfig, setModeConfig] = useState<ModeControlConfig | null>(null);
  const [modeConfigDraft, setModeConfigDraft] = useState<ModeControlConfig | null>(null);
  const [modeState, setModeState] = useState<ModeStateSnapshot | null>(null);
  const [cpuStats, setCpuStats] = useState<CpuStats | null>(null);
  const [cpuStatsError, setCpuStatsError] = useState<string | null>(null);
  const [modeStats, setModeStats] = useState<RuntimeStrategyStat[]>([]);
  const [modeLoading, setModeLoading] = useState<boolean>(true);
  const [modeError, setModeError] = useState<string | null>(null);
  const [modeMessage, setModeMessage] = useState<string | null>(null);
  const [modeSaving, setModeSaving] = useState<boolean>(false);
  const [configDirty, setConfigDirty] = useState<boolean>(false);
  const [globalAutoSaving, setGlobalAutoSaving] = useState<boolean>(false);
  const [forceTestSaving, setForceTestSaving] = useState<boolean>(false);
  const [pathSaving, setPathSaving] = useState<Record<string, boolean>>({});
  const [adaptiveMessage, setAdaptiveMessage] = useState<string | null>(null);
  const [adaptiveError, setAdaptiveError] = useState<string | null>(null);
  const [adaptiveSaving, setAdaptiveSaving] = useState<Record<string, boolean>>({});
  const [disabledMessage, setDisabledMessage] = useState<string | null>(null);
  const [disabledError, setDisabledError] = useState<string | null>(null);
  const [disabledSaving, setDisabledSaving] = useState<Record<string, boolean>>({});
  const [sshAllowlist, setSshAllowlist] = useState<SshAllowlistResponse | null>(null);
  const [sshAllowlistDraft, setSshAllowlistDraft] = useState<string>("");
  const [sshAllowlistLoading, setSshAllowlistLoading] = useState<boolean>(false);
  const [sshAllowlistSaving, setSshAllowlistSaving] = useState<boolean>(false);
  const [sshAllowlistError, setSshAllowlistError] = useState<string | null>(null);
  const [sshAllowlistMessage, setSshAllowlistMessage] = useState<string | null>(null);
  const [binanceFeeSaving, setBinanceFeeSaving] = useState<boolean>(false);
  const [binanceFeeMessage, setBinanceFeeMessage] = useState<string | null>(null);
  const [binanceFeeError, setBinanceFeeError] = useState<string | null>(null);
  const [dataHealth, setDataHealth] = useState<DataHealthResponse | null>(null);
  const [dataHealthError, setDataHealthError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const [schemaResponse, templateResponse, currentSettingsResponse, hiddenResponse, sshResponse] = await Promise.all([
          fetchSettingsSchema(),
          fetchTemplates(),
          fetchCurrentSettings(),
          fetchVisibleSettingsFields(),
          fetchSshAllowlist()
        ]);
        const settingsPayload = currentSettingsResponse.settings as Record<string, unknown>;
        setTooltips(schemaResponse.tooltips ?? {});
        setSchemaNodes(flattenSchema(schemaResponse.schema));
        setTemplates(templateResponse.items);
        setCurrentSettings(settingsPayload);
        setHiddenFields(new Set(hiddenResponse ?? []));
        const config = extractModeControlConfig(settingsPayload);
        setModeConfig(config);
        setModeConfigDraft(config);
        setConfigDirty(false);
        setSshAllowlist(sshResponse);
        setSshAllowlistDraft((sshResponse.ips ?? []).join("\n"));
      } catch (err) {
        console.error(err);
        setError("Settings-Daten konnten nicht geladen werden.");
      } finally {
        setLoading(false);
      }
      setModeLoading(true);
      try {
        const [modeSnapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
        setModeState(modeSnapshot);
        setModeStats(stats);
        setModeError(null);
      } catch (err) {
        console.error(err);
        setModeError("Mode-Controller-Daten konnten nicht geladen werden.");
      } finally {
        setModeLoading(false);
      }
    };
    load();
  }, []);

  // Load Mode State and Stats periodically (to catch auto-switches)
  useEffect(() => {
    const loadModeState = async () => {
      try {
        const [modeSnapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
        setModeState(modeSnapshot);
        setModeStats(stats);
        setModeError(null);
      } catch (err) {
        console.error("Failed to refresh Mode State:", err);
        // Don't set modeError on polling failures to avoid overwriting initial load errors
      }
    };
    // Initial load already happened in previous useEffect, so start polling after a delay
    const interval = setInterval(loadModeState, 10000); // Refresh every 10 seconds
    return () => clearInterval(interval);
  }, []);

  // Load CPU stats periodically
  useEffect(() => {
    const loadCpuStats = async () => {
      try {
        const stats = await fetchCpuStats();
        setCpuStats(stats);
        setCpuStatsError(null);
      } catch (err) {
        console.error("Failed to load CPU stats:", err);
        setCpuStatsError(err instanceof Error ? err.message : "Fehler beim Laden der CPU-Statistiken");
        // Set empty stats on error so UI can show error message
        setCpuStats({
          system: { cpu_percent: 0.0, load_avg: [0.0, 0.0, 0.0], cpu_count: 1 },
          processes: [],
          error: err instanceof Error ? err.message : "Unbekannter Fehler",
        });
      }
    };
    loadCpuStats();
    const interval = setInterval(loadCpuStats, 5000); // Refresh every 5 seconds
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const loadHealth = async () => {
      try {
        const data = await fetchCurrentDataHealth();
        setDataHealth(data);
        setDataHealthError(null);
      } catch (err) {
        console.error("Data health fetch failed:", err);
        setDataHealthError("Data-Health-Daten konnten nicht geladen werden.");
      }
    };
    void loadHealth();
    const interval = setInterval(loadHealth, 30_000);
    return () => clearInterval(interval);
  }, []);

  const groupedSchema = useMemo(() => {
    const map: Record<string, SchemaNode[]> = {};
    schemaNodes.forEach((node) => {
      const group = node.path.split(".")[0];
      map[group] = map[group] ?? [];
      map[group].push(node);
    });
    return map;
  }, [schemaNodes]);

  const settingsFields = useMemo(
    () => extractEditableFields(currentSettings, tooltips),
    [currentSettings, tooltips]
  );

  const filteredVisibilityFields = useMemo(() => {
    if (!visibilitySearch.trim()) return settingsFields;
    const needle = visibilitySearch.trim().toLowerCase();
    return settingsFields.filter(
      (field) =>
        field.displayName.toLowerCase().includes(needle) || field.path.toLowerCase().includes(needle)
    );
  }, [settingsFields, visibilitySearch]);

  const statsSummary = useMemo(() => {
    if (!modeStats.length) return null;
    const totalTrades = modeStats.reduce((acc, stat) => acc + (stat.trades ?? 0), 0);
    const totalWins = modeStats.reduce((acc, stat) => acc + (stat.wins ?? 0), 0);
    const totalPnl = modeStats.reduce((acc, stat) => acc + (stat.pnl ?? 0), 0);
    const winRate = totalTrades ? totalWins / totalTrades : 0;
    const pnlPerTrade = totalTrades ? totalPnl / totalTrades : 0;
    return { totalTrades, totalWins, totalPnl, winRate, pnlPerTrade };
  }, [modeStats]);

  const adaptiveEntryConfig = useMemo(
    () => extractAdaptiveEntryConfig(currentSettings),
    [currentSettings]
  );
  const disabledBranches = useMemo(
    () => extractDisabledBranches(currentSettings),
    [currentSettings]
  );
  const noMcBranches = useMemo(
    () => extractNoMcBranches(currentSettings),
    [currentSettings]
  );

  const adaptiveEntryKey = (strategy: string, symbol: string, direction: "long" | "short") =>
    `${strategy}|${symbol.toUpperCase()}|${direction}`;

  const pathRows = useMemo<ModePathRow[]>(() => {
    if (!modeConfigDraft) return [];
    const currentPaths = modeState?.paths ?? {};
    const statsByKey = new Map<string, RuntimeStrategyStat>();
    modeStats.forEach((stat) => {
      const dir = stat.direction?.toLowerCase() === "short" ? "short" : "long";
      const key = `${stat.strategy}|${stat.symbol}|${dir}`;
      statsByKey.set(key, { ...stat, direction: dir });
    });
    const map = new Map<string, ModePathRow>();
    const ensureRow = (strategy: string, symbol: string, direction: "long" | "short") => {
      const key = `${strategy}|${symbol}|${direction}`;
      if (map.has(key)) {
        return map.get(key)!;
      }
      const existing = currentPaths[key];
      const state: ModePathState = existing
        ? existing
        : {
            current_mode: modeConfigDraft.default_mode,
            manual_enabled: true,
            manual_mode: null,
            auto_enabled: modeConfigDraft.auto_enabled_by_default,
            last_switch_reason: null,
            last_switch_at: null,
            cooldown_until: null,
            auto_switch_count_date: null,
            auto_switch_count: 0,
            degraded_reason: null
          };
      const row: ModePathRow = {
        key,
        strategy,
        symbol,
        direction,
        state,
        stats: statsByKey.get(key)
      };
      map.set(key, row);
      return row;
    };

    // First, create rows for all strategy variants from settings
    extractStrategyVariants(currentSettings).forEach((variant) => {
      ensureRow(variant.strategy, variant.symbol, "long");
      ensureRow(variant.strategy, variant.symbol, "short");
    });

    // Then, update rows with data from mode state (this will overwrite defaults)
    Object.entries(currentPaths).forEach(([key, state]) => {
      const [strategy, symbol, dirRaw] = key.split("|");
      if (!strategy || !symbol || !dirRaw) return;
      const direction = dirRaw.toLowerCase() === "short" ? "short" : "long";
      const row = ensureRow(strategy, symbol, direction as "long" | "short");
      // Deep copy the state to ensure all properties are updated
      // WICHTIG: Wenn manual_mode gesetzt ist, sollte current_mode auf manual_mode gesetzt werden
      const finalState = { ...row.state, ...state };
      if (finalState.manual_mode && finalState.manual_mode !== finalState.current_mode) {
        // Wenn manual_mode gesetzt ist, aber current_mode nicht übereinstimmt, setze current_mode auf manual_mode
        finalState.current_mode = finalState.manual_mode;
      }
      row.state = finalState;
      row.stats = statsByKey.get(key) ?? row.stats;
    });

    // Finally, add stats to rows
    modeStats.forEach((stat) => {
      const direction = stat.direction?.toLowerCase() === "short" ? "short" : "long";
      const row = ensureRow(stat.strategy, stat.symbol, direction as "long" | "short");
      row.stats = stat;
    });

    return Array.from(map.values()).sort((a, b) => {
      if (a.strategy !== b.strategy) return a.strategy.localeCompare(b.strategy);
      if (a.symbol !== b.symbol) return a.symbol.localeCompare(b.symbol);
      if (a.direction === b.direction) return 0;
      return a.direction === "long" ? -1 : 1;
    });
  }, [modeState, modeStats, modeConfigDraft, currentSettings]);

  const applyHiddenFields = async (nextHidden: string[], previousHidden: string[]) => {
    setVisibilitySaving(true);
    try {
      await updateVisibleSettingsFields(nextHidden);
      setHiddenFields(new Set(nextHidden));
      setVisibilityError(null);
    } catch (err) {
      console.error(err);
      setVisibilityError("Filter konnte nicht gespeichert werden.");
      setHiddenFields(new Set(previousHidden));
    } finally {
      setVisibilitySaving(false);
    }
  };

  const formatPercent = (value?: number) => (value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);
  const formatNumber = (value?: number) => (value === undefined ? "—" : value.toFixed(2));
  const formatTimestamp = (value?: string | null) => (value ? new Date(value).toLocaleString() : "—");

  const handleSshAllowlistRefresh = async () => {
    setSshAllowlistLoading(true);
    setSshAllowlistError(null);
    try {
      const data = await fetchSshAllowlist();
      setSshAllowlist(data);
      setSshAllowlistDraft((data.ips ?? []).join("\n"));
      setSshAllowlistMessage("SSH-Whitelist aktualisiert.");
    } catch (err) {
      console.error(err);
      setSshAllowlistError("SSH-Whitelist konnte nicht geladen werden.");
    } finally {
      setSshAllowlistLoading(false);
    }
  };

  const handleSshAllowlistSave = async (event?: FormEvent) => {
    event?.preventDefault();
    setSshAllowlistSaving(true);
    setSshAllowlistError(null);
    setSshAllowlistMessage(null);
    try {
      const ips = sshAllowlistDraft
        .split("\n")
        .map((entry) => entry.trim())
        .filter((entry) => entry.length > 0);
      const data = await updateSshAllowlist(ips);
      setSshAllowlist(data);
      setSshAllowlistDraft((data.ips ?? []).join("\n"));
      setSshAllowlistMessage("SSH-Whitelist gespeichert.");
    } catch (err) {
      console.error(err);
      const detail = (err as any)?.response?.data?.detail ?? (err as any)?.message;
      setSshAllowlistError(
        detail ? `SSH-Whitelist konnte nicht gespeichert werden: ${detail}` : "SSH-Whitelist konnte nicht gespeichert werden."
      );
    } finally {
      setSshAllowlistSaving(false);
    }
  };

  const updatePathStateLocal = (path: string, state: ModePathState) => {
    const currentGlobal = modeState?.global_auto_enabled ?? true;
    setModeState((prev) => {
      const nextPaths = { ...(prev?.paths ?? {}) };
      nextPaths[path] = state;
      return {
        global_auto_enabled: prev?.global_auto_enabled ?? currentGlobal,
        paths: nextPaths
      };
    });
  };

  const handleGlobalAutoToggle = async (enabled: boolean) => {
    setGlobalAutoSaving(true);
    setModeMessage(null);
    try {
      const snapshot = await setGlobalModeAuto(enabled);
      // Lade auch Stats neu, um sicherzustellen, dass alles synchron ist
      const stats = await fetchCurrentStrategyStats();
      setModeState(snapshot);
      setModeStats(stats);
      setModeError(null);
      setModeMessage(`Globales Auto-Switching ${enabled ? "aktiviert" : "deaktiviert"}.`);
    } catch (err) {
      console.error(err);
      setModeError("Globales Auto-Switching konnte nicht aktualisiert werden.");
    } finally {
      setGlobalAutoSaving(false);
    }
  };

  const handleForceAllToTest = async () => {
    if (!window.confirm("Mode Controller temporär deaktivieren und alle bekannten Pfade auf TEST setzen?")) {
      return;
    }
    setForceTestSaving(true);
    setModeMessage(null);
    setModeError(null);
    try {
      const [snapshot, stats] = await Promise.all([
        forceAllTradeModesToTest(),
        fetchCurrentStrategyStats(),
      ]);
      setModeState(snapshot);
      setModeStats(stats);
      setModeMessage(
        `Temporärer TEST-Modus aktiv: ${snapshot.affected_paths} Pfade gesetzt (${snapshot.switched_to_test} umgeschaltet), Auto-Switching global deaktiviert.`
      );
    } catch (err) {
      console.error(err);
      setModeError("Temporärer TEST-Modus konnte nicht aktiviert werden.");
    } finally {
      setForceTestSaving(false);
    }
  };

  const handleManualEnabledChange = async (row: ModePathRow, enabled: boolean) => {
    const pathKey = row.key;
    setPathSaving((prev) => ({ ...prev, [pathKey]: true }));
    setModeMessage(null);
    try {
      const { state } = await setManualEnabled(row.strategy, row.symbol, row.direction, enabled);
      updatePathStateLocal(pathKey, state);
      // Lade den vollständigen Mode-State neu, um sicherzustellen, dass alles synchron ist
      const [snapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
      setModeState(snapshot);
      setModeStats(stats);
      setModeError(null);
      setModeMessage(`Pfad ${row.strategy}/${row.symbol}/${row.direction} aktualisiert.`);
    } catch (err) {
      console.error(err);
      setModeError("Pfad konnte nicht aktualisiert werden.");
    } finally {
      setPathSaving((prev) => {
        const next = { ...prev };
        delete next[pathKey];
        return next;
      });
    }
  };

  const handleManualModeChange = async (row: ModePathRow, mode: "live" | "test" | null) => {
    const pathKey = row.key;
    setPathSaving((prev) => ({ ...prev, [pathKey]: true }));
    setModeMessage(null);
    try {
      const { state } = await setManualMode(row.strategy, row.symbol, row.direction, mode);
      // Aktualisiere lokalen State sofort mit dem zurückgegebenen State (inkl. current_mode)
      updatePathStateLocal(pathKey, state);
      setModeError(null);
      setModeMessage(`Manueller Modus für ${row.strategy}/${row.symbol}/${row.direction} auf ${mode?.toUpperCase() ?? "AUTO"} gesetzt.`);
      
      // Lade den vollständigen Mode-State nach kurzer Verzögerung neu, um sicherzustellen, dass alles synchron ist
      // Dies gibt dem Backend Zeit, die Änderung zu persistieren
      setTimeout(async () => {
        try {
          const [snapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
          setModeState(snapshot);
          setModeStats(stats);
        } catch (err) {
          console.error("Fehler beim Neuladen des Mode-States:", err);
        }
      }, 500);
    } catch (err) {
      console.error(err);
      setModeError("Manueller Modus konnte nicht gesetzt werden.");
    } finally {
      setPathSaving((prev) => {
        const next = { ...prev };
        delete next[pathKey];
        return next;
      });
    }
  };

  const handleAutoEnabledChange = async (row: ModePathRow, enabled: boolean) => {
    const pathKey = row.key;
    setPathSaving((prev) => ({ ...prev, [pathKey]: true }));
    setModeMessage(null);
    try {
      const { state } = await setAutoEnabled(row.strategy, row.symbol, row.direction, enabled);
      updatePathStateLocal(pathKey, state);
      // Lade den vollständigen Mode-State neu, um sicherzustellen, dass alles synchron ist
      const [snapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
      setModeState(snapshot);
      setModeStats(stats);
      setModeError(null);
      setModeMessage(`Auto-Switching für ${row.strategy}/${row.symbol}/${row.direction} ${enabled ? "aktiviert" : "deaktiviert"}.`);
    } catch (err) {
      console.error(err);
      setModeError("Auto-Switching konnte nicht aktualisiert werden.");
    } finally {
      setPathSaving((prev) => {
        const next = { ...prev };
        delete next[pathKey];
        return next;
      });
    }
  };

  const updateAdaptiveOverridesLocal = (overrides: Record<string, boolean>) => {
    setCurrentSettings((prev) => {
      if (!prev) return prev;
      return setNestedValueImmutable(prev, "strategy.adaptive_entry_overrides", overrides);
    });
  };

  const handleAdaptiveEntryToggle = async (row: ModePathRow, enabled: boolean) => {
    const pathKey = adaptiveEntryKey(row.strategy, row.symbol, row.direction);
    setAdaptiveSaving((prev) => ({ ...prev, [pathKey]: true }));
    setAdaptiveMessage(null);
    setAdaptiveError(null);
    try {
      const response = await updateAdaptiveEntryOverride(
        row.strategy,
        row.symbol,
        row.direction,
        enabled
      );
      updateAdaptiveOverridesLocal(response.overrides);
      setAdaptiveMessage(
        `Adaptive Entry für ${row.strategy}/${row.symbol}/${row.direction} ${enabled ? "aktiviert" : "deaktiviert"}. ` +
          "Wirkt beim nächsten Deploy."
      );
    } catch (err) {
      console.error(err);
      setAdaptiveError("Adaptive Entry konnte nicht aktualisiert werden.");
    } finally {
      setAdaptiveSaving((prev) => {
        const next = { ...prev };
        delete next[pathKey];
        return next;
      });
    }
  };

  const handleDisabledToggle = async (row: ModePathRow, enabled: boolean) => {
    if (!currentSettings) return;
    setDisabledSaving((prev) => ({ ...prev, [row.key]: true }));
    setDisabledMessage(null);
    setDisabledError(null);
    try {
      const response = await updateDisabledBranch(row.strategy, row.symbol, row.direction, enabled);
      setCurrentSettings((prev) => {
        if (!prev) return prev;
        return setNestedValueImmutable(prev, "strategy.disabled_branches", response.disabled_branches);
      });
      setDisabledMessage(enabled ? "Pfad wieder aktiviert." : "Pfad deaktiviert.");
    } catch (err) {
      console.error(err);
      setDisabledError("Pfad-Status konnte nicht gespeichert werden.");
    } finally {
      setDisabledSaving((prev) => {
        const next = { ...prev };
        delete next[row.key];
        return next;
      });
    }
  };

  const handleRefreshModeState = async () => {
    setModeLoading(true);
    setModeMessage(null);
    try {
      const [snapshot, stats] = await Promise.all([fetchModeState(), fetchCurrentStrategyStats()]);
      setModeState(snapshot);
      setModeStats(stats);
      setModeError(null);
      setModeMessage("Mode-Controller aktualisiert.");
    } catch (err) {
      console.error(err);
      setModeError("Mode-Controller konnte nicht aktualisiert werden.");
    } finally {
      setModeLoading(false);
    }
  };

  const handleConfigCheckboxChange = (field: "enabled" | "auto_enabled_by_default", checked: boolean) => {
    if (!modeConfigDraft) return;
    setModeConfigDraft({ ...modeConfigDraft, [field]: checked });
    setModeMessage(null);
    setConfigDirty(true);
  };

  const handleDefaultModeChange = (value: "live" | "test") => {
    if (!modeConfigDraft) return;
    setModeConfigDraft({ ...modeConfigDraft, default_mode: value });
    setModeMessage(null);
    setConfigDirty(true);
  };

  const handleThresholdChange = (field: keyof ModeThresholdsConfig, raw: string) => {
    if (!modeConfigDraft) return;
    const parsed = Number(raw);
    if (Number.isNaN(parsed)) {
      return;
    }
    let value = parsed;
    const integerFields: Array<keyof ModeThresholdsConfig> = ["cooldown_minutes", "max_auto_switches_per_day", "ema_period", "lookback_trades"];
    if (integerFields.includes(field)) {
      value = Math.round(value);
    }
    value = Math.max(0, value);
    const nextThresholds = { ...modeConfigDraft.thresholds, [field]: value };
    setModeConfigDraft({ ...modeConfigDraft, thresholds: nextThresholds });
    setModeMessage(null);
    setConfigDirty(true);
  };

  const handleModeConfigSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!modeConfigDraft) return;
    setModeSaving(true);
    setModeMessage(null);
    try {
      const updated = await updateModeControlConfig(modeConfigDraft);
      setModeConfig(updated);
      setModeConfigDraft(updated);
      setConfigDirty(false);
      setModeError(null);
      setModeMessage("Mode-Control-Einstellungen gespeichert.");
      setCurrentSettings((prev) => {
        if (!prev) return prev;
        const next = { ...prev } as Record<string, unknown>;
        const currentStrategy = ((next["strategy"] ?? {}) as Record<string, unknown>) || {};
        next["strategy"] = { ...currentStrategy, mode_control: updated };
        return next;
      });
    } catch (err) {
      console.error(err);
      setModeError("Mode-Control-Einstellungen konnten nicht gespeichert werden.");
    } finally {
      setModeSaving(false);
    }
  };

  const toggleField = (path: string, checked: boolean) => {
    if (hiddenFields === null) return;
    const previous = Array.from(hiddenFields);
    const nextSet = new Set(hiddenFields);
    if (checked) {
      nextSet.delete(path);
    } else {
      nextSet.add(path);
    }
    applyHiddenFields(Array.from(nextSet), previous);
  };

  const handleSelectAll = () => {
    if (hiddenFields === null) return;
    applyHiddenFields([], Array.from(hiddenFields));
  };

  const handleSelectNone = () => {
    if (hiddenFields === null) return;
    const allPaths = settingsFields.map((field) => field.path);
    applyHiddenFields(allPaths, Array.from(hiddenFields));
  };

  const extractBinanceFeeConfig = (settings: Record<string, unknown> | null) => {
    const binance = (settings?.binance ?? {}) as Record<string, unknown>;
    return {
      fee_model: (binance.fee_model as string) ?? "spot",
      fee_entry_rate: numberOrFallback(binance.fee_entry_rate, 0.001),
      fee_exit_rate: numberOrFallback(binance.fee_exit_rate, 0.001),
      fee_maker_rate: numberOrFallback(binance.fee_maker_rate, 0.0002),
      fee_taker_rate: numberOrFallback(binance.fee_taker_rate, 0.0004)
    };
  };

  const handleBinanceFeeModelChange = (model: "spot" | "futures") => {
    if (!currentSettings) return;
    const updated = setNestedValueImmutable(currentSettings, "binance.fee_model", model);
    setCurrentSettings(updated);
    setBinanceFeeMessage(null);
    setBinanceFeeError(null);
  };

  const handleBinanceFeeRateChange = (field: string, value: string) => {
    if (!currentSettings) return;
    const parsed = Number(value);
    if (Number.isNaN(parsed) || parsed < 0) {
      setBinanceFeeError(`Ungültiger Wert für ${field}`);
      return;
    }
    const updated = setNestedValueImmutable(currentSettings, `binance.${field}`, parsed);
    setCurrentSettings(updated);
    setBinanceFeeMessage(null);
    setBinanceFeeError(null);
  };

  const handleBinanceFeeSave = async () => {
    if (!currentSettings) return;
    setBinanceFeeSaving(true);
    setBinanceFeeMessage(null);
    setBinanceFeeError(null);
    try {
      await deploySettings({
        settings: currentSettings,
        started_at: new Date().toISOString()
      });
      setBinanceFeeMessage("Binance Fee-Einstellungen gespeichert. Neuer Run gestartet.");
    } catch (err) {
      console.error(err);
      setBinanceFeeError("Fee-Einstellungen konnten nicht gespeichert werden.");
    } finally {
      setBinanceFeeSaving(false);
    }
  };

  const binanceFeeConfig = extractBinanceFeeConfig(currentSettings);

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Settings &amp; Templates</h2>
          <p>Tooltips, Filter und gespeicherte Templates.</p>
        </div>
      </header>

      {error && <div className="card card--error">{error}</div>}

      <div className="card" style={{ marginBottom: "2rem" }}>
        <h3>Data-Health (Live Cache)</h3>
        {dataHealthError && <p className="text-negative">{dataHealthError}</p>}
        {!dataHealth || dataHealth.items.length === 0 ? (
          <p>Keine Data-Health-Daten verfügbar.</p>
        ) : (
          <>
            <p style={{ marginBottom: "1rem", opacity: 0.8 }}>
              OK: {dataHealth.summary.ok} / WARN: {dataHealth.summary.warn}
            </p>
            <div className="table-wrapper">
              <table className="table">
                <thead>
                  <tr>
                    <th>Exchange</th>
                    <th>Symbol</th>
                    <th>Intervall</th>
                    <th>Status</th>
                    <th>Coverage</th>
                    <th>Max Gap</th>
                    <th>Last Age</th>
                    <th>Timestamp</th>
                  </tr>
                </thead>
                <tbody>
                  {dataHealth.items.map((row, idx) => (
                    <tr key={`${row.exchange}-${row.symbol}-${row.interval}-${idx}`}>
                      <td>{row.exchange}</td>
                      <td><strong>{row.symbol}</strong></td>
                      <td>{row.interval}</td>
                      <td>
                        <span className={`badge badge--${row.status === "ok" ? "success" : "warning"}`}>
                          {row.status.toUpperCase()}
                        </span>
                      </td>
                      <td>{row.coverage_pct.toFixed(2)}%</td>
                      <td>{Math.round(row.max_gap_seconds)}s</td>
                      <td>{Math.round(row.last_bar_age_seconds)}s</td>
                      <td>{new Date(row.timestamp).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <div className="card mode-control-card">
        <div className="mode-control-card__header">
          <div>
            <h3>Mode Controller</h3>
            <p>Live/Test Steuerung & automatische Umschaltung.</p>
          </div>
          <div className="mode-control-card__actions">
            <button
              type="button"
              className="mode-control-card__button"
              onClick={handleForceAllToTest}
              disabled={forceTestSaving || modeLoading}
              title="Temporär: Mode Controller aus + alle bekannten Pfade auf TEST"
            >
              {forceTestSaving ? "Schalte um..." : "Temporär alles auf TEST"}
            </button>
            <button
              type="button"
              className="mode-control-card__button"
              onClick={handleRefreshModeState}
              disabled={modeLoading || forceTestSaving}
            >
              {modeLoading ? "Aktualisiere..." : "Aktualisieren"}
            </button>
          </div>
        </div>
        {modeMessage && (
          <p className="mode-control-card__status mode-control-card__status--ok">{modeMessage}</p>
        )}
        {modeError && (
          <p className="mode-control-card__status mode-control-card__status--error">{modeError}</p>
        )}
        {modeLoading && !modeConfigDraft ? (
          <p>Lade Mode-Controller...</p>
        ) : (
          <>
            <div className="mode-control-global">
              <label className="mode-control-toggle">
                <input
                  type="checkbox"
                  checked={modeState?.global_auto_enabled ?? true}
                  onChange={(event) => handleGlobalAutoToggle(event.target.checked)}
                  disabled={globalAutoSaving || modeLoading}
                />
                <span>Globales Auto-Switching</span>
              </label>
              <span className="mode-control-global__status">
                {globalAutoSaving
                  ? "Aktualisiere..."
                  : modeState?.global_auto_enabled
                    ? "aktiv"
                    : "deaktiviert"}
              </span>
            </div>
            {modeConfigDraft ? (
              <form className="mode-control-form" onSubmit={handleModeConfigSubmit}>
                <div className="mode-control-form__row">
                  <label className="mode-control-toggle">
                    <input
                      type="checkbox"
                      checked={modeConfigDraft.enabled}
                      onChange={(event) => handleConfigCheckboxChange("enabled", event.target.checked)}
                    />
                    <span>Mode Controller aktiv</span>
                  </label>
                  <label className="mode-control-toggle">
                    <input
                      type="checkbox"
                      checked={modeConfigDraft.auto_enabled_by_default}
                      onChange={(event) =>
                        handleConfigCheckboxChange("auto_enabled_by_default", event.target.checked)
                      }
                    />
                    <span>Auto-Switch standardmäßig an</span>
                  </label>
                  <label className="mode-control-select">
                    <span>Default Modus</span>
                    <select
                      value={modeConfigDraft.default_mode}
                      onChange={(event) => handleDefaultModeChange(event.target.value as "live" | "test")}
                    >
                      <option value="live">Live</option>
                      <option value="test">Test</option>
                    </select>
                  </label>
                </div>
                <fieldset className="mode-control-form__thresholds">
                  <legend>Auto-Switch Parameter</legend>
                  <div className="mode-control-form__grid">
                    {THRESHOLD_FIELD_META.map(({ key, label, step, hint }) => (
                      <label key={key} className="mode-control-field">
                        <span>{label}</span>
                        <input
                          type="number"
                          step={step}
                          value={modeConfigDraft.thresholds[key]}
                          onChange={(event) => handleThresholdChange(key, event.target.value)}
                        />
                        {hint && <small>{hint}</small>}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <div className="mode-control-form__actions">
                  <button
                    type="submit"
                    className="mode-control-card__button"
                    disabled={modeSaving || !configDirty}
                  >
                    {modeSaving ? "Speichere..." : configDirty ? "Änderungen speichern" : "Gespeichert"}
                  </button>
                </div>
              </form>
            ) : (
              <p>Mode-Control Settings konnten nicht geladen werden.</p>
            )}
            {statsSummary ? (
              <div className="mode-control-summary">
                <h4>Laufende Strategien</h4>
                <ul className="kv-list">
                  <li>
                    <span>Trades gesamt</span>
                    <strong>{statsSummary.totalTrades}</strong>
                  </li>
                  <li>
                    <span>Win-Rate</span>
                    <strong>{formatPercent(statsSummary.winRate)}</strong>
                  </li>
                  <li>
                    <span>Ø PnL pro Trade</span>
                    <strong>{formatNumber(statsSummary.pnlPerTrade)}</strong>
                  </li>
                  <li>
                    <span>PnL gesamt</span>
                    <strong>{formatNumber(statsSummary.totalPnl)}</strong>
                  </li>
                </ul>
              </div>
            ) : (
              <p className="mode-control-summary__empty">Keine laufenden Strategiestats verfügbar.</p>
            )}
          </>
        )}
      </div>

      <div className="card mode-control-paths">
        <h3>Strategiepfade</h3>
        {adaptiveMessage && (
          <p className="mode-control-card__status mode-control-card__status--ok">{adaptiveMessage}</p>
        )}
        {adaptiveError && (
          <p className="mode-control-card__status mode-control-card__status--error">{adaptiveError}</p>
        )}
        {disabledMessage && (
          <p className="mode-control-card__status mode-control-card__status--ok">{disabledMessage}</p>
        )}
        {disabledError && (
          <p className="mode-control-card__status mode-control-card__status--error">{disabledError}</p>
        )}
        {modeLoading && !modeState ? (
          <p>Lade Pfad-Daten...</p>
        ) : pathRows.length === 0 ? (
          <p>
            Keine Pfade bekannt. Es werden Einträge erstellt, sobald Strategien handeln oder du Einstellungen anpasst.
          </p>
        ) : (
          <div className="mode-control-table-wrapper">
            <table className="mode-control-table">
              <thead>
                <tr>
                  <th>Strategie</th>
                  <th>Symbol</th>
                  <th>Richtung</th>
                  <th>Aktiv</th>
                  <th>Modus</th>
                  <th>Manuell</th>
                  <th>Modus Override</th>
                  <th>Auto</th>
                  <th>Adaptive Entry</th>
                  <th>Stats</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {pathRows.map((row) => {
                  const saving = pathSaving[row.key] ?? false;
                  const adaptiveKey = adaptiveEntryKey(row.strategy, row.symbol, row.direction);
                  const adaptiveOverride = adaptiveEntryConfig.overrides[adaptiveKey];
                  const adaptiveEnabled =
                    adaptiveOverride !== undefined ? adaptiveOverride : adaptiveEntryConfig.globalEnabled;
                  const adaptiveSavingRow = adaptiveSaving[adaptiveKey] ?? false;
                  const disabledKey = `${row.strategy.toLowerCase()}|${row.symbol.toUpperCase()}|${row.direction}`;
                  const isDisabled = disabledBranches.has(disabledKey);
                  const isNoMc = noMcBranches.has(disabledKey);
                  const disabledSavingRow = disabledSaving[row.key] ?? false;
                  return (
                    <tr key={row.key} className={isDisabled ? "mode-control-row--disabled" : undefined}>
                      <td>
                        <strong>
                          {row.strategy}
                          {isNoMc ? " NOMC" : ""}
                        </strong>
                        {row.state.last_switch_reason && (
                          <div className="mode-control-table__sub">{row.state.last_switch_reason}</div>
                        )}
                      </td>
                      <td>{row.symbol}</td>
                      <td>
                        <span className={`mode-control-direction mode-control-direction--${row.direction}`}>
                          {row.direction === "long" ? "Long" : "Short"}
                        </span>
                      </td>
                      <td>
                        <label className="mode-control-toggle">
                          <input
                            type="checkbox"
                            checked={!isDisabled}
                            onChange={(event) => handleDisabledToggle(row, event.target.checked)}
                            disabled={disabledSavingRow || modeLoading}
                          />
                          <span>{isDisabled ? "Aus" : "An"}</span>
                        </label>
                      </td>
                      <td>
                        {/* Zeige current_mode mit Badge-Styling, aber wenn manual_mode gesetzt ist und current_mode nicht übereinstimmt, 
                            zeige manual_mode (weil das der tatsächliche aktive Mode ist) */}
                        {(() => {
                          const displayMode = (row.state.manual_mode && row.state.manual_mode !== row.state.current_mode)
                            ? row.state.manual_mode
                            : (row.state.current_mode ?? "test");
                          const modeBadgeClass = displayMode === "live" ? "badge badge--live" : "badge badge--test";
                          return (
                            <span className={modeBadgeClass}>
                              {displayMode.toUpperCase()}
                            </span>
                          );
                        })()}
                      </td>
                      <td>
                        <label className="mode-control-toggle">
                          <input
                            type="checkbox"
                            checked={row.state.manual_enabled}
                            onChange={(event) => handleManualEnabledChange(row, event.target.checked)}
                            disabled={saving || modeLoading}
                          />
                          <span>An</span>
                        </label>
                      </td>
                      <td>
                        <select
                          className="mode-control-select__input"
                          value={row.state.manual_mode ?? ""}
                          onChange={(event) =>
                            handleManualModeChange(
                              row,
                              event.target.value ? (event.target.value as "live" | "test") : null
                            )
                          }
                          disabled={saving || modeLoading}
                        >
                          <option value="">Auto</option>
                          <option value="live">Live</option>
                          <option value="test">Test</option>
                        </select>
                      </td>
                      <td>
                        <label className="mode-control-toggle">
                          <input
                            type="checkbox"
                            checked={row.state.auto_enabled}
                            onChange={(event) => handleAutoEnabledChange(row, event.target.checked)}
                            disabled={saving || modeLoading}
                          />
                          <span>An</span>
                        </label>
                      </td>
                      <td>
                        <label
                          className="mode-control-toggle"
                          title={adaptiveOverride === undefined ? "Globaler Standard" : "Pfad-Override"}
                        >
                          <input
                            type="checkbox"
                            checked={adaptiveEnabled}
                            onChange={(event) => handleAdaptiveEntryToggle(row, event.target.checked)}
                            disabled={adaptiveSavingRow || modeLoading || !currentSettings}
                          />
                          <span>{adaptiveEnabled ? "An" : "Aus"}</span>
                        </label>
                      </td>
                      <td>
                        {row.stats ? (
                          <div className="mode-control-stats">
                            <span>{row.stats.trades} Trades</span>
                            <span>{formatPercent(row.stats.win_rate)}</span>
                            <span>{formatNumber(row.stats.pnl)}</span>
                          </div>
                        ) : (
                          <span>—</span>
                        )}
                      </td>
                      <td>
                        <div className="mode-control-status-cell">
                          <span>{formatTimestamp(row.state.last_switch_at)}</span>
                          {row.state.degraded_reason && (
                            <small>{row.state.degraded_reason.replace(/_/g, " ")}</small>
                          )}
                          {row.state.cooldown_until && (
                            <small>Cooldown bis {formatTimestamp(row.state.cooldown_until)}</small>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="mode-control-card__header">
          <div>
            <h3>SSH-Whitelist</h3>
            <p>
              Erlaubte IPs für SSH-Zugriff (eine IP pro Zeile). Änderungen greifen sofort auf dem Server.
            </p>
            <p>
              Hinweis: Zugriff gilt nur für `root@IP` und nur via SSH-Key (kein Root-Passwort-Login).
            </p>
          </div>
          <div className="mode-control-card__actions">
            <button
              type="button"
              className="mode-control-card__button"
              onClick={handleSshAllowlistRefresh}
              disabled={sshAllowlistLoading}
            >
              {sshAllowlistLoading ? "Lade..." : "Aktualisieren"}
            </button>
          </div>
        </div>
        {sshAllowlistMessage && (
          <p className="mode-control-card__status mode-control-card__status--ok">{sshAllowlistMessage}</p>
        )}
        {sshAllowlistError && (
          <p className="mode-control-card__status mode-control-card__status--error">{sshAllowlistError}</p>
        )}
        <form className="mode-control-form" onSubmit={handleSshAllowlistSave}>
          <div className="mode-control-form__row">
            <label className="mode-control-field" style={{ flex: 1 }}>
              <span>Erlaubte IPs</span>
              <textarea
                rows={4}
                value={sshAllowlistDraft}
                onChange={(event) => setSshAllowlistDraft(event.target.value)}
                placeholder="z.B. 87.166.162.74&#10;100.90.220.22"
              />
              <small>
                Quelle: {sshAllowlist?.source ?? "—"}{" "}
                {sshAllowlist?.path ? `(${sshAllowlist.path})` : ""}
              </small>
            </label>
          </div>
          <div className="mode-control-form__actions">
            <button
              type="submit"
              className="mode-control-card__button"
              disabled={sshAllowlistSaving || sshAllowlistLoading}
            >
              {sshAllowlistSaving ? "Speichere..." : "Whitelist speichern"}
            </button>
          </div>
        </form>
      </div>

      <div className="card">
        <div className="mode-control-card__header">
          <div>
            <h3>Binance Fee-Modell</h3>
            <p>Konfiguriere das Fee-Modell für Binance Trading (Spot oder Futures).</p>
          </div>
        </div>
        {binanceFeeMessage && (
          <p className="mode-control-card__status mode-control-card__status--ok">{binanceFeeMessage}</p>
        )}
        {binanceFeeError && (
          <p className="mode-control-card__status mode-control-card__status--error">{binanceFeeError}</p>
        )}
        {!currentSettings ? (
          <p>Lade Settings...</p>
        ) : (
          <form
            className="mode-control-form"
            onSubmit={(e) => {
              e.preventDefault();
              handleBinanceFeeSave();
            }}
          >
            <div className="mode-control-form__row">
              <label className="mode-control-select">
                <span>Fee-Modell</span>
                <select
                  value={binanceFeeConfig.fee_model}
                  onChange={(e) => handleBinanceFeeModelChange(e.target.value as "spot" | "futures")}
                >
                  <option value="spot">Spot (0.1% pro Trade)</option>
                  <option value="futures">Futures (0.02% Maker / 0.04% Taker)</option>
                </select>
              </label>
            </div>
            {binanceFeeConfig.fee_model === "spot" ? (
              <fieldset className="mode-control-form__thresholds">
                <legend>Spot Fees</legend>
                <div className="mode-control-form__grid">
                  <label className="mode-control-field">
                    <span>Entry Fee Rate</span>
                    <input
                      type="number"
                      step="0.0001"
                      min="0"
                      max="0.01"
                      value={binanceFeeConfig.fee_entry_rate}
                      onChange={(e) => handleBinanceFeeRateChange("fee_entry_rate", e.target.value)}
                    />
                    <small>{(binanceFeeConfig.fee_entry_rate * 100).toFixed(2)}%</small>
                  </label>
                  <label className="mode-control-field">
                    <span>Exit Fee Rate</span>
                    <input
                      type="number"
                      step="0.0001"
                      min="0"
                      max="0.01"
                      value={binanceFeeConfig.fee_exit_rate}
                      onChange={(e) => handleBinanceFeeRateChange("fee_exit_rate", e.target.value)}
                    />
                    <small>{(binanceFeeConfig.fee_exit_rate * 100).toFixed(2)}%</small>
                  </label>
                </div>
                <p className="mode-control-form__hint">
                  Standard Binance Spot: 0.1% (0.001) pro Trade. Roundtrip:{" "}
                  {((binanceFeeConfig.fee_entry_rate + binanceFeeConfig.fee_exit_rate) * 100).toFixed(2)}%
                </p>
              </fieldset>
            ) : (
              <fieldset className="mode-control-form__thresholds">
                <legend>Futures Fees</legend>
                <div className="mode-control-form__grid">
                  <label className="mode-control-field">
                    <span>Maker Fee Rate</span>
                    <input
                      type="number"
                      step="0.0001"
                      min="0"
                      max="0.01"
                      value={binanceFeeConfig.fee_maker_rate}
                      onChange={(e) => handleBinanceFeeRateChange("fee_maker_rate", e.target.value)}
                    />
                    <small>{(binanceFeeConfig.fee_maker_rate * 100).toFixed(2)}%</small>
                  </label>
                  <label className="mode-control-field">
                    <span>Taker Fee Rate</span>
                    <input
                      type="number"
                      step="0.0001"
                      min="0"
                      max="0.01"
                      value={binanceFeeConfig.fee_taker_rate}
                      onChange={(e) => handleBinanceFeeRateChange("fee_taker_rate", e.target.value)}
                    />
                    <small>{(binanceFeeConfig.fee_taker_rate * 100).toFixed(2)}%</small>
                  </label>
                </div>
                <p className="mode-control-form__hint">
                  Standard Binance Futures: 0.02% Maker / 0.04% Taker. Paper Trading verwendet Taker-Rate (Market Orders).
                  Roundtrip: {(binanceFeeConfig.fee_taker_rate * 2 * 100).toFixed(2)}%
                </p>
              </fieldset>
            )}
            <div className="mode-control-form__actions">
              <button
                type="submit"
                className="mode-control-card__button"
                disabled={binanceFeeSaving}
              >
                {binanceFeeSaving ? "Speichere..." : "Fee-Einstellungen speichern"}
              </button>
            </div>
          </form>
        )}
      </div>

      <div className="grid grid--cols-2 grid--gap-lg">
        <div className="card">
          <h3>Bot-Control Filter</h3>
          {!currentSettings || hiddenFields === null ? (
            <p>Lade Settings...</p>
          ) : (
            <div className="settings-visibility">
              <div className="settings-visibility__controls">
                <input
                  type="text"
                  className="settings-visibility__search"
                  placeholder="Parameter suchen..."
                  value={visibilitySearch}
                  onChange={(event) => setVisibilitySearch(event.target.value)}
                />
                <div className="settings-visibility__buttons">
                  <button
                    type="button"
                    className="settings-visibility__button"
                    onClick={handleSelectAll}
                    disabled={visibilitySaving}
                  >
                    Alle anzeigen
                  </button>
                  <button
                    type="button"
                    className="settings-visibility__button"
                    onClick={handleSelectNone}
                    disabled={visibilitySaving}
                  >
                    Alle ausblenden
                  </button>
                </div>
              </div>
              {visibilitySaving && <p className="settings-visibility__status">Speichere Filter…</p>}
              {visibilityError && <p className="settings-field__error">{visibilityError}</p>}
              <div className="settings-visibility__list">
                {filteredVisibilityFields.map((field) => {
                  const checked = !hiddenFields.has(field.path);
                  return (
                    <label key={field.path} className="settings-visibility__item">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => toggleField(field.path, event.target.checked)}
                        disabled={visibilitySaving}
                      />
                      <div>
                        <span className="settings-visibility__item-name">{field.displayName}</span>
                        <code className="settings-visibility__item-path">{field.path}</code>
                        {field.tooltip && <p className="settings-visibility__item-hint">{field.tooltip}</p>}
                      </div>
                    </label>
                  );
                })}
                {filteredVisibilityFields.length === 0 && (
                  <p className="settings-visibility__empty">Keine Felder passend zum Filter.</p>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <h3>Settings Schema</h3>
          {loading ? (
            <p>Lade Schema...</p>
          ) : (
            <div className="settings-schema">
              {Object.entries(groupedSchema).map(([group, nodes]) => (
                <details key={group} open>
                  <summary>{group}</summary>
                  <ul>
                    {nodes.map((node) => {
                      const tooltip = tooltips[node.path] ?? tooltips[`${node.path}.value`] ?? "Keine Beschreibung vorhanden.";
                      return (
                        <li key={node.path}>
                          <strong>{node.name}</strong>
                          <p>{tooltip}</p>
                          <code>{node.path}</code>
                        </li>
                      );
                    })}
                  </ul>
                </details>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <h3>CPU Monitoring</h3>
        {cpuStats ? (
          <div className="cpu-monitoring">
            {cpuStats.error && (
              <p className="cpu-monitoring__error">
                {cpuStats.error}
                {cpuStatsError && ` (${cpuStatsError})`}
              </p>
            )}
            <div className="cpu-monitoring__system">
              <div className="cpu-monitoring__system-item">
                <strong>System CPU:</strong>{" "}
                <span
                  className={
                    cpuStats.system.cpu_percent > 80
                      ? "cpu-high"
                      : cpuStats.system.cpu_percent > 50
                        ? "cpu-medium"
                        : "cpu-low"
                  }
                >
                  {cpuStats.system.cpu_percent.toFixed(1)}%
                </span>
                {" "}({cpuStats.system.cpu_count} cores)
              </div>
              <div className="cpu-monitoring__system-item">
                <strong>Load Average:</strong> {cpuStats.system.load_avg[0].toFixed(2)},{" "}
                {cpuStats.system.load_avg[1].toFixed(2)}, {cpuStats.system.load_avg[2].toFixed(2)}
              </div>
              <div className="cpu-monitoring__system-item" style={{ fontSize: "0.85em", color: "#666", fontStyle: "italic" }}>
                Note: Process CPU % is normalized to system-wide (100% of 1 core = {cpuStats.system.cpu_count > 0 ? (100 / cpuStats.system.cpu_count).toFixed(1) : "100"}% system-wide)
              </div>
            </div>
            {cpuStats.processes.length > 0 ? (
              <div className="cpu-monitoring__processes">
                <table className="cpu-monitoring__table">
                  <thead>
                    <tr>
                      <th>PID/Thread</th>
                      <th>Name</th>
                      <th>Type</th>
                      <th>CPU %</th>
                      <th>Memory %</th>
                      <th>Command</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cpuStats.processes
                      .sort((a, b) => b.cpu_percent - a.cpu_percent)
                      .map((proc) => {
                        // Use worker_count from API if available, otherwise try to extract from cmdline
                        const workerCount = proc.worker_count !== null && proc.worker_count !== undefined
                          ? proc.worker_count
                          : null;
                        
                        // Calculate CPU display: show raw CPU if available, otherwise normalized
                        const cpuDisplay = proc.cpu_percent_raw !== null && proc.cpu_percent_raw !== undefined
                          ? proc.cpu_percent_raw
                          : proc.cpu_percent * (cpuStats.system.cpu_count || 1);
                        const coresUsed = proc.cores_used !== null && proc.cores_used !== undefined
                          ? proc.cores_used
                          : (cpuDisplay > 0 ? cpuDisplay / 100 : null);
                        
                        return (
                          <tr key={proc.thread_id ? `${proc.pid}-${proc.thread_id}` : proc.pid}>
                            <td>
                              {proc.pid}
                              {proc.thread_id && (
                                <span className="cpu-monitoring__thread-id"> (T:{proc.thread_id})</span>
                              )}
                            </td>
                            <td>
                              {proc.thread_name ? (
                                <span title={proc.thread_name}>{proc.thread_name}</span>
                              ) : (
                                proc.name
                              )}
                            </td>
                            <td>
                              <span
                                className={
                                  proc.type === "backtest"
                                    ? "cpu-type-backtest"
                                    : proc.type === "multitest"
                                      ? "cpu-type-multitest"
                                      : "cpu-type-other"
                                }
                              >
                                {proc.type}
                              </span>
                            </td>
                            <td>
                              <div>
                                <span
                                  className={
                                    proc.cpu_percent > 80
                                      ? "cpu-high"
                                      : proc.cpu_percent > 50
                                        ? "cpu-medium"
                                        : "cpu-low"
                                  }
                                >
                                  {proc.cpu_percent.toFixed(1)}%
                                </span>
                                {workerCount !== null && (
                                  <div style={{ fontSize: "0.85em", color: "#666", marginTop: "2px" }}>
                                    ({workerCount} Workers konfiguriert
                                    {coresUsed !== null && coresUsed > 0.1 && (
                                      <>, ~{coresUsed.toFixed(1)} Cores genutzt</>
                                    )}
                                    {coresUsed !== null && coresUsed <= 0.1 && workerCount > 1 && (
                                      <> - I/O-bound (Python GIL limitiert CPU-Parallelität)</>
                                    )}
                                    )
                                  </div>
                                )}
                                {workerCount === null && coresUsed !== null && coresUsed > 0.1 && coresUsed < 0.9 && (
                                  <div style={{ fontSize: "0.85em", color: "#666", marginTop: "2px" }}>
                                    (~{coresUsed.toFixed(1)} Core genutzt)
                                  </div>
                                )}
                              </div>
                            </td>
                            <td>{proc.memory_percent.toFixed(1)}%</td>
                            <td className="cpu-monitoring__cmdline" title={proc.cmdline}>
                              {proc.cmdline.length > 60 ? `${proc.cmdline.substring(0, 60)}...` : proc.cmdline}
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p>Keine CPU-intensiven Prozesse gefunden.</p>
            )}
          </div>
        ) : (
          <p>Lade CPU-Statistiken...</p>
        )}
      </div>

      <div className="card">
        <h3>Templates Übersicht</h3>
        {templates.length === 0 ? (
          <p>Noch keine Templates gespeichert.</p>
        ) : (
          <ul className="template-list">
            {templates.map((tpl) => (
              <li key={tpl.id}>
                <div className="template-list__info">
                  <strong>{tpl.name}</strong>
                  {tpl.description && <span>{tpl.description}</span>}
                  {(tpl.tags ?? []).length > 0 && <span className="template-list__tags">{tpl.tags.join(", ")}</span>}
                  {tpl.updated_at && <span>Aktualisiert: {tpl.updated_at}</span>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

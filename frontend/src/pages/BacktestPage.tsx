import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Area, Bar, ComposedChart, Line, LineChart, Tooltip, XAxis, YAxis, ResponsiveContainer, CartesianGrid } from "recharts";
import {
  fetchBacktest,
  fetchBacktestLog,
  fetchCurrentSettings,
  fetchSettingsSchema,
  fetchTemplate,
  fetchTemplates,
  createTemplate,
  deleteTemplate,
  fetchBacktestCompare,
  fetchTradeReplays,
  fetchShadowMirrors,
  fetchShadowMirrorConfig,
  updateShadowMirrorConfig,
  listBacktests,
  startBacktest,
  cancelBacktest,
  type BacktestCompareBranch,
  type BacktestCompareMeta,
  type BacktestCompareResponse,
  type BacktestDetailResponse,
  type BacktestLogResponse,
  type BacktestSummaryPayload,
  type ManualBacktestMeta,
  type TradeReplayItem,
  type TradeReplayResponse,
  type ShadowMirrorItem,
  type ShadowMirrorResponse,
  type ShadowMirrorConfig,
  type TemplateMeta,
  type SettingsResponse
} from "../api/client";
import { extractEditableFields, getNestedValue, setNestedValueImmutable } from "../utils/settings";
import { deepMerge } from "../utils/deepMerge";

const CURRENT_TEMPLATE_SENTINEL = "__current__";
type Primitive = string | number | boolean | null;

function formatDateInput(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function formatTimestamp(value?: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) {
    return "—";
  }
  return value.toFixed(digits);
}

function formatDeltaSeconds(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "—";
  const sign = value >= 0 ? "+" : "−";
  const abs = Math.abs(value);
  if (abs >= 3600) {
    return `${sign}${(abs / 3600).toFixed(2)}h`;
  }
  if (abs >= 60) {
    return `${sign}${(abs / 60).toFixed(2)}m`;
  }
  return `${sign}${abs.toFixed(0)}s`;
}

function formatHour(hour: number): string {
  if (!Number.isFinite(hour)) return "—";
  const h = Math.max(0, Math.min(23, Math.floor(hour)));
  return `${h.toString().padStart(2, "0")}:00`;
}

function parseReplayResult(item: TradeReplayItem): Record<string, any> | null {
  if (item.result && typeof item.result === "object") {
    return item.result;
  }
  if (item.result_json) {
    try {
      return JSON.parse(item.result_json);
    } catch {
      return null;
    }
  }
  return null;
}

function formatHourRange(
  startHour: number | null | undefined,
  endHour: number | null | undefined,
  timezone: string | null | undefined
): string | null {
  if (startHour == null && endHour == null) {
    return null;
  }
  const startLabel = startHour == null ? "—" : formatHour(startHour);
  const endLabel = endHour == null ? "—" : formatHour(endHour);
  const tz = timezone ? ` ${timezone}` : "";
  return `${startLabel}-${endLabel}${tz}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return "—";
  }
  return `${(value * 100).toFixed(2)}%`;
}

const PNL_PER_TRADE_THRESHOLD = 0.001; // 0.1%
const BACKTEST_PAGE_SIZE = 10;
const MODE_CONTROL_OVERRIDE_FIELDS: Array<{ key: string; label: string; step?: string; min?: string }> = [
  { key: "lookback_trades", label: "Lookback Trades", step: "1", min: "1" },
  { key: "min_significant_trades", label: "Min. Signifikante Trades", step: "1", min: "1" },
  { key: "noise_threshold_pct", label: "Noise Threshold (%)", step: "0.01", min: "0" },
  { key: "strong_impulse_pct", label: "Strong Impulse (%)", step: "0.1", min: "0" },
  { key: "min_test_avg_pct", label: "Min. Test Avg (%)", step: "0.1", min: "0" },
  { key: "min_test_ema_pct", label: "Min. Test EMA (%)", step: "0.1", min: "0" },
  { key: "min_live_trades", label: "Min. Live Trades", step: "1", min: "1" },
  { key: "cooldown_minutes", label: "Cooldown (Min)", step: "1", min: "0" },
  { key: "ema_period", label: "EMA Period", step: "1", min: "1" },
  { key: "ema_threshold_pct", label: "EMA Threshold (%)", step: "0.1", min: "0" },
];
type ModeControlProfileKey = "fast_current" | "balanced" | "conservative" | "custom";

const getPnLClass = (value: number): string => {
  if (!Number.isFinite(value)) {
    return "";
  }
  if (value >= PNL_PER_TRADE_THRESHOLD) {
    return "text-positive";
  }
  if (value <= -PNL_PER_TRADE_THRESHOLD) {
    return "text-negative";
  }
  return "";
};

const calculatePnLPerTrade = (pnl: number, trades: number): number => {
  if (!Number.isFinite(pnl) || !Number.isFinite(trades) || trades === 0) {
    return 0;
  }
  return pnl / trades;
};

/**
 * Berechnet Netto-PNL und Fees aus Brutto-PNL basierend auf Fee-Settings.
 */
const calculateNetPnlAndFees = (grossPnl: number, feeRate: number, trades: number = 0): { netPnl: number; fees: number } => {
  if (!Number.isFinite(grossPnl) || !Number.isFinite(feeRate) || grossPnl === 0) {
    return { netPnl: grossPnl, fees: 0 };
  }
  
  let estimatedNotional: number;
  
  if (trades > 0) {
    // Bei vielen Trades: Schätze Notional basierend auf durchschnittlichem Trade-Volumen
    const avgPnlPerTrade = Math.abs(grossPnl) / trades;
    
    // Dynamische Schätzung basierend auf PNL pro Trade:
    // - Bei sehr niedrigem PNL pro Trade (< 0.01): Return ist sehr niedrig (0.1-0.5%)
    // - Bei niedrigem PNL pro Trade (0.01-0.1): Return ist niedrig (0.5-1%)
    // - Bei mittlerem PNL pro Trade (0.1-1): Return ist mittel (1-2%)
    // - Bei hohem PNL pro Trade (> 1): Return ist hoch (2-5%)
    let pnlToNotionalRatioPerTrade: number;
    if (avgPnlPerTrade < 0.01) {
      // Sehr viele kleine Trades: Return ist sehr niedrig
      pnlToNotionalRatioPerTrade = 0.001; // 0.1%
    } else if (avgPnlPerTrade < 0.1) {
      // Viele kleine Trades: Return ist niedrig
      pnlToNotionalRatioPerTrade = 0.005; // 0.5%
    } else if (avgPnlPerTrade < 1.0) {
      // Mittlere Trades: Return ist mittel
      pnlToNotionalRatioPerTrade = 0.01; // 1%
    } else {
      // Große Trades: Return ist höher
      pnlToNotionalRatioPerTrade = 0.02; // 2%
    }
    
    const estimatedNotionalPerTrade = avgPnlPerTrade / pnlToNotionalRatioPerTrade;
    estimatedNotional = estimatedNotionalPerTrade * trades;
  } else {
    // Keine Trade-Anzahl: Verwende konservative Schätzung
    const pnlToNotionalRatio = 0.01; // 1% (konservativer als 2%)
    estimatedNotional = Math.abs(grossPnl) / pnlToNotionalRatio;
  }
  
  const fees = estimatedNotional * feeRate;
  const netPnl = grossPnl - fees;
  
  return { netPnl, fees };
};

/**
 * Berechnet Netto-PNL aus Brutto-PNL basierend auf Fee-Settings.
 * (Legacy-Funktion für Kompatibilität)
 */
const calculateNetPnl = (grossPnl: number, feeRate: number): number => {
  return calculateNetPnlAndFees(grossPnl, feeRate).netPnl;
};

/**
 * Extrahiert Fee-Rate aus Settings.
 * 
 * Diese Funktion verwendet die Settings als "Master Settings" für die Fee-Berechnung.
 * Wenn Fee-Settings in den Settings vorhanden sind, werden diese verwendet.
 * Wenn nicht, werden Default-Werte verwendet (0.2% für Spot, 0.08% für Futures).
 */
const getFeeRateFromSettings = (settings: SettingsResponse | null): number => {
  if (!settings?.settings) {
    // Keine Settings geladen: verwende Default für Spot
    return 0.002; // Default: 0.2% für Spot
  }
  
  const binance = settings.settings.binance as Record<string, unknown> | undefined;
  if (!binance) {
    // Keine Binance-Settings: verwende Default für Spot
    return 0.002; // Default: 0.2% für Spot
  }
  
  // Verwende fee_model aus Settings (Master Settings)
  // Wenn nicht gesetzt, wird "spot" als Default verwendet
  const feeModel = (binance.fee_model as string | undefined)?.toLowerCase() || "spot";
  
  if (feeModel === "futures") {
    // Futures: Round-trip fees (entry + exit)
    const feeTakerRate = (binance.fee_taker_rate as number | undefined) || 0.0004;
    // Round-trip fees: 0.04% entry + 0.04% exit = 0.08% total
    return feeTakerRate * 2;
  } else {
    // Spot: Entry + Exit fees
    // Verwende fee_entry_rate und fee_exit_rate aus Settings (Master Settings)
    const feeEntryRate = (binance.fee_entry_rate as number | undefined) || 0.001;
    const feeExitRate = (binance.fee_exit_rate as number | undefined) || 0.001;
    // Spot: 0.1% entry + 0.1% exit = 0.2% total (default)
    return feeEntryRate + feeExitRate;
  }
};

type SummarySlice = {
  trades: number;
  wins: number;
  win_rate: number;
  pnl: number;
  fees: number;
  avg_notional?: number;
  pnl_per_trade_pct?: number;
  symbol?: string;
  strategy?: string;
  side?: string;
};

type StrategyOption = {
  id: string;
  label: string;
  enabled: boolean;
};

type SortDirection = "asc" | "desc";
type SortState = { key: string; direction: SortDirection } | null;

function renderSliceTable(
  title: string,
  rows: SummarySlice[],
  columns: Array<{
    key: keyof SummarySlice | string;
    label: string;
    formatter?: (value: any, row?: SummarySlice) => string;
    cellRenderer?: (value: any, row: SummarySlice) => JSX.Element;
    sortValue?: (row: SummarySlice) => string | number | null | undefined;
    sortable?: boolean;
  }>,
  options?: {
    sortState?: SortState;
    onSort?: (key: string) => void;
  }
): JSX.Element {
  if (!rows || rows.length === 0) {
    return (
      <div className="card card--compact">
        <h4>{title}</h4>
        <p>Keine Daten.</p>
      </div>
    );
  }
  const { sortState, onSort } = options ?? {};
  const sortedRows = [...rows];
  if (sortState) {
    const sortColumn = columns.find((column) => String(column.key) === sortState.key);
    const sortValue =
      sortColumn?.sortValue ?? ((row: SummarySlice) => (row as Record<string, unknown>)[sortState.key] as any);
    const direction = sortState.direction === "asc" ? 1 : -1;
    sortedRows.sort((a, b) => {
      const aVal = sortValue(a);
      const bVal = sortValue(b);
      const aMissing = aVal == null || (typeof aVal === "number" && Number.isNaN(aVal));
      const bMissing = bVal == null || (typeof bVal === "number" && Number.isNaN(bVal));
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (typeof aVal === "string" || typeof bVal === "string") {
        return String(aVal).localeCompare(String(bVal)) * direction;
      }
      return ((aVal as number) - (bVal as number)) * direction;
    });
  }
  return (
    <div className="card card--compact">
      <h4>{title}</h4>
      <div className="table-wrapper">
        <table className="table table--compact">
          <thead>
            <tr>
              {columns.map((column) => {
                const key = String(column.key);
                const isSortable = !!onSort && column.sortable !== false;
                const isActive = sortState?.key === key;
                const indicator = isActive ? (sortState?.direction === "asc" ? "▲" : "▼") : "";
                return (
                  <th key={key}>
                    {isSortable ? (
                      <button
                        type="button"
                        className={`table__sort-button${isActive ? " table__sort-button--active" : ""}`}
                        onClick={() => onSort?.(key)}
                        aria-label={`Sortiere nach ${column.label}`}
                      >
                        <span>{column.label}</span>
                        <span className="table__sort-indicator">{indicator}</span>
                      </button>
                    ) : (
                      column.label
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, index) => (
              <tr key={`${title}-${index}`}>
                {columns.map((column) => {
                  const value = (row as Record<string, unknown>)[column.key as string];
                  if (column.cellRenderer) {
                    return <td key={`${column.key as string}-${index}`}>{column.cellRenderer(value, row)}</td>;
                  }
                  if (column.formatter) {
                    return <td key={`${column.key as string}-${index}`}>{column.formatter(value, row)}</td>;
                  }
                  return <td key={`${column.key as string}-${index}`}>{value != null ? String(value) : "—"}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function BacktestPage(): JSX.Element {
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [tooltips, setTooltips] = useState<Record<string, string>>({});
  const [selectedTemplate, setSelectedTemplate] = useState<string>(CURRENT_TEMPLATE_SENTINEL);
  const [settingsPreview, setSettingsPreview] = useState<Record<string, unknown> | null>(null);
  const [baseSettings, setBaseSettings] = useState<Record<string, unknown> | null>(null);
  const [settingsFields, setSettingsFields] = useState<ReturnType<typeof extractEditableFields>>([]);
  const [settingsJson, setSettingsJson] = useState<string>("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [fieldDrafts, setFieldDrafts] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [filterSearch, setFilterSearch] = useState<string>("");
  const [availableStrategies, setAvailableStrategies] = useState<StrategyOption[]>([]);
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [settings, setSettings] = useState<SettingsResponse | null>(null);

  const [startDate, setStartDate] = useState<string>(() => {
    const date = new Date();
    date.setMonth(date.getMonth() - 2);
    return formatDateInput(date);
  });
  const [endDate, setEndDate] = useState<string>(() => formatDateInput(new Date()));
  const [startHour, setStartHour] = useState<number | null>(null);
  const [endHour, setEndHour] = useState<number | null>(null);
  const [hourTimezone, setHourTimezone] = useState<string>("UTC");
  const [notes, setNotes] = useState<string>("");
  const [simulateModeController, setSimulateModeController] = useState<boolean>(false);
  const [useModeControlThresholdOverride, setUseModeControlThresholdOverride] = useState<boolean>(false);
  const [modeControlThresholdDrafts, setModeControlThresholdDrafts] = useState<Record<string, string>>({});
  const [selectedModeControlProfile, setSelectedModeControlProfile] = useState<ModeControlProfileKey>("fast_current");
  const [flipAllBranches, setFlipAllBranches] = useState<boolean>(false);
  const [useAdaptiveEntry, setUseAdaptiveEntry] = useState<boolean>(false);
  const [cacheOnly, setCacheOnly] = useState<boolean>(true);
  const [backtestDataSource, setBacktestDataSource] = useState<string>("KRAKEN");
  const [sampleRatio, setSampleRatio] = useState<string>("1.0");
  const [globalEntryTightness, setGlobalEntryTightness] = useState<string>("1");
  const [maxParallelCores, setMaxParallelCores] = useState<number>(2);
  const [startLoading, setStartLoading] = useState<boolean>(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [startSuccess, setStartSuccess] = useState<string | null>(null);

  const [backtests, setBacktests] = useState<ManualBacktestMeta[]>([]);
  const [listLoading, setListLoading] = useState<boolean>(false);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedBacktestId, setSelectedBacktestId] = useState<string | null>(null);
  const [backtestPage, setBacktestPage] = useState<number>(1);

  const [detail, setDetail] = useState<ManualBacktestMeta | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [logState, setLogState] = useState<{ lines: string[]; path: string | null; loading: boolean }>({
    lines: [],
    path: null,
    loading: false
  });
  const [compareData, setCompareData] = useState<BacktestCompareResponse | null>(null);
  const [compareLoading, setCompareLoading] = useState<boolean>(false);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [compareNotice, setCompareNotice] = useState<string | null>(null);
  const [compareFilter, setCompareFilter] = useState<string>("");
  const [compareSort, setCompareSort] = useState<"latest_pnl" | "avg_pnl" | "total_trades">("latest_pnl");
  const [compareMode, setCompareMode] = useState<"normal" | "with_mode_controller">("normal");
  const [activeSection, setActiveSection] = useState<"compare" | "runs" | "mode" | "templates" | "charts" | "replays" | "shadow">("runs");
  const [compareSelections, setCompareSelections] = useState<string[]>(["", "", "", "", ""]);
  const [chartGranularity, setChartGranularity] = useState<"day" | "week" | "month">("day");
  const [chartStrategy, setChartStrategy] = useState<string>("");
  const [sliceSorts, setSliceSorts] = useState<Record<string, SortState>>({});
  const [modeFilter, setModeFilter] = useState<string>("");
  const [modeSort, setModeSort] = useState<"live_pnl" | "delta_pnl" | "live_trades" | "live_share">("live_pnl");
  const [modeTableSort, setModeTableSort] = useState<SortState>({ key: "live_pnl", direction: "desc" });
  const [replayItems, setReplayItems] = useState<TradeReplayItem[]>([]);
  const [replayTotal, setReplayTotal] = useState<number>(0);
  const [replayLoading, setReplayLoading] = useState<boolean>(false);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [replayFilter, setReplayFilter] = useState<string>("");
  const [replayStatus, setReplayStatus] = useState<string>("all");
  const [replayMode, setReplayMode] = useState<string>("all");
  const [shadowItems, setShadowItems] = useState<ShadowMirrorItem[]>([]);
  const [shadowTotal, setShadowTotal] = useState<number>(0);
  const [shadowLoading, setShadowLoading] = useState<boolean>(false);
  const [shadowError, setShadowError] = useState<string | null>(null);
  const [shadowStatus, setShadowStatus] = useState<string>("all");
  const [shadowFilter, setShadowFilter] = useState<string>("");
  const [shadowConfig, setShadowConfig] = useState<ShadowMirrorConfig | null>(null);
  const [shadowConfigSaving, setShadowConfigSaving] = useState<boolean>(false);

  const [templateName, setTemplateName] = useState<string>("");
  const [templateDescription, setTemplateDescription] = useState<string>("");
  const [templateTags, setTemplateTags] = useState<string>("");
  const [templateSaving, setTemplateSaving] = useState<boolean>(false);
  const [templateError, setTemplateError] = useState<string | null>(null);
  const [templateSuccess, setTemplateSuccess] = useState<string | null>(null);

  const loadTemplatesAndSchema = useCallback(async () => {
    try {
      const [templateResponse, currentSettingsResponse, schemaResponse] = await Promise.all([
        fetchTemplates(),
        fetchCurrentSettings(),
        fetchSettingsSchema()
      ]);

      setTemplates(templateResponse.items);
      setTooltips(schemaResponse.tooltips ?? {});

      const fields = extractEditableFields(currentSettingsResponse.settings, schemaResponse.tooltips ?? {});
      setSettingsFields(fields);
      setSettingsPreview(currentSettingsResponse.settings);
      setBaseSettings(currentSettingsResponse.settings);
      setSettingsJson(JSON.stringify(currentSettingsResponse.settings, null, 2));
      setJsonError(null);
      setFieldDrafts({});
      setFieldErrors({});
    } catch (err) {
      console.error("Failed to load template/settings preview:", err);
    }
  }, []);

  const refreshTemplates = useCallback(async () => {
    try {
      const response = await fetchTemplates();
      setTemplates(response.items);
    } catch (err) {
      console.error("Failed to refresh templates:", err);
    }
  }, []);

  const loadTradeReplays = useCallback(async () => {
    setReplayLoading(true);
    setReplayError(null);
    try {
      const response: TradeReplayResponse = await fetchTradeReplays({ limit: 500 });
      setReplayItems(response.items ?? []);
      setReplayTotal(response.total ?? response.items?.length ?? 0);
    } catch (err: any) {
      setReplayError(err?.response?.data?.detail ?? "Replay-Auswertung konnte nicht geladen werden.");
    } finally {
      setReplayLoading(false);
    }
  }, []);

  const loadShadowMirrors = useCallback(async () => {
    setShadowLoading(true);
    setShadowError(null);
    try {
      const [mirrors, cfg]: [ShadowMirrorResponse, ShadowMirrorConfig] = await Promise.all([
        fetchShadowMirrors({ limit: 500 }),
        fetchShadowMirrorConfig(),
      ]);
      setShadowItems(mirrors.items ?? []);
      setShadowTotal(mirrors.total ?? mirrors.items?.length ?? 0);
      setShadowConfig(cfg);
    } catch (err: any) {
      setShadowError(err?.response?.data?.detail ?? "Shadow-Mirror Daten konnten nicht geladen werden.");
    } finally {
      setShadowLoading(false);
    }
  }, []);

  useEffect(() => {
    const strategyCfg =
      settingsPreview && typeof settingsPreview === "object"
        ? (((settingsPreview as Record<string, unknown>).strategy as Record<string, unknown>) ?? {})
        : {};
    const modeControlCfg = (strategyCfg.mode_control as Record<string, unknown>) ?? {};
    const thresholdCfg = (modeControlCfg.thresholds as Record<string, unknown>) ?? {};
    const nextDrafts: Record<string, string> = {};
    MODE_CONTROL_OVERRIDE_FIELDS.forEach(({ key }) => {
      const raw = thresholdCfg[key];
      if (typeof raw === "number" && Number.isFinite(raw)) {
        nextDrafts[key] = String(raw);
      } else if (typeof raw === "string" && raw.trim() !== "") {
        nextDrafts[key] = raw.trim();
      } else {
        nextDrafts[key] = "";
      }
    });
    setModeControlThresholdDrafts(nextDrafts);
    setSelectedModeControlProfile("fast_current");
  }, [settingsPreview]);

  const handleSliceSort = useCallback((tableId: string, key: string) => {
    setSliceSorts((prev) => {
      const current = prev[tableId];
      let direction: SortDirection = "desc";
      if (current?.key === key) {
        direction = current.direction === "desc" ? "asc" : "desc";
      }
      return { ...prev, [tableId]: { key, direction } };
    });
  }, []);

  useEffect(() => {
    void loadTemplatesAndSchema();
  }, [loadTemplatesAndSchema]);

  const loadTemplatePreview = useCallback(
    async (templateId: string) => {
      const applyPreview = (payload: Record<string, unknown>) => {
        setSettingsPreview(payload);
        setSettingsFields(extractEditableFields(payload, tooltips));
        setSettingsJson(JSON.stringify(payload, null, 2));
        setJsonError(null);
        setFieldDrafts({});
        setFieldErrors({});
      };

      if (templateId === CURRENT_TEMPLATE_SENTINEL) {
        try {
          const current = await fetchCurrentSettings();
          setBaseSettings(current.settings);
          applyPreview(current.settings);
        } catch (err) {
          console.error("Failed to load current settings preview:", err);
        }
        return;
      }
      if (!templateId) {
        setSettingsPreview(null);
        setBaseSettings(null);
        setSettingsFields([]);
        setSettingsJson("{}");
        setJsonError(null);
        setFieldDrafts({});
        setFieldErrors({});
        return;
      }
      try {
        const tpl = await fetchTemplate(templateId);
        // Templates are treated as patches over the current settings, so that minimal templates
        // (e.g. only {strategy: {...}}) don't wipe strategy_pipeline and other required roots.
        let base = baseSettings;
        if (!base) {
          const current = await fetchCurrentSettings();
          base = current.settings as Record<string, unknown>;
          setBaseSettings(base);
        }
        const merged = deepMerge(base as any, (tpl.payload ?? {}) as any) as Record<string, unknown>;
        applyPreview(merged);
      } catch (err) {
        console.error("Failed to load template preview:", err);
        setSettingsPreview(null);
        setBaseSettings(null);
        setSettingsFields([]);
        setSettingsJson("{}");
        setJsonError(null);
        setFieldDrafts({});
        setFieldErrors({});
      }
    },
    [tooltips, baseSettings]
  );

  useEffect(() => {
    void loadTemplatePreview(selectedTemplate);
  }, [selectedTemplate, loadTemplatePreview]);

  const selectedTemplateLabel = useMemo(() => {
    if (selectedTemplate === CURRENT_TEMPLATE_SENTINEL) {
      return "Aktuelle Settings";
    }
    const match = templates.find((tpl) => tpl.id === selectedTemplate);
    return match?.name ?? selectedTemplate;
  }, [selectedTemplate, templates]);

  const filteredFields = useMemo(() => {
    if (!filterSearch.trim()) {
      return settingsFields;
    }
    const needle = filterSearch.trim().toLowerCase();
    return settingsFields.filter(
      (field) =>
        field.displayName.toLowerCase().includes(needle) || field.path.toLowerCase().includes(needle)
    );
  }, [settingsFields, filterSearch]);

  const visibleFieldCount = filteredFields.length;

  useEffect(() => {
    if (!settingsPreview || typeof settingsPreview !== "object") {
      setAvailableStrategies([]);
      setSelectedStrategies([]);
      return;
    }
    const pipelineRaw = (settingsPreview as Record<string, unknown>).strategy_pipeline;
    const pipeline = Array.isArray(pipelineRaw) ? (pipelineRaw as Array<Record<string, any>>) : [];
    const options: StrategyOption[] = pipeline.map((entry, index) => {
      const name = typeof entry?.name === "string" && entry.name ? entry.name : `strategy_${index}`;
      const alias = typeof entry?.params?.alias === "string" && entry.params.alias ? entry.params.alias : name;
      const enabled = entry?.enabled !== false;
      const label = alias !== name ? `${alias} (${name})` : alias;
      return { id: alias, label, enabled };
    });
    setAvailableStrategies(options);
    setSelectedStrategies((prev) => {
      if (!prev.length) {
        return [];
      }
      const validIds = new Set(options.map((opt) => opt.id));
      return prev.filter((id) => validIds.has(id));
    });
  }, [settingsPreview]);

  const refreshList = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const { items } = await listBacktests(100, 0);
      setBacktests(items);
      if (!selectedBacktestId && items.length > 0) {
        setSelectedBacktestId(items[0].id);
      } else if (selectedBacktestId) {
        const stillExists = items.some((item) => item.id === selectedBacktestId);
        if (!stillExists) {
          setSelectedBacktestId(items[0]?.id ?? null);
        }
      }
    } catch (err: any) {
      console.error("Failed to load backtests:", err);
      setListError(err?.response?.data?.detail ?? "Backtests konnten nicht geladen werden.");
    } finally {
      setListLoading(false);
    }
  }, [selectedBacktestId]);

  useEffect(() => {
    void refreshList();
    const interval = window.setInterval(() => {
      void refreshList();
    }, 15000);
    return () => window.clearInterval(interval);
  }, [refreshList]);

  const loadCompare = useCallback(async () => {
    setCompareLoading(true);
    setCompareError(null);
    setCompareNotice(null);
    try {
      const selectedIds = compareSelections.filter((id) => id.trim() !== "");
      const data = await fetchBacktestCompare(5, compareMode, selectedIds.length ? selectedIds : undefined);
      setCompareData(data);
      if (data.missing_ids?.length || data.skipped_ids?.length) {
        const missing = data.missing_ids?.length ? `Nicht gefunden: ${data.missing_ids.join(", ")}` : "";
        const skipped = data.skipped_ids?.length ? `Übersprungen: ${data.skipped_ids.join(", ")}` : "";
        setCompareNotice([missing, skipped].filter(Boolean).join(" | "));
      }
      setCompareSelections((prev) => {
        if (prev.some((value) => value.trim() !== "")) {
          return prev;
        }
        const next = data.backtests.map((bt) => bt.id);
        while (next.length < 5) {
          next.push("");
        }
        return next.slice(0, 5);
      });
    } catch (err: any) {
      console.error("Failed to load backtest compare:", err);
      setCompareError(err?.response?.data?.detail ?? "Vergleichsdaten konnten nicht geladen werden.");
      setCompareData(null);
    } finally {
      setCompareLoading(false);
    }
  }, [compareMode, compareSelections]);

  useEffect(() => {
    if (activeSection !== "compare") {
      return;
    }
    void loadCompare();
  }, [loadCompare, activeSection]);

  useEffect(() => {
    if (activeSection !== "replays") {
      return;
    }
    void loadTradeReplays();
  }, [loadTradeReplays, activeSection]);

  useEffect(() => {
    if (activeSection !== "shadow") {
      return;
    }
    void loadShadowMirrors();
  }, [loadShadowMirrors, activeSection]);

  const loadDetail = useCallback(
    async (backtestId: string | null) => {
      if (!backtestId) {
        setDetail(null);
        setLogState((prev) => ({ ...prev, lines: [], path: null }));
        return;
      }
      setDetailLoading(true);
      setDetailError(null);
      try {
        const { backtest }: BacktestDetailResponse = await fetchBacktest(backtestId);
        setDetail(backtest);
      } catch (err: any) {
        console.error("Failed to load backtest detail:", err);
        setDetail(null);
        setDetailError(err?.response?.data?.detail ?? "Backtest konnte nicht geladen werden.");
      } finally {
        setDetailLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void loadDetail(selectedBacktestId);
  }, [selectedBacktestId, loadDetail]);

  const allStrategyIds = useMemo(() => availableStrategies.map((opt) => opt.id), [availableStrategies]);

  const handleStrategyToggle = useCallback((id: string, checked: boolean) => {
    setSelectedStrategies((prev) => {
      if (checked) {
        if (prev.includes(id)) {
          return prev;
        }
        return [...prev, id];
      }
      return prev.filter((value) => value !== id);
    });
  }, []);

  const handleSelectAllStrategies = useCallback(() => {
    setSelectedStrategies(allStrategyIds);
  }, [allStrategyIds]);

  const handleClearStrategies = useCallback(() => {
    setSelectedStrategies([]);
  }, []);

  const loadLog = useCallback(
    async (backtestId: string | null) => {
      if (!backtestId) {
        setLogState({ lines: [], path: null, loading: false });
        return;
      }
      setLogState((prev) => ({ ...prev, loading: true }));
      try {
        const response: BacktestLogResponse = await fetchBacktestLog(backtestId, 400);
        setLogState({ lines: response.lines, path: response.path_absolute, loading: false });
      } catch (err) {
        console.error("Failed to load backtest log:", err);
        setLogState({ lines: ["Log konnte nicht geladen werden."], path: null, loading: false });
      }
    },
    []
  );

  const handleCancel = useCallback(
    async (backtestId: string) => {
      if (!confirm("Backtest wirklich abbrechen?")) {
        return;
      }
      try {
        await cancelBacktest(backtestId);
        await refreshList();
        if (selectedBacktestId === backtestId) {
          await loadDetail(backtestId);
        }
      } catch (err: any) {
        console.error("Failed to cancel backtest:", err);
        alert(err?.response?.data?.detail ?? "Backtest konnte nicht abgebrochen werden.");
      }
    },
    [selectedBacktestId, refreshList, loadDetail]
  );

  useEffect(() => {
    void loadLog(selectedBacktestId);
  }, [selectedBacktestId, loadLog]);

  const updateSettingAtPath = (path: string, value: Primitive | Record<string, unknown> | unknown[]) => {
    setSettingsPreview((prev) => {
      const next = setNestedValueImmutable(prev, path, value);
      setSettingsJson(JSON.stringify(next, null, 2));
      setJsonError(null);
      return next;
    });
  };

  const clearFieldError = (path: string) => {
    setFieldErrors((prev) => {
      if (!(path in prev)) return prev;
      const next = { ...prev };
      delete next[path];
      return next;
    });
  };

  const handleStringChange = (path: string, raw: string) => {
    clearFieldError(path);
    updateSettingAtPath(path, raw);
  };

  const normalizeDecimalInput = (raw: string) => {
    const cleaned = raw.trim();
    if (cleaned.includes(",") && !cleaned.includes(".")) {
      return cleaned.replace(",", ".");
    }
    return cleaned;
  };

  const buildModeControlThresholdOverride = () => {
    if (!useModeControlThresholdOverride) {
      return null;
    }
    const thresholds: Record<string, number> = {};
    for (const field of MODE_CONTROL_OVERRIDE_FIELDS) {
      const raw = (modeControlThresholdDrafts[field.key] ?? "").trim();
      if (!raw) {
        continue;
      }
      const normalized = normalizeDecimalInput(raw);
      const parsed = Number(normalized);
      if (!Number.isFinite(parsed)) {
        throw new Error(`Ungültiger Mode-Controller-Wert für ${field.label}`);
      }
      thresholds[field.key] = parsed;
    }
    return thresholds;
  };

  const applyModeControlProfile = (profile: ModeControlProfileKey) => {
    const base = { ...modeControlThresholdDrafts };
    if (profile === "fast_current") {
      setUseModeControlThresholdOverride(true);
      setSelectedModeControlProfile("fast_current");
      setModeControlThresholdDrafts(base);
      return;
    }
    const merged = { ...base };
    if (profile === "balanced") {
      merged.lookback_trades = "4";
      merged.min_significant_trades = "2";
      merged.noise_threshold_pct = "0.08";
      merged.strong_impulse_pct = "14";
      merged.min_test_avg_pct = "1.4";
      merged.min_test_ema_pct = "0.4";
      merged.min_live_trades = "3";
      merged.cooldown_minutes = "5";
      merged.ema_period = "3";
      merged.ema_threshold_pct = "0.6";
    } else if (profile === "conservative") {
      merged.lookback_trades = "6";
      merged.min_significant_trades = "3";
      merged.noise_threshold_pct = "0.10";
      merged.strong_impulse_pct = "18";
      merged.min_test_avg_pct = "1.0";
      merged.min_test_ema_pct = "0.3";
      merged.min_live_trades = "4";
      merged.cooldown_minutes = "8";
      merged.ema_period = "5";
      merged.ema_threshold_pct = "0.8";
    }
    setUseModeControlThresholdOverride(true);
    setSelectedModeControlProfile(profile);
    setModeControlThresholdDrafts(merged);
  };

  const handleNumberChange = (path: string, raw: string) => {
    if (!raw.trim()) {
      clearFieldError(path);
      updateSettingAtPath(path, null);
      return;
    }
    const normalized = normalizeDecimalInput(raw);
    const parsed = Number(normalized);
    if (Number.isNaN(parsed)) {
      setFieldErrors((prev) => ({ ...prev, [path]: "Ungültige Zahl" }));
      return;
    }
    clearFieldError(path);
    updateSettingAtPath(path, parsed);
  };

  const handleBooleanChange = (path: string, checked: boolean) => {
    clearFieldError(path);
    updateSettingAtPath(path, checked);
  };

  const handleJsonFieldChange = (path: string, raw: string) => {
    if (!raw.trim()) {
      clearFieldError(path);
      setFieldDrafts((prev) => {
        if (!(path in prev)) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
      });
      updateSettingAtPath(path, null);
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      clearFieldError(path);
      updateSettingAtPath(path, parsed as Record<string, unknown> | unknown[]);
      setFieldDrafts((prev) => {
        if (!(path in prev)) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
      });
    } catch {
      setFieldErrors((prev) => ({ ...prev, [path]: "Ungültiges JSON" }));
      setFieldDrafts((prev) => ({ ...prev, [path]: raw }));
    }
  };

  const handleRawJsonChange = (value: string) => {
    setSettingsJson(value);
    try {
      if (!value.trim()) {
        throw new Error("leer");
      }
      const parsed = JSON.parse(value);
      setSettingsPreview(parsed as Record<string, unknown>);
      setSettingsFields(extractEditableFields(parsed as Record<string, unknown>, tooltips));
      setJsonError(null);
      setFieldDrafts({});
      setFieldErrors({});
    } catch {
      setJsonError("Settings JSON ist ungültig. Änderungen werden nicht übernommen.");
    }
  };

  const handleSaveTemplate = async (event: FormEvent) => {
    event.preventDefault();
    setTemplateError(null);
    setTemplateSuccess(null);
    const name = templateName.trim();
    if (!name) {
      setTemplateError("Template-Name ist erforderlich.");
      return;
    }
    if (!settingsPreview) {
      setTemplateError("Keine Settings geladen. Bitte zuerst Settings oder Template auswählen.");
      return;
    }
    if (jsonError) {
      setTemplateError("Bitte behebe die Fehler im Raw JSON.");
      return;
    }
    if (Object.keys(fieldErrors).length > 0) {
      setTemplateError("Bitte behebe die markierten Feldfehler.");
      return;
    }
    setTemplateSaving(true);
    try {
      const modeControlThresholdOverride = buildModeControlThresholdOverride();
      let templatePayload = settingsPreview as Record<string, unknown>;
      if (modeControlThresholdOverride && Object.keys(modeControlThresholdOverride).length > 0) {
        templatePayload = setNestedValueImmutable(
          templatePayload,
          "strategy.mode_control.thresholds",
          modeControlThresholdOverride
        ) as Record<string, unknown>;
      }
      const tags = templateTags
        ? templateTags.split(",").map((tag) => tag.trim()).filter(Boolean)
        : [];
      await createTemplate({
        name,
        description: templateDescription.trim() || undefined,
        tags,
        payload: templatePayload,
      });
      setTemplateSuccess(`Template "${name}" gespeichert.`);
      setTemplateName("");
      setTemplateDescription("");
      setTemplateTags("");
      await refreshTemplates();
    } catch (err: any) {
      console.error("Failed to create template:", err);
      setTemplateError(err?.response?.data?.detail ?? "Template konnte nicht gespeichert werden.");
    } finally {
      setTemplateSaving(false);
    }
  };

  const handleDeleteTemplate = async (id: string) => {
    if (!window.confirm("Template wirklich löschen?")) {
      return;
    }
    setTemplateError(null);
    setTemplateSuccess(null);
    try {
      await deleteTemplate(id);
      setTemplates((prev) => prev.filter((tpl) => tpl.id !== id));
      if (selectedTemplate === id) {
        setSelectedTemplate(CURRENT_TEMPLATE_SENTINEL);
      }
      setTemplateSuccess(`Template ${id} gelöscht.`);
    } catch (err: any) {
      console.error("Failed to delete template:", err);
      setTemplateError(err?.response?.data?.detail ?? "Template konnte nicht gelöscht werden.");
    }
  };

  const buildFieldInput = (path: string): JSX.Element => {
    const rawValue = getNestedValue(settingsPreview, path);
    const draft = fieldDrafts[path];
    const errorMessage = fieldErrors[path];
    const valueType = typeof rawValue;

    if (path === "kraken.demo_execution_mode") {
      const valueString = draft ?? (rawValue ? String(rawValue) : "kraken");
      return (
        <>
          <select
            className="settings-field__input"
            value={valueString}
            onChange={(event) => handleStringChange(path, event.target.value)}
          >
            <option value="kraken">Kraken Demo</option>
            <option value="internal">Interne Simulation</option>
          </select>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (path.endsWith("enabled") && valueType === "boolean") {
      return (
        <>
          <label className="settings-field__checkbox">
            <input
              type="checkbox"
              checked={Boolean(rawValue)}
              onChange={(event) => handleBooleanChange(path, event.target.checked)}
            />
            <span>Aktiviert</span>
          </label>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (valueType === "number") {
      const valueString = rawValue === null || rawValue === undefined ? "" : String(rawValue);
      return (
        <>
          <input
            className="settings-field__input"
            type="number"
            value={valueString}
            onChange={(event) => handleNumberChange(path, event.target.value)}
          />
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (valueType === "boolean") {
      return (
        <>
          <label className="settings-field__checkbox">
            <input
              type="checkbox"
              checked={Boolean(rawValue)}
              onChange={(event) => handleBooleanChange(path, event.target.checked)}
            />
            <span>Aktiviert</span>
          </label>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (rawValue && typeof rawValue === "object") {
      const textValue = draft ?? JSON.stringify(rawValue, null, 2);
      return (
        <>
          <textarea
            className="settings-field__textarea"
            value={textValue}
            onChange={(event) => handleJsonFieldChange(path, event.target.value)}
            rows={4}
            spellCheck={false}
          />
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    const valueString =
      draft ?? (rawValue === null || rawValue === undefined ? "" : String(rawValue as Primitive));
    return (
      <>
        <input
          className="settings-field__input"
          type="text"
          value={valueString}
          onChange={(event) => handleStringChange(path, event.target.value)}
        />
        {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
      </>
    );
  };

  const handleStart = async (event: FormEvent) => {
    event.preventDefault();
    setStartError(null);
    setStartSuccess(null);
    setStartLoading(true);
    try {
      const parsedSampleRatio = parseFloat(normalizeDecimalInput(sampleRatio));
      const sampleRatioValue = Number.isFinite(parsedSampleRatio) ? parsedSampleRatio : undefined;
      let tightnessValue: number | null | undefined = undefined;
      if (globalEntryTightness.trim() !== "") {
        const parsed = parseFloat(normalizeDecimalInput(globalEntryTightness));
        if (!Number.isFinite(parsed) || parsed <= 0) {
          throw new Error("Bitte eine gültige globale Entry-Tightness > 0 eingeben.");
        }
        tightnessValue = parsed;
      }
      const modeControlThresholdOverride = buildModeControlThresholdOverride();
      const settingsOverride =
        modeControlThresholdOverride && Object.keys(modeControlThresholdOverride).length > 0
          ? {
              strategy: {
                mode_control: {
                  thresholds: modeControlThresholdOverride,
                },
              },
            }
          : undefined;
      await startBacktest({
        template_id: selectedTemplate === CURRENT_TEMPLATE_SENTINEL ? null : selectedTemplate,
        start_date: startDate,
        end_date: endDate,
        notes: notes.trim() ? notes.trim() : undefined,
        settings_override: settingsOverride,
        data_source: backtestDataSource,
        strategy_names: selectedStrategies.length > 0 ? Array.from(new Set(selectedStrategies)) : undefined,
        simulate_mode_controller: simulateModeController,
        max_parallel_cores: maxParallelCores,
        start_hour: startHour ?? undefined,
        end_hour: endHour ?? undefined,
        hour_timezone: hourTimezone || undefined,
        flip_all_branches: flipAllBranches,
        use_adaptive_entry: useAdaptiveEntry,
        cache_only: cacheOnly,
        sample_ratio: sampleRatioValue,
        global_entry_tightness: tightnessValue ?? undefined,
      });
      setStartSuccess("Backtest gestartet.");
      setNotes("");
      await refreshList();
    } catch (err: any) {
      console.error("Failed to start backtest:", err);
      setStartError(err?.response?.data?.detail ?? "Backtest konnte nicht gestartet werden.");
    } finally {
      setStartLoading(false);
    }
  };

  const summarySlices = useMemo<BacktestSummaryPayload | null>(() => {
    return detail?.summary ?? null;
  }, [detail]);

  const strategySummaryRows = useMemo<SummarySlice[]>(() => {
    const rows = (summarySlices?.by_strategy ?? []) as SummarySlice[];
    const settingsSource = detail?.settings_snapshot ?? settingsPreview;
    const pipelineRaw = settingsSource && typeof settingsSource === "object"
      ? (settingsSource as Record<string, unknown>).strategy_pipeline
      : null;
    if (!Array.isArray(pipelineRaw)) {
      return rows;
    }
    const pipeline = pipelineRaw as Array<Record<string, any>>;
    const rowByStrategy = new Map(rows.map((row) => [row.strategy ?? "", row]));
    const added = new Set<string>();
    const merged: SummarySlice[] = [];
    pipeline.forEach((entry, index) => {
      const name = typeof entry?.name === "string" && entry.name ? entry.name : `strategy_${index}`;
      const alias = typeof entry?.params?.alias === "string" && entry.params.alias ? entry.params.alias : name;
      const enabled = entry?.enabled !== false;
      if (!enabled) {
        return;
      }
      const existing = rowByStrategy.get(alias);
      if (existing) {
        merged.push(existing);
      } else {
        merged.push({
          strategy: alias,
          trades: 0,
          wins: 0,
          win_rate: 0,
          pnl: 0,
          fees: 0,
        });
      }
      added.add(alias);
    });
    rows.forEach((row) => {
      const key = row.strategy ?? "";
      if (!added.has(key)) {
        merged.push(row);
      }
    });
    return merged;
  }, [summarySlices, detail?.settings_snapshot, settingsPreview]);

  const symbolStrategySideRows = useMemo<SummarySlice[] | Array<{ normal: unknown }>>(() => {
    const rawRows = (((summarySlices as any)?.by_symbol_strategy_side_all ?? summarySlices?.by_symbol_strategy_side) ?? []) as Array<SummarySlice & { normal?: unknown }>;
    if (rawRows.length && rawRows[0].normal) {
      return rawRows;
    }
    const rows = rawRows as SummarySlice[];
    const settingsSource = detail?.settings_snapshot ?? settingsPreview;
    const pipelineRaw = settingsSource && typeof settingsSource === "object"
      ? (settingsSource as Record<string, unknown>).strategy_pipeline
      : null;
    if (!Array.isArray(pipelineRaw)) {
      return rows;
    }
    const symbolsFromSummary = new Set(
      ((summarySlices?.by_symbol ?? []) as SummarySlice[])
        .map((row) => row.symbol)
        .filter((symbol): symbol is string => typeof symbol === "string" && symbol.length > 0)
    );
    const rowByKey = new Map(rows.map((row) => [`${row.strategy ?? ""}||${row.symbol ?? ""}||${row.side ?? ""}`, row]));
    const pipeline = pipelineRaw as Array<Record<string, any>>;
    const merged: SummarySlice[] = [];
    const added = new Set<string>();

    const normalizeSymbolKey = (symbol: string) => {
      const trimmed = symbol.trim();
      if (!trimmed) {
        return trimmed;
      }
      const parts = trimmed.split("_");
      return parts[0] || trimmed;
    };

    pipeline.forEach((entry, index) => {
      const name = typeof entry?.name === "string" && entry.name ? entry.name : `strategy_${index}`;
      const baseAlias = typeof entry?.params?.alias === "string" && entry.params.alias ? entry.params.alias : name;
      const enabled = entry?.enabled !== false;
      if (!enabled) {
        return;
      }
      const perSymbolParams = (entry?.per_symbol_params ?? {}) as Record<string, Record<string, unknown>>;
      const perSymbolKeys = Object.keys(perSymbolParams);
      const explicitSymbols = Array.isArray(entry?.symbols) ? (entry.symbols as string[]) : [];
      const symbols = explicitSymbols.length
        ? explicitSymbols
        : perSymbolKeys.length
          ? perSymbolKeys
          : Array.from(symbolsFromSummary);
      if (!symbols.length) {
        return;
      }

      const flipMode = entry?.flip_mode ?? entry?.params?.flip_mode ?? "none";
      let sides: Array<"long" | "short"> = ["long", "short"];
      if (flipMode === "short_only") {
        sides = ["short"];
      } else if (flipMode === "long_only") {
        sides = ["long"];
      } else if (entry?.params?.allow_short === false) {
        sides = ["long"];
      }

      symbols.forEach((symbol) => {
      const perSymbolAlias = perSymbolParams[symbol]?.alias;
      // Keep canonical alias stable across symbols unless an explicit per-symbol alias is configured.
      // This avoids synthetic duplicate rows with zero trades (e.g. alias_symbol vs alias).
      const alias = typeof perSymbolAlias === "string" && perSymbolAlias
        ? perSymbolAlias
        : baseAlias;
      const symbolKey = normalizeSymbolKey(String(symbol));
      sides.forEach((side) => {
          const aliasCandidates = Array.from(
            new Set(
              [alias, baseAlias, name, `${baseAlias}_${String(symbol).toLowerCase()}`]
                .map((value) => String(value ?? "").trim())
                .filter((value) => value.length > 0)
            )
          );
          let existing: SummarySlice | undefined;
          let matchedKey = "";
          for (const candidate of aliasCandidates) {
            const candidateKey = `${candidate}||${symbolKey}||${side}`;
            const row = rowByKey.get(candidateKey);
            if (row) {
              existing = row;
              matchedKey = candidateKey;
              break;
            }
          }

          if (existing) {
            merged.push(existing);
            added.add(matchedKey);
          } else {
            merged.push({
              strategy: alias,
              symbol,
              side,
              trades: 0,
              wins: 0,
              win_rate: 0,
              pnl: 0,
              fees: 0,
            });
            added.add(`${alias}||${symbolKey}||${side}`);
          }
        });
      });
    });

    rows.forEach((row) => {
      const key = `${row.strategy ?? ""}||${row.symbol ?? ""}||${row.side ?? ""}`;
      if (!added.has(key)) {
        merged.push(row);
      }
    });

    return merged;
  }, [summarySlices, detail?.settings_snapshot, settingsPreview]);

  const compareBacktests = useMemo<BacktestCompareMeta[]>(() => {
    return compareData?.backtests ?? [];
  }, [compareData]);

  const compareBranches = useMemo<BacktestCompareBranch[]>(() => {
    const branches = compareData?.branches ?? [];
    const filter = compareFilter.trim().toLowerCase();
    const filtered = filter
      ? branches.filter((branch) => {
          const haystack = `${branch.strategy} ${branch.alias} ${branch.symbol} ${branch.direction}`.toLowerCase();
          return haystack.includes(filter);
        })
      : branches;

    const sorted = [...filtered].sort((a, b) => {
      const metricsA = a.metrics ?? [];
      const metricsB = b.metrics ?? [];
      const latestPnlA = metricsA[0]?.pnl ?? 0;
      const latestPnlB = metricsB[0]?.pnl ?? 0;
      const avgPnlA = metricsA.length ? metricsA.reduce((sum, metric) => sum + metric.pnl, 0) / metricsA.length : 0;
      const avgPnlB = metricsB.length ? metricsB.reduce((sum, metric) => sum + metric.pnl, 0) / metricsB.length : 0;
      const totalTradesA = metricsA.reduce((sum, metric) => sum + (metric.trades ?? 0), 0);
      const totalTradesB = metricsB.reduce((sum, metric) => sum + (metric.trades ?? 0), 0);

      let valueA = latestPnlA;
      let valueB = latestPnlB;
      if (compareSort === "avg_pnl") {
        valueA = avgPnlA;
        valueB = avgPnlB;
      } else if (compareSort === "total_trades") {
        valueA = totalTradesA;
        valueB = totalTradesB;
      }
      if (valueA !== valueB) {
        return valueB - valueA;
      }
      return `${a.strategy}-${a.alias}-${a.symbol}-${a.direction}`.localeCompare(
        `${b.strategy}-${b.alias}-${b.symbol}-${b.direction}`
      );
    });

    return sorted;
  }, [compareData, compareFilter, compareSort]);

  const compareTotals = useMemo(() => {
    if (!compareBacktests.length) {
      return [] as Array<{ pnl: number; trades: number; pnl_per_trade_pct: number | null }>;
    }
    const totals = compareBacktests.map(() => ({ pnl: 0, trades: 0, notional: 0 }));
    compareBranches.forEach((branch) => {
      (branch.metrics || []).forEach((metric, index) => {
        if (!metric || index >= totals.length) {
          return;
        }
        totals[index].pnl += metric.pnl ?? 0;
        const trades = metric.trades ?? 0;
        totals[index].trades += trades;
        const avgNotional = metric.avg_notional ?? 0;
        if (avgNotional && trades) {
          totals[index].notional += avgNotional * trades;
        }
      });
    });
    return totals.map((total) => ({
      pnl: total.pnl,
      trades: total.trades,
      pnl_per_trade_pct: total.notional ? (total.pnl / total.notional) * 100 : null,
    }));
  }, [compareBacktests, compareBranches]);

  const compareOptions = useMemo(() => {
    return backtests
      .filter((bt) => bt.status === "completed")
      .map((bt) => {
        const parts: Array<string | null> = [
          `${bt.start_date} → ${bt.end_date}`,
          formatTimestamp(bt.completed_at),
          bt.template_name ?? bt.template_id ?? "Aktuelle Settings",
          formatHourRange(bt.start_hour ?? null, bt.end_hour ?? null, bt.hour_timezone ?? null),
          bt.flip_all_branches ? "inverted" : null,
          bt.notes ? `Notizen: ${bt.notes}` : null,
        ];
        const label = parts.filter((item): item is string => Boolean(item)).join(" · ");
        return { id: bt.id, label };
      });
  }, [backtests]);

  const replaySummary = useMemo(() => {
    const summary = {
      total: replayItems.length,
      completed: 0,
      failed: 0,
      deferred: 0,
      queued: 0,
      running: 0,
      matched: 0,
      unmatched: 0,
    };
    replayItems.forEach((item) => {
      const status = (item.status || "").toLowerCase();
      if (status === "completed") summary.completed += 1;
      else if (status === "failed") summary.failed += 1;
      else if (status === "deferred") summary.deferred += 1;
      else if (status === "running") summary.running += 1;
      else summary.queued += 1;
      const result = parseReplayResult(item);
      if (result && typeof result.matched === "boolean") {
        if (result.matched) summary.matched += 1;
        else summary.unmatched += 1;
      }
    });
    return summary;
  }, [replayItems]);

  const replayFiltered = useMemo(() => {
    const filter = replayFilter.trim().toLowerCase();
    return replayItems.filter((item) => {
      if (replayStatus !== "all" && (item.status || "").toLowerCase() !== replayStatus) {
        return false;
      }
      if (replayMode !== "all" && (item.mode || "").toLowerCase() !== replayMode) {
        return false;
      }
      if (!filter) {
        return true;
      }
      const haystack = [
        item.strategy,
        item.symbol,
        item.direction,
        item.mode,
        item.status,
        item.trade_id,
      ]
        .filter((value) => value != null)
        .join(" ")
        .toLowerCase();
      return haystack.includes(filter);
    });
  }, [replayItems, replayFilter, replayStatus, replayMode]);

  const shadowSummary = useMemo(() => {
    const summary = { total: shadowItems.length, open: 0, closed: 0, failed: 0 };
    shadowItems.forEach((item) => {
      const status = (item.status || "").toLowerCase();
      if (status === "open") summary.open += 1;
      else if (status === "closed") summary.closed += 1;
      else summary.failed += 1;
    });
    return summary;
  }, [shadowItems]);

  const shadowFiltered = useMemo(() => {
    const filter = shadowFilter.trim().toLowerCase();
    return shadowItems.filter((item) => {
      if (shadowStatus !== "all" && (item.status || "").toLowerCase() !== shadowStatus) {
        return false;
      }
      if (!filter) {
        return true;
      }
      const haystack = [
        item.strategy,
        item.symbol,
        item.direction,
        item.status,
        item.live_trade_id,
        item.shadow_trade_id,
      ]
        .filter((value) => value != null)
        .join(" ")
        .toLowerCase();
      return haystack.includes(filter);
    });
  }, [shadowItems, shadowFilter, shadowStatus]);

  const totalBacktestPages = Math.max(1, Math.ceil(backtests.length / BACKTEST_PAGE_SIZE));
  const pagedBacktests = useMemo(() => {
    const startIndex = (backtestPage - 1) * BACKTEST_PAGE_SIZE;
    return backtests.slice(startIndex, startIndex + BACKTEST_PAGE_SIZE);
  }, [backtests, backtestPage]);

  useEffect(() => {
    setBacktestPage((prev) => Math.min(prev, totalBacktestPages));
  }, [totalBacktestPages]);

  const handleCompareSelectionChange = (index: number, value: string) => {
    setCompareSelections((prev) => {
      const next = [...prev];
      next[index] = value;
      return next;
    });
  };

  const renderPagination = (
    totalPages: number,
    current: number,
    onSelect: (page: number) => void
  ): JSX.Element | null => {
    if (totalPages <= 1) {
      return null;
    }
    return (
      <div className="pagination">
        {Array.from({ length: totalPages }, (_, index) => {
          const page = index + 1;
          const isActive = page === current;
          return (
            <button
              key={page}
              type="button"
              className={`pagination__item ${isActive ? "pagination__item--active" : ""}`}
              onClick={() => onSelect(page)}
              aria-pressed={isActive}
            >
              {page}
            </button>
          );
        })}
      </div>
    );
  };

  type DaySlice = SummarySlice & { day: string; symbol?: string; strategy?: string; side?: string };
  type DayAggregate = {
    day: string;
    ts: number;
    netPnl: number;
    trades: number;
    wins: number;
    notionalTotal: number | null;
  };
  type PeriodAggregate = {
    key: string;
    ts: number;
    netPnl: number;
    trades: number;
    wins: number;
    days: number;
  };
  type TrendPoint = {
    period: string;
    cumulative: number;
    drawdown: number;
    trades: number;
    netPnl: number;
    rolling7: number | null;
    rolling30: number | null;
  };
  type HourPoint = {
    hour: number;
    label: string;
    netPnl: number;
    trades: number;
    wins: number;
    winRate: number;
    pnlPerTrade: number;
    pnlPerTradePct: number | null;
  };
  type ChartSeries = { symbol: string; side: string; data: Array<{ period: string; pnl: number; trades: number }> };

  const chartSourceRows = useMemo<DaySlice[]>(() => {
    const summary = summarySlices as any;
    if (!summary) {
      return [];
    }
    return (summary.by_symbol_strategy_side_day_all ??
      summary.by_symbol_strategy_side_day ??
      []) as DaySlice[];
  }, [summarySlices]);

  const chartStrategies = useMemo(() => {
    const set = new Set<string>();
    chartSourceRows.forEach((row) => {
      if (row.strategy) {
        set.add(row.strategy);
      }
    });
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [chartSourceRows]);

  useEffect(() => {
    if (!chartStrategies.length) {
      setChartStrategy("");
      return;
    }
    if (!chartStrategy || !chartStrategies.includes(chartStrategy)) {
      setChartStrategy(chartStrategies[0]);
    }
  }, [chartStrategies, chartStrategy]);

  const toDayDate = (day: string): Date | null => {
    const date = new Date(`${day}T00:00:00Z`);
    if (Number.isNaN(date.getTime())) {
      return null;
    }
    return date;
  };

  const getMonthKey = (date: Date): { key: string; ts: number } => {
    const year = date.getUTCFullYear();
    const month = `${date.getUTCMonth() + 1}`.padStart(2, "0");
    const ts = Date.UTC(year, date.getUTCMonth(), 1);
    return { key: `${year}-${month}`, ts };
  };

  const getWeekKey = (date: Date): { key: string; ts: number } => {
    const temp = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
    const dayNum = temp.getUTCDay() || 7;
    temp.setUTCDate(temp.getUTCDate() + 4 - dayNum);
    const yearStart = new Date(Date.UTC(temp.getUTCFullYear(), 0, 1));
    const weekNo = Math.ceil((((temp.getTime() - yearStart.getTime()) / 86400000) + 1) / 7);
    const weekStr = `${weekNo}`.padStart(2, "0");
    const monday = new Date(temp.getTime());
    monday.setUTCDate(temp.getUTCDate() - (temp.getUTCDay() || 7) + 1);
    return { key: `${temp.getUTCFullYear()}-W${weekStr}`, ts: monday.getTime() };
  };

  // Lade Settings für Fee-Berechnung (Master Settings)
  // Settings werden als "Master Settings" verwendet - wenn sie im Frontend gespeichert werden,
  // werden sie sofort für die Fee-Berechnung verwendet
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const data = await fetchCurrentSettings();
        if (!active) return;
        setSettings(data);
      } catch (err) {
        console.error(err);
      }
    };
    load();
    // Settings können sich ändern (z.B. wenn im SettingsPage gespeichert wird)
    // Prüfe häufiger, damit Änderungen schnell sichtbar werden
    const interval = window.setInterval(load, 5_000); // Alle 5 Sekunden
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  // Berechne Fee-Rate aus Settings
  const feeRate = useMemo(() => getFeeRateFromSettings(settings), [settings]);

  // Helper-Funktion: Berechnet Netto-PNL, Brutto-PNL und Fees für einen Row
  const getPnlValues = (row: { pnl: number; fees?: number; trades?: number }): { gross: number; net: number; fees: number } => {
    if ((row as any).pnl_net != null && (row as any).fees_current != null) {
      const gross = (row as any).pnl_net + (row as any).fees_current;
      return {
        gross,
        net: (row as any).pnl_net,
        fees: (row as any).fees_current,
      };
    }
    
    const oldNetPnl = row.pnl;
    const oldFees = row.fees ?? 0;
    const grossPnl = oldFees > 0 ? oldNetPnl + oldFees : oldNetPnl;
    if (oldFees > 0) {
      return {
        gross: grossPnl,
        net: oldNetPnl,
        fees: oldFees,
      };
    }
    const trades = row.trades ?? 0;
    const { netPnl, fees } = calculateNetPnlAndFees(grossPnl, feeRate, trades);
    
    return {
      gross: grossPnl,
      net: netPnl,
      fees,
    };
  };

  // Helper-Funktion: Berechnet Netto-PNL für einen Row (für Kompatibilität)
  const getNetPnl = (row: { pnl: number; fees?: number; trades?: number }): number => {
    return getPnlValues(row).net;
  };

  // Helper-Funktion: Berechnet aktuelle Fees für einen Row (für Kompatibilität)
  const getCurrentFees = (row: { pnl: number; fees?: number; trades?: number }): number => {
    return getPnlValues(row).fees;
  };

  // Helper-Funktion: Berechnet Brutto-PNL für einen Row
  const getGrossPnl = (row: { pnl: number; fees?: number; trades?: number }): number => {
    return getPnlValues(row).gross;
  };

  const getAvgNotional = (row: SummarySlice): number | null => {
    const value = row.avg_notional;
    return Number.isFinite(value ?? NaN) ? (value as number) : null;
  };

  const getPnlPerTradePct = (row: SummarySlice): number | null => {
    const value = row.pnl_per_trade_pct;
    if (Number.isFinite(value ?? NaN)) {
      return value as number;
    }
    const avgNotional = getAvgNotional(row);
    if (!avgNotional) {
      return null;
    }
    const netPnl = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
    const pnlPerTrade = calculatePnLPerTrade(netPnl, row.trades);
    return (pnlPerTrade / avgNotional) * 100;
  };

  const getRowNotionalTotal = (row: SummarySlice): number | null => {
    const trades = row.trades ?? 0;
    if (!trades) {
      return null;
    }
    const avgNotional = getAvgNotional(row);
    if (avgNotional && Number.isFinite(avgNotional)) {
      return avgNotional * trades;
    }
    const pnlPerTradePct = row.pnl_per_trade_pct;
    if (!Number.isFinite(pnlPerTradePct ?? NaN) || !pnlPerTradePct) {
      return null;
    }
    const netPnl = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
    const pnlPerTrade = netPnl / trades;
    const avgNotionalFallback = Math.abs(pnlPerTrade) / (Math.abs(pnlPerTradePct) / 100);
    if (!Number.isFinite(avgNotionalFallback) || avgNotionalFallback <= 0) {
      return null;
    }
    return avgNotionalFallback * trades;
  };

  const dayAggregates = useMemo<DayAggregate[]>(() => {
    if (!chartStrategy) {
      return [];
    }
    const map = new Map<
      string,
      { day: string; ts: number; netPnl: number; trades: number; wins: number; notionalTotal: number; hasNotional: boolean }
    >();
    chartSourceRows.forEach((row) => {
      if (row.strategy !== chartStrategy || !row.day) {
        return;
      }
      const date = toDayDate(row.day);
      if (!date) {
        return;
      }
      const key = row.day;
      let entry = map.get(key);
      if (!entry) {
        entry = { day: row.day, ts: date.getTime(), netPnl: 0, trades: 0, wins: 0, notionalTotal: 0, hasNotional: false };
        map.set(key, entry);
      }
      entry.netPnl += getNetPnl(row as { pnl: number; fees?: number; trades?: number });
      entry.trades += row.trades ?? 0;
      entry.wins += row.wins ?? 0;
      const notional = getRowNotionalTotal(row as SummarySlice);
      if (notional != null) {
        entry.notionalTotal += notional;
        entry.hasNotional = true;
      }
    });
    return Array.from(map.values())
      .map((entry) => ({
        day: entry.day,
        ts: entry.ts,
        netPnl: entry.netPnl,
        trades: entry.trades,
        wins: entry.wins,
        notionalTotal: entry.hasNotional ? entry.notionalTotal : null,
      }))
      .sort((a, b) => a.ts - b.ts);
  }, [chartSourceRows, chartStrategy, getNetPnl, getRowNotionalTotal]);

  const monthAggregates = useMemo<PeriodAggregate[]>(() => {
    const map = new Map<string, PeriodAggregate>();
    dayAggregates.forEach((day) => {
      if (day.trades <= 0) {
        return;
      }
      const bucket = getMonthKey(new Date(day.ts));
      const entry = map.get(bucket.key) ?? {
        key: bucket.key,
        ts: bucket.ts,
        netPnl: 0,
        trades: 0,
        wins: 0,
        days: 0,
      };
      entry.netPnl += day.netPnl;
      entry.trades += day.trades;
      entry.wins += day.wins;
      entry.days += 1;
      map.set(bucket.key, entry);
    });
    return Array.from(map.values()).sort((a, b) => a.ts - b.ts);
  }, [dayAggregates]);

  const weekAggregates = useMemo<PeriodAggregate[]>(() => {
    const map = new Map<string, PeriodAggregate>();
    dayAggregates.forEach((day) => {
      if (day.trades <= 0) {
        return;
      }
      const bucket = getWeekKey(new Date(day.ts));
      const entry = map.get(bucket.key) ?? {
        key: bucket.key,
        ts: bucket.ts,
        netPnl: 0,
        trades: 0,
        wins: 0,
        days: 0,
      };
      entry.netPnl += day.netPnl;
      entry.trades += day.trades;
      entry.wins += day.wins;
      entry.days += 1;
      map.set(bucket.key, entry);
    });
    return Array.from(map.values()).sort((a, b) => a.ts - b.ts);
  }, [dayAggregates]);

  const trendSeries = useMemo<TrendPoint[]>(() => {
    if (!dayAggregates.length) {
      return [];
    }
    const rollingPct = (windowSize: number): Array<number | null> => {
      const result: Array<number | null> = [];
      const queue: Array<{ pnl: number; trades: number; notional: number | null }> = [];
      let sumPnl = 0;
      let sumTrades = 0;
      let sumNotional = 0;
      dayAggregates.forEach((day) => {
        queue.push({ pnl: day.netPnl, trades: day.trades, notional: day.notionalTotal });
        sumPnl += day.netPnl;
        sumTrades += day.trades;
        if (day.notionalTotal != null) {
          sumNotional += day.notionalTotal;
        }
        if (queue.length > windowSize) {
          const removed = queue.shift();
          if (removed) {
            sumPnl -= removed.pnl;
            sumTrades -= removed.trades;
            if (removed.notional != null) {
              sumNotional -= removed.notional;
            }
          }
        }
        if (!sumTrades || sumNotional <= 0) {
          result.push(null);
        } else {
          result.push((sumPnl / sumNotional) * 100);
        }
      });
      return result;
    };

    const rolling7 = rollingPct(7);
    const rolling30 = rollingPct(30);
    let cumulative = 0;
    let peak = 0;
    return dayAggregates.map((day, index) => {
      cumulative += day.netPnl;
      peak = Math.max(peak, cumulative);
      const drawdown = cumulative - peak;
      return {
        period: day.day,
        cumulative,
        drawdown,
        trades: day.trades,
        netPnl: day.netPnl,
        rolling7: rolling7[index] ?? null,
        rolling30: rolling30[index] ?? null,
      };
    });
  }, [dayAggregates]);

  const strategySummary = useMemo(() => {
    if (!dayAggregates.length) {
      return null;
    }
    let netPnl = 0;
    let trades = 0;
    let wins = 0;
    let notionalTotal = 0;
    let notionalKnown = false;
    let positiveDays = 0;
    let activeDays = 0;
    let cumulative = 0;
    let peak = 0;
    let maxDrawdown = 0;
    let winStreak = 0;
    let lossStreak = 0;
    let maxWinStreak = 0;
    let maxLossStreak = 0;

    dayAggregates.forEach((day) => {
      netPnl += day.netPnl;
      trades += day.trades;
      wins += day.wins;
      if (day.notionalTotal != null) {
        notionalTotal += day.notionalTotal;
        notionalKnown = true;
      }
      if (day.trades > 0) {
        activeDays += 1;
        if (day.netPnl > 0) {
          positiveDays += 1;
        }
      }
      cumulative += day.netPnl;
      peak = Math.max(peak, cumulative);
      maxDrawdown = Math.min(maxDrawdown, cumulative - peak);

      if (day.trades > 0) {
        if (day.netPnl > 0) {
          winStreak += 1;
          lossStreak = 0;
        } else if (day.netPnl < 0) {
          lossStreak += 1;
          winStreak = 0;
        } else {
          winStreak = 0;
          lossStreak = 0;
        }
        maxWinStreak = Math.max(maxWinStreak, winStreak);
        maxLossStreak = Math.max(maxLossStreak, lossStreak);
      }
    });

    const avgNotional = notionalKnown && trades > 0 ? notionalTotal / trades : null;
    const pnlPerTradePct = notionalKnown && notionalTotal > 0 ? (netPnl / notionalTotal) * 100 : null;
    const winRate = trades > 0 ? wins / trades : 0;
    const positiveDayRatio = activeDays > 0 ? positiveDays / activeDays : 0;

    const bestMonth = monthAggregates.length
      ? monthAggregates.reduce((best, current) => (current.netPnl > best.netPnl ? current : best))
      : null;
    const worstMonth = monthAggregates.length
      ? monthAggregates.reduce((worst, current) => (current.netPnl < worst.netPnl ? current : worst))
      : null;

    const positiveWeeks = weekAggregates.filter((week) => week.netPnl > 0).length;
    const positiveMonths = monthAggregates.filter((month) => month.netPnl > 0).length;

    return {
      netPnl,
      trades,
      wins,
      winRate,
      avgNotional,
      pnlPerTradePct,
      positiveDayRatio,
      maxDrawdown,
      bestMonth,
      worstMonth,
      positiveWeeks,
      totalWeeks: weekAggregates.length,
      positiveMonths,
      totalMonths: monthAggregates.length,
      maxWinStreak,
      maxLossStreak,
    };
  }, [dayAggregates, monthAggregates, weekAggregates]);

  const monthRanks = useMemo(() => {
    const rows = monthAggregates.map((month) => ({
      period: month.key,
      netPnl: month.netPnl,
      trades: month.trades,
      winRate: month.trades > 0 ? month.wins / month.trades : 0,
    }));
    const sorted = [...rows].sort((a, b) => b.netPnl - a.netPnl);
    return {
      best: sorted.slice(0, 3),
      worst: sorted.slice(-3).reverse(),
    };
  }, [monthAggregates]);

  const weekdayStats = useMemo(() => {
    const labels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
    const stats = labels.map((label) => ({ label, pnl: 0, days: 0, trades: 0 }));
    dayAggregates.forEach((day) => {
      if (day.trades <= 0) {
        return;
      }
      const date = new Date(day.ts);
      const weekdayIndex = (date.getUTCDay() + 6) % 7;
      const entry = stats[weekdayIndex];
      entry.pnl += day.netPnl;
      entry.days += 1;
      entry.trades += day.trades;
    });
    const maxAbs = Math.max(...stats.map((entry) => Math.abs(entry.pnl / (entry.days || 1))), 0);
    return stats.map((entry) => ({
      ...entry,
      avgPnl: entry.days ? entry.pnl / entry.days : 0,
      intensity: maxAbs > 0 ? Math.min(1, Math.abs(entry.pnl / (entry.days || 1)) / maxAbs) : 0,
    }));
  }, [dayAggregates]);

  const hourRows = useMemo(() => {
    const summary = summarySlices as any;
    if (!summary) {
      return [];
    }
    return (summary.by_strategy_hour ?? []) as Array<SummarySlice & { strategy: string; hour: number }>;
  }, [summarySlices]);

  const hourPerformance = useMemo(() => {
    const base: HourPoint[] = Array.from({ length: 24 }, (_, hour) => ({
      hour,
      label: formatHour(hour),
      netPnl: 0,
      trades: 0,
      wins: 0,
      winRate: 0,
      pnlPerTrade: 0,
      pnlPerTradePct: null,
    }));
    if (!chartStrategy) {
      return { data: base, hasTrades: false, best: [], worst: [] };
    }
    const notionalTotals = new Array(24).fill(0);
    const notionalKnown = new Array(24).fill(false);
    hourRows.forEach((row) => {
      const hour = Number(row.hour);
      if (!Number.isFinite(hour) || hour < 0 || hour > 23) {
        return;
      }
      if (row.strategy !== chartStrategy) {
        return;
      }
      const entry = base[hour];
      entry.netPnl += getNetPnl(row as { pnl: number; fees?: number; trades?: number });
      entry.trades += row.trades ?? 0;
      entry.wins += row.wins ?? 0;
      const notional = getRowNotionalTotal(row as SummarySlice);
      if (notional != null) {
        notionalTotals[hour] += notional;
        notionalKnown[hour] = true;
      }
    });
    base.forEach((entry, idx) => {
      if (entry.trades > 0) {
        entry.winRate = entry.wins / entry.trades;
        entry.pnlPerTrade = entry.netPnl / entry.trades;
      }
      if (notionalKnown[idx] && notionalTotals[idx] > 0) {
        entry.pnlPerTradePct = (entry.netPnl / notionalTotals[idx]) * 100;
      }
    });
    const withTrades = base.filter((entry) => entry.trades > 0);
    const sorted = [...withTrades].sort((a, b) => b.netPnl - a.netPnl);
    return {
      data: base,
      hasTrades: withTrades.length > 0,
      best: sorted.slice(0, 3),
      worst: sorted.slice(-3).reverse(),
    };
  }, [chartStrategy, hourRows, getNetPnl, getRowNotionalTotal]);

  const chartSeries = useMemo<ChartSeries[]>(() => {
    if (!chartStrategy) {
      return [];
    }
    const bucketKey = (day: string): { key: string; ts: number } | null => {
      const date = new Date(`${day}T00:00:00Z`);
      if (Number.isNaN(date.getTime())) {
        return null;
      }
      if (chartGranularity === "day") {
        return { key: day, ts: date.getTime() };
      }
      if (chartGranularity === "month") {
        const year = date.getUTCFullYear();
        const month = `${date.getUTCMonth() + 1}`.padStart(2, "0");
        const ts = Date.UTC(year, date.getUTCMonth(), 1);
        return { key: `${year}-${month}`, ts };
      }
      const temp = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
      const dayNum = temp.getUTCDay() || 7;
      temp.setUTCDate(temp.getUTCDate() + 4 - dayNum);
      const yearStart = new Date(Date.UTC(temp.getUTCFullYear(), 0, 1));
      const weekNo = Math.ceil((((temp.getTime() - yearStart.getTime()) / 86400000) + 1) / 7);
      const weekStr = `${weekNo}`.padStart(2, "0");
      const monday = new Date(temp.getTime());
      monday.setUTCDate(temp.getUTCDate() - (temp.getUTCDay() || 7) + 1);
      return { key: `${temp.getUTCFullYear()}-W${weekStr}`, ts: monday.getTime() };
    };

    const byPath = new Map<string, ChartSeries>();
    chartSourceRows.forEach((row) => {
      if (row.strategy !== chartStrategy || !row.symbol || !row.side || !row.day) {
        return;
      }
      const bucket = bucketKey(row.day);
      if (!bucket) {
        return;
      }
      const pathKey = `${row.symbol}||${row.side}`;
      let series = byPath.get(pathKey);
      if (!series) {
        series = { symbol: row.symbol, side: row.side, data: [] };
        byPath.set(pathKey, series);
      }
      const entryMap = (series as any)._entries as Map<string, { period: string; pnl: number; trades: number; ts: number }> | undefined;
      const entries = entryMap ?? new Map();
      if (!entryMap) {
        (series as any)._entries = entries;
      }
      const existing = entries.get(bucket.key) ?? { period: bucket.key, pnl: 0, trades: 0, ts: bucket.ts };
      const netPnl = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
      existing.pnl += netPnl;
      existing.trades += row.trades ?? 0;
      existing.ts = Math.min(existing.ts, bucket.ts);
      entries.set(bucket.key, existing);
    });

    const result: ChartSeries[] = [];
    byPath.forEach((series) => {
      const entries = (series as any)._entries as Map<string, { period: string; pnl: number; trades: number; ts: number }> | undefined;
      const data = entries
        ? Array.from(entries.values())
            .sort((a, b) => a.ts - b.ts)
            .map(({ period, pnl, trades }) => ({ period, pnl, trades }))
        : [];
      result.push({ symbol: series.symbol, side: series.side, data });
    });
    return result.sort((a, b) => `${a.symbol}-${a.side}`.localeCompare(`${b.symbol}-${b.side}`));
  }, [chartSourceRows, chartStrategy, chartGranularity, getNetPnl]);

  const modeRows = useMemo(() => {
    if (!summarySlices || !(summarySlices as any).summary_with_mode_controller) {
      return [];
    }
    const liveRows = (summarySlices.by_symbol_strategy_side ?? []) as SummarySlice[];
    const allRows = ((summarySlices as any).by_symbol_strategy_side_all ?? []) as SummarySlice[];
    if (!liveRows.length && !allRows.length) {
      return [];
    }
    const liveMap = new Map<string, SummarySlice>();
    const allMap = new Map<string, SummarySlice>();
    const calcPnlPerTradePct = (slice: any): number | null => {
      if (!slice) return null;
      const rawPct = slice.pnl_per_trade_pct;
      if (Number.isFinite(rawPct ?? NaN)) {
        return Number(rawPct);
      }
      const trades = Number(slice.trades ?? 0);
      const avgNotional = Number(slice.avg_notional ?? 0);
      const pnl = Number(slice.pnl ?? 0);
      if (trades > 0 && avgNotional > 0) {
        return (pnl / (trades * avgNotional)) * 100;
      }
      return null;
    };
    liveRows.forEach((row) => {
      liveMap.set(`${row.strategy ?? ""}||${row.symbol ?? ""}||${row.side ?? ""}`, row);
    });
    allRows.forEach((row) => {
      allMap.set(`${row.strategy ?? ""}||${row.symbol ?? ""}||${row.side ?? ""}`, row);
    });
    const keys = new Set<string>([...liveMap.keys(), ...allMap.keys()]);
    const rows = Array.from(keys).map((key) => {
      const live = liveMap.get(key) ?? { trades: 0, wins: 0, win_rate: 0, pnl: 0, fees: 0 };
      const all = allMap.get(key) ?? { trades: 0, wins: 0, win_rate: 0, pnl: 0, fees: 0 };
      const [strategy, symbol, side] = key.split("||");
      const liveNet = getNetPnl(live as { pnl: number; fees?: number; trades?: number });
      const allNet = getNetPnl(all as { pnl: number; fees?: number; trades?: number });
      const liveShare = all.trades > 0 ? live.trades / all.trades : 0;
      return {
        strategy,
        symbol,
        side,
        live_trades: live.trades,
        all_trades: all.trades,
        live_win_rate: live.win_rate,
        all_win_rate: all.win_rate,
        live_pnl: liveNet,
        all_pnl: allNet,
        live_pnl_per_trade_pct: calcPnlPerTradePct(live),
        all_pnl_per_trade_pct: calcPnlPerTradePct(all),
        live_fees: getCurrentFees(live as { pnl: number; fees?: number; trades?: number }),
        all_fees: getCurrentFees(all as { pnl: number; fees?: number; trades?: number }),
        live_share: liveShare,
        delta_pnl: liveNet - allNet
      };
    });
    const needle = modeFilter.trim().toLowerCase();
    const filtered = needle
      ? rows.filter((row) => `${row.strategy} ${row.symbol} ${row.side}`.toLowerCase().includes(needle))
      : rows;
    const activeSort = modeTableSort ?? { key: modeSort, direction: "desc" as SortDirection };
    const direction = activeSort.direction === "asc" ? 1 : -1;
    const getValue = (row: any, key: string): string | number | null => {
      switch (key) {
        case "strategy":
          return row.strategy;
        case "symbol":
          return row.symbol;
        case "side":
          return row.side;
        case "live_trades":
          return row.live_trades;
        case "all_trades":
          return row.all_trades;
        case "live_share":
          return row.live_share;
        case "live_win_rate":
          return row.live_win_rate;
        case "all_win_rate":
          return row.all_win_rate;
        case "live_pnl":
          return row.live_pnl;
        case "live_pnl_per_trade_pct":
          return row.live_pnl_per_trade_pct;
        case "all_pnl":
          return row.all_pnl;
        case "all_pnl_per_trade_pct":
          return row.all_pnl_per_trade_pct;
        case "delta_pnl":
          return row.delta_pnl;
        default:
          return null;
      }
    };
    const sorted = [...filtered].sort((a, b) => {
      const aValue = getValue(a, activeSort.key);
      const bValue = getValue(b, activeSort.key);
      const aMissing = aValue == null || (typeof aValue === "number" && Number.isNaN(aValue));
      const bMissing = bValue == null || (typeof bValue === "number" && Number.isNaN(bValue));
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (typeof aValue === "string" || typeof bValue === "string") {
        return String(aValue).localeCompare(String(bValue)) * direction;
      }
      const diff = Number(aValue) - Number(bValue);
      if (diff !== 0) return diff * direction;
      return `${a.strategy}-${a.symbol}-${a.side}`.localeCompare(`${b.strategy}-${b.symbol}-${b.side}`);
    });
    return sorted;
  }, [summarySlices, modeFilter, modeSort, modeTableSort, getNetPnl, getCurrentFees]);

  const handleModeTableSort = useCallback((key: string) => {
    setModeTableSort((prev) => {
      if (prev?.key === key) {
        return { key, direction: prev.direction === "desc" ? "asc" : "desc" };
      }
      return { key, direction: "desc" };
    });
  }, []);

  const renderCompareMetric = (metric?: BacktestCompareBranch["metrics"][number]): JSX.Element => {
    if (!metric) {
      return <span>—</span>;
    }
    const pnlClass = getPnLClass(metric.pnl);
    let pnlPerTradePct: number | null = null;
    if (Number.isFinite(metric.pnl_per_trade_pct ?? NaN)) {
      pnlPerTradePct = metric.pnl_per_trade_pct as number;
    } else if (Number.isFinite(metric.avg_notional ?? NaN) && metric.avg_notional) {
      const pnlPerTrade = metric.trades ? metric.pnl / metric.trades : 0;
      pnlPerTradePct = (pnlPerTrade / metric.avg_notional) * 100;
    }
    return (
      <div className="stack stack--vertical">
        <span className={pnlClass}>{formatNumber(metric.pnl, 2)}</span>
        <small>
          Trades {metric.trades} · Win {formatPercent(metric.win_rate)} · PnL/Trade {pnlPerTradePct == null ? "—" : `${formatNumber(pnlPerTradePct, 2)}%`}
        </small>
      </div>
    );
  };

  return (
    <div className="page page--constrained page--backtest">
      <h2>Backtests</h2>

      <div className="card card--compact" style={{ marginBottom: "1.5rem" }}>
        <div className="form__row" style={{ gap: "0.75rem" }}>
          <button
            type="button"
            className={`button ${activeSection === "runs" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("runs")}
          >
            Backtest Läufe
          </button>
          <button
            type="button"
            className={`button ${activeSection === "compare" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("compare")}
          >
            Vergleich
          </button>
          <button
            type="button"
            className={`button ${activeSection === "mode" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("mode")}
          >
            Mode Controller
          </button>
          <button
            type="button"
            className={`button ${activeSection === "charts" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("charts")}
          >
            Charts
          </button>
          <button
            type="button"
            className={`button ${activeSection === "replays" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("replays")}
          >
            Replay-Checks
          </button>
          <button
            type="button"
            className={`button ${activeSection === "shadow" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("shadow")}
          >
            Shadow-Paper
          </button>
          <button
            type="button"
            className={`button ${activeSection === "templates" ? "button--primary" : "button--secondary"}`}
            onClick={() => setActiveSection("templates")}
          >
            Templates
          </button>
        </div>
      </div>

      {activeSection === "compare" && (
      <div className="card">
        <div className="card__header">
          <h3>Backtest Vergleich (letzte 5 manuelle Runs)</h3>
          {compareLoading && <span className="badge badge--running">Lädt…</span>}
        </div>
        <div className="form__row" style={{ alignItems: "flex-end", gap: "1rem", flexWrap: "wrap" }}>
          <label className="form__label" style={{ minWidth: "220px" }}>
            Filter (Strategie, Alias, Symbol, Richtung)
            <input
              type="text"
              value={compareFilter}
              onChange={(event) => setCompareFilter(event.target.value)}
              placeholder="z.B. swing, btc, long"
            />
          </label>
          <label className="form__label" style={{ minWidth: "200px" }}>
            Sortierung
            <select
              value={compareSort}
              onChange={(event) => setCompareSort(event.target.value as "latest_pnl" | "avg_pnl" | "total_trades")}
            >
              <option value="latest_pnl">Letzter PnL</option>
              <option value="avg_pnl">Ø PnL</option>
              <option value="total_trades">Trades gesamt</option>
            </select>
          </label>
          <label className="form__label" style={{ minWidth: "220px" }}>
            Mode Controller
            <select
              value={compareMode}
              onChange={(event) => setCompareMode(event.target.value as "normal" | "with_mode_controller")}
            >
              <option value="normal">Normal</option>
              <option value="with_mode_controller">Mit Mode Controller</option>
            </select>
          </label>
          <button type="button" className="button button--secondary" onClick={loadCompare}>
            Neu laden
          </button>
        </div>
        <div className="form__row" style={{ gap: "0.75rem", flexWrap: "wrap" }}>
          {compareSelections.map((value, index) => (
            <label key={`compare-select-${index}`} className="form__label" style={{ minWidth: "240px" }}>
              Vergleichsspalte {index + 1}
              <select
                value={value}
                onChange={(event) => handleCompareSelectionChange(index, event.target.value)}
              >
                <option value="">(leer)</option>
                {compareOptions.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        {compareError && <div className="alert alert--error">{compareError}</div>}
        {compareNotice && <div className="alert alert--info">{compareNotice}</div>}
        {compareBacktests.length === 0 ? (
          <p>Keine abgeschlossenen manuellen Backtests verfügbar.</p>
        ) : compareBranches.length === 0 ? (
          <p>Keine Strategiezweige im Vergleich gefunden.</p>
        ) : (
          <div className="table-wrapper">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Strategie</th>
                  <th>Alias</th>
                  <th>Symbol</th>
                  <th>Richtung</th>
                  {compareBacktests.map((bt) => (
                    <th key={bt.id}>
                      <div style={{ display: "grid", gap: "0.25rem" }}>
                        <strong>{bt.start_date} → {bt.end_date}</strong>
                        <small>{formatTimestamp(bt.completed_at)}</small>
                        <span className="table__hint">{bt.template_name ?? bt.template_id ?? "Aktuelle Settings"}</span>
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {compareTotals.length > 0 && (
                  <tr>
                    <td><strong>Gesamt</strong></td>
                    <td>—</td>
                    <td>—</td>
                    <td>—</td>
                    {compareBacktests.map((bt, index) => {
                      const total = compareTotals[index];
                      if (!total) {
                        return <td key={`${bt.id}-${index}`}>—</td>;
                      }
                      const pnlClass = getPnLClass(total.pnl);
                      return (
                        <td key={`${bt.id}-${index}`}>
                          <div className="stack stack--vertical">
                            <span className={pnlClass}>{formatNumber(total.pnl, 2)}</span>
                            <small>PnL/Trade {total.pnl_per_trade_pct == null ? "—" : `${formatNumber(total.pnl_per_trade_pct, 2)}%`}</small>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                )}
                {compareBranches.map((branch) => (
                  <tr key={`${branch.strategy}-${branch.alias}-${branch.symbol}-${branch.direction}`}>
                    <td>{branch.strategy}</td>
                    <td>{branch.alias}</td>
                    <td>{branch.symbol}</td>
                    <td>{branch.direction}</td>
                    {compareBacktests.map((bt, index) => (
                      <td key={`${bt.id}-${index}`}>{renderCompareMetric(branch.metrics[index])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      )}

      {activeSection === "mode" && (
      <div className="card">
        <div className="card__header">
          <h3>Mode-Controller Auswertung</h3>
          {detailLoading && <span className="badge badge--running">Lädt…</span>}
        </div>
        <div className="form__row" style={{ alignItems: "flex-end", gap: "1rem", flexWrap: "wrap" }}>
          <label className="form__label" style={{ minWidth: "320px" }}>
            Backtest auswählen
            <select
              value={selectedBacktestId ?? ""}
              onChange={(event) => setSelectedBacktestId(event.target.value || null)}
            >
              <option value="">(keiner)</option>
              {backtests.map((bt) => (
                <option key={bt.id} value={bt.id}>
                  {bt.id} · {bt.start_date} → {bt.end_date} · {formatTimestamp(bt.completed_at)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {detailError && <div className="alert alert--error">{detailError}</div>}
        {!detail && !detailError && <p>Bitte einen Backtest auswählen.</p>}
        {detail && detail.status !== "completed" && (
          <p>Der ausgewählte Backtest ist noch nicht abgeschlossen.</p>
        )}
        {detail && detail.status === "completed" && (
          <>
            {summarySlices && (summarySlices as any).summary_with_mode_controller ? (
              <div className="stack stack--vertical">
                <div className="card card--compact">
                  <h4>Live vs. Alle Trades</h4>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "1rem" }}>
                    <div>
                      <h5>Live (Mode Controller)</h5>
                      <ul className="metrics-list">
                        <li>Trades <strong>{(summarySlices as any).summary_with_mode_controller.trades}</strong></li>
                        <li>Win-Rate <strong>{formatPercent((summarySlices as any).summary_with_mode_controller.win_rate)}</strong></li>
                        <li>
                          PnL (Netto){" "}
                          <strong>
                            {formatNumber(
                              getNetPnl((summarySlices as any).summary_with_mode_controller),
                              2
                            )}
                          </strong>
                        </li>
                        <li>
                          Fees (aktuell){" "}
                          <strong>
                            {formatNumber(
                              getCurrentFees((summarySlices as any).summary_with_mode_controller),
                              2
                            )}
                          </strong>
                        </li>
                      </ul>
                    </div>
                    <div>
                      <h5>Alle Trades</h5>
                      <ul className="metrics-list">
                        <li>Trades <strong>{(summarySlices as any).summary_normal?.trades ?? summarySlices.summary.trades}</strong></li>
                        <li>Win-Rate <strong>{formatPercent((summarySlices as any).summary_normal?.win_rate ?? summarySlices.summary.win_rate)}</strong></li>
                        <li>
                          PnL (Netto){" "}
                          <strong>
                            {formatNumber(
                              getNetPnl(((summarySlices as any).summary_normal ?? summarySlices.summary) as any),
                              2
                            )}
                          </strong>
                        </li>
                        <li>
                          Fees (aktuell){" "}
                          <strong>
                            {formatNumber(
                              getCurrentFees(((summarySlices as any).summary_normal ?? summarySlices.summary) as any),
                              2
                            )}
                          </strong>
                        </li>
                      </ul>
                    </div>
                    <div>
                      <h5>Live-Anteil</h5>
                      <ul className="metrics-list">
                        <li>
                          Trades{" "}
                          <strong>
                            {formatPercent(
                              ((summarySlices as any).summary_normal?.trades ?? summarySlices.summary.trades) > 0
                                ? (summarySlices as any).summary_with_mode_controller.trades /
                                  ((summarySlices as any).summary_normal?.trades ?? summarySlices.summary.trades)
                                : 0
                            )}
                          </strong>
                        </li>
                        <li>
                          PnL-Delta{" "}
                          <strong className={getPnLClass(
                            getNetPnl((summarySlices as any).summary_with_mode_controller) -
                            getNetPnl(((summarySlices as any).summary_normal ?? summarySlices.summary) as any)
                          )}>
                            {formatNumber(
                              getNetPnl((summarySlices as any).summary_with_mode_controller) -
                              getNetPnl(((summarySlices as any).summary_normal ?? summarySlices.summary) as any),
                              2
                            )}
                          </strong>
                        </li>
                      </ul>
                    </div>
                  </div>
                </div>

                <div className="form__row" style={{ alignItems: "flex-end", gap: "1rem", flexWrap: "wrap" }}>
                  <label className="form__label" style={{ minWidth: "220px" }}>
                    Filter (Strategie, Symbol, Richtung)
                    <input
                      type="text"
                      value={modeFilter}
                      onChange={(event) => setModeFilter(event.target.value)}
                      placeholder="z.B. vwap, btc, short"
                    />
                  </label>
                  <label className="form__label" style={{ minWidth: "220px" }}>
                    Sortierung
                    <select
                      value={modeSort}
                      onChange={(event) => {
                        const value = event.target.value as typeof modeSort;
                        setModeSort(value);
                        setModeTableSort({ key: value, direction: "desc" });
                      }}
                    >
                      <option value="live_pnl">Live-PnL</option>
                      <option value="delta_pnl">PnL-Delta</option>
                      <option value="live_trades">Live-Trades</option>
                      <option value="live_share">Live-Anteil</option>
                    </select>
                  </label>
                </div>

                {modeRows.length === 0 ? (
                  <p>Keine Mode-Controller Daten gefunden.</p>
                ) : (
                  <div className="table-wrapper">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          {[
                            { key: "strategy", label: "Strategie" },
                            { key: "symbol", label: "Symbol" },
                            { key: "side", label: "Richtung" },
                            { key: "live_trades", label: "Live Trades" },
                            { key: "all_trades", label: "Alle Trades" },
                            { key: "live_share", label: "Live-Anteil" },
                            { key: "live_win_rate", label: "Live Win-Rate" },
                            { key: "all_win_rate", label: "Alle Win-Rate" },
                            { key: "live_pnl", label: "Live PnL" },
                            { key: "live_pnl_per_trade_pct", label: "Live PnL/Trade %" },
                            { key: "all_pnl", label: "Alle PnL" },
                            { key: "all_pnl_per_trade_pct", label: "Alle PnL/Trade %" },
                            { key: "delta_pnl", label: "Delta" },
                          ].map((column) => {
                            const isActive = modeTableSort?.key === column.key;
                            const indicator = isActive ? (modeTableSort?.direction === "asc" ? "▲" : "▼") : "";
                            return (
                              <th key={`mode-col-${column.key}`}>
                                <button
                                  type="button"
                                  className={`table__sort-button${isActive ? " table__sort-button--active" : ""}`}
                                  onClick={() => handleModeTableSort(column.key)}
                                  aria-label={`Sortiere nach ${column.label}`}
                                >
                                  <span>{column.label}</span>
                                  <span className="table__sort-indicator">{indicator}</span>
                                </button>
                              </th>
                            );
                          })}
                        </tr>
                      </thead>
                      <tbody>
                        {modeRows.map((row) => (
                          <tr key={`${row.strategy}-${row.symbol}-${row.side}`}>
                            <td>{row.strategy}</td>
                            <td>{row.symbol}</td>
                            <td>{row.side}</td>
                            <td>{row.live_trades}</td>
                            <td>{row.all_trades}</td>
                            <td>{formatPercent(row.live_share)}</td>
                            <td>{formatPercent(row.live_win_rate)}</td>
                            <td>{formatPercent(row.all_win_rate)}</td>
                            <td className={getPnLClass(row.live_pnl)}>{formatNumber(row.live_pnl, 2)}</td>
                            <td className={getPnLClass(row.live_pnl_per_trade_pct ?? 0)}>
                              {row.live_pnl_per_trade_pct == null ? "—" : `${formatNumber(row.live_pnl_per_trade_pct, 2)}%`}
                            </td>
                            <td className={getPnLClass(row.all_pnl)}>{formatNumber(row.all_pnl, 2)}</td>
                            <td className={getPnLClass(row.all_pnl_per_trade_pct ?? 0)}>
                              {row.all_pnl_per_trade_pct == null ? "—" : `${formatNumber(row.all_pnl_per_trade_pct, 2)}%`}
                            </td>
                            <td className={getPnLClass(row.delta_pnl)}>{formatNumber(row.delta_pnl, 2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ) : (
              <p>Dieser Backtest enthält keine Mode-Controller-Auswertung.</p>
            )}
          </>
        )}
      </div>
      )}

      {activeSection === "charts" && (
      <div className="card">
        <div className="card__header">
          <h3>Backtest Charts</h3>
          {detailLoading && <span className="badge badge--running">Lädt…</span>}
        </div>
        {!summarySlices && <p>Bitte einen Backtest auswählen.</p>}
        {summarySlices && chartSourceRows.length === 0 && (
          <p>Keine Tagesdaten vorhanden. Bitte einen neuen Backtest starten, um Tages-Buckets zu erzeugen.</p>
        )}
        {summarySlices && chartSourceRows.length > 0 && (
          <>
            <div className="form__row" style={{ gap: "0.75rem", flexWrap: "wrap" }}>
              <label className="form__label" style={{ minWidth: "220px" }}>
                Strategie
                <select
                  value={chartStrategy}
                  onChange={(event) => setChartStrategy(event.target.value)}
                >
                  {chartStrategies.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="form__label" style={{ minWidth: "200px" }}>
                Zeitraum
                <select
                  value={chartGranularity}
                  onChange={(event) => setChartGranularity(event.target.value as "day" | "week" | "month")}
                >
                  <option value="day">Tage</option>
                  <option value="week">Wochen</option>
                  <option value="month">Monate</option>
                </select>
              </label>
            </div>
            {dayAggregates.length === 0 ? (
              <p>Keine Trades für die gewählte Strategie.</p>
            ) : (
              <>
                {strategySummary && (
                  <div className="grid grid--cols-3 grid--gap-lg" style={{ marginTop: "1rem" }}>
                    <div className="card card--compact">
                      <h4>Zusammenfassung</h4>
                      <ul className="metrics-list">
                        <li>
                          Netto PnL{" "}
                          <strong className={getPnLClass(strategySummary.netPnl)}>{formatNumber(strategySummary.netPnl, 2)}</strong>
                        </li>
                        <li>Trades <strong>{strategySummary.trades}</strong></li>
                        <li>Win-Rate <strong>{formatPercent(strategySummary.winRate)}</strong></li>
                        <li>
                          PnL/Trade %{" "}
                          <strong>
                            {strategySummary.pnlPerTradePct == null ? "—" : `${formatNumber(strategySummary.pnlPerTradePct, 2)}%`}
                          </strong>
                        </li>
                        <li>
                          Avg Notional{" "}
                          <strong>
                            {strategySummary.avgNotional == null ? "—" : formatNumber(strategySummary.avgNotional, 2)}
                          </strong>
                        </li>
                        <li>
                          Max Drawdown{" "}
                          <strong className={getPnLClass(strategySummary.maxDrawdown)}>{formatNumber(strategySummary.maxDrawdown, 2)}</strong>
                        </li>
                      </ul>
                    </div>
                    <div className="card card--compact">
                      <h4>Konsistenz</h4>
                      <ul className="metrics-list">
                        <li>
                          Positive Tage{" "}
                          <strong>{formatPercent(strategySummary.positiveDayRatio)}</strong>
                        </li>
                        <li>
                          Positive Wochen{" "}
                          <strong>
                            {strategySummary.totalWeeks > 0
                              ? formatPercent(strategySummary.positiveWeeks / strategySummary.totalWeeks)
                              : "—"}
                          </strong>
                        </li>
                        <li>
                          Positive Monate{" "}
                          <strong>
                            {strategySummary.totalMonths > 0
                              ? formatPercent(strategySummary.positiveMonths / strategySummary.totalMonths)
                              : "—"}
                          </strong>
                        </li>
                        <li>Max Win-Streak <strong>{strategySummary.maxWinStreak}</strong></li>
                        <li>Max Loss-Streak <strong>{strategySummary.maxLossStreak}</strong></li>
                      </ul>
                    </div>
                    <div className="card card--compact">
                      <h4>Best/Worst</h4>
                      <ul className="metrics-list">
                        <li>
                          Bestes Monat{" "}
                          <strong className={getPnLClass(strategySummary.bestMonth?.netPnl ?? 0)}>
                            {strategySummary.bestMonth
                              ? `${strategySummary.bestMonth.key} (${formatNumber(strategySummary.bestMonth.netPnl, 2)})`
                              : "—"}
                          </strong>
                        </li>
                        <li>
                          Schlechtestes Monat{" "}
                          <strong className={getPnLClass(strategySummary.worstMonth?.netPnl ?? 0)}>
                            {strategySummary.worstMonth
                              ? `${strategySummary.worstMonth.key} (${formatNumber(strategySummary.worstMonth.netPnl, 2)})`
                              : "—"}
                          </strong>
                        </li>
                      </ul>
                    </div>
                  </div>
                )}

                {trendSeries.length > 0 && (
                  <div className="grid grid--cols-2 grid--gap-lg" style={{ marginTop: "1.5rem" }}>
                    <div className="card card--compact">
                      <h4>Kumulativ & Drawdown (tagesbasiert)</h4>
                      <div style={{ width: "100%", height: "260px" }}>
                        <ResponsiveContainer>
                          <ComposedChart data={trendSeries} margin={{ top: 10, right: 20, left: 0, bottom: 20 }}>
                            <CartesianGrid strokeDasharray="3 3" />
                            <XAxis dataKey="period" angle={-30} textAnchor="end" height={60} />
                            <YAxis />
                            <Tooltip
                              formatter={(value: number, name: string) => {
                                if (name === "cumulative") return [formatNumber(value, 2), "Kumul. PnL"];
                                if (name === "drawdown") return [formatNumber(value, 2), "Drawdown"];
                                if (name === "netPnl") return [formatNumber(value, 2), "PnL (Tag)"];
                                return [value, name];
                              }}
                              labelFormatter={(label) => `Tag: ${label}`}
                            />
                            <Bar dataKey="netPnl" fill="#94a3b8" opacity={0.35} />
                            <Area dataKey="drawdown" stroke="#ef4444" fill="#fecaca" opacity={0.6} />
                            <Line type="monotone" dataKey="cumulative" stroke="#2563eb" strokeWidth={2} dot={false} />
                          </ComposedChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                    <div className="card card--compact">
                      <h4>Rolling PnL/Trade % & Trades</h4>
                      <div style={{ width: "100%", height: "260px" }}>
                        <ResponsiveContainer>
                          <ComposedChart data={trendSeries} margin={{ top: 10, right: 20, left: 0, bottom: 20 }}>
                            <CartesianGrid strokeDasharray="3 3" />
                            <XAxis dataKey="period" angle={-30} textAnchor="end" height={60} />
                            <YAxis
                              yAxisId="pct"
                              tickFormatter={(value) => {
                                const num = typeof value === "number" ? value : Number(value);
                                return Number.isFinite(num) ? `${num.toFixed(2)}%` : "—";
                              }}
                            />
                            <YAxis yAxisId="trades" orientation="right" />
                            <Tooltip
                              formatter={(value: number, name: string) => {
                                if (name === "rolling7") return [value == null ? "—" : `${formatNumber(value, 2)}%`, "Rolling 7"];
                                if (name === "rolling30") return [value == null ? "—" : `${formatNumber(value, 2)}%`, "Rolling 30"];
                                if (name === "trades") return [value, "Trades"];
                                return [value, name];
                              }}
                              labelFormatter={(label) => `Tag: ${label}`}
                            />
                            <Line yAxisId="pct" type="monotone" dataKey="rolling7" stroke="#22c55e" strokeWidth={2} dot={false} />
                            <Line yAxisId="pct" type="monotone" dataKey="rolling30" stroke="#f59e0b" strokeWidth={2} dot={false} />
                            <Bar yAxisId="trades" dataKey="trades" fill="#94a3b8" opacity={0.35} />
                          </ComposedChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                  </div>
                )}

                {monthRanks.best.length > 0 && (
                  <div className="grid grid--cols-2 grid--gap-lg" style={{ marginTop: "1.5rem" }}>
                    <div className="card card--compact">
                      <h4>Top Monate</h4>
                      <div className="table-wrapper">
                        <table className="table table--compact">
                          <thead>
                            <tr>
                              <th>Monat</th>
                              <th>PnL</th>
                              <th>Trades</th>
                              <th>Win-Rate</th>
                            </tr>
                          </thead>
                          <tbody>
                            {monthRanks.best.map((row) => (
                              <tr key={`best-${row.period}`}>
                                <td>{row.period}</td>
                                <td className={getPnLClass(row.netPnl)}>{formatNumber(row.netPnl, 2)}</td>
                                <td>{row.trades}</td>
                                <td>{formatPercent(row.winRate)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                    <div className="card card--compact">
                      <h4>Flop Monate</h4>
                      <div className="table-wrapper">
                        <table className="table table--compact">
                          <thead>
                            <tr>
                              <th>Monat</th>
                              <th>PnL</th>
                              <th>Trades</th>
                              <th>Win-Rate</th>
                            </tr>
                          </thead>
                          <tbody>
                            {monthRanks.worst.map((row) => (
                              <tr key={`worst-${row.period}`}>
                                <td>{row.period}</td>
                                <td className={getPnLClass(row.netPnl)}>{formatNumber(row.netPnl, 2)}</td>
                                <td>{row.trades}</td>
                                <td>{formatPercent(row.winRate)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                )}

                {weekdayStats.length > 0 && (
                  <div className="card card--compact" style={{ marginTop: "1.5rem" }}>
                    <h4>Wochentag-Heatmap (Avg PnL)</h4>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0, 1fr))", gap: "0.5rem" }}>
                      {weekdayStats.map((entry) => {
                        const base = entry.avgPnl >= 0 ? "34, 197, 94" : "239, 68, 68";
                        const intensity = entry.intensity;
                        const background = intensity > 0
                          ? `rgba(${base}, ${0.15 + 0.55 * intensity})`
                          : "rgba(148, 163, 184, 0.15)";
                        return (
                          <div
                            key={entry.label}
                            style={{
                              padding: "0.75rem",
                              borderRadius: "8px",
                              background,
                              textAlign: "center",
                            }}
                          >
                            <div style={{ fontSize: "0.85rem", opacity: 0.8 }}>{entry.label}</div>
                            <div className={getPnLClass(entry.avgPnl)} style={{ fontWeight: 600 }}>
                              {formatNumber(entry.avgPnl, 2)}
                            </div>
                            <div style={{ fontSize: "0.75rem", opacity: 0.7 }}>
                              {entry.days} Tage · {entry.trades} Trades
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {hourRows.length === 0 ? (
                  <div className="card card--compact" style={{ marginTop: "1.5rem" }}>
                    <h4>Stunden-Performance</h4>
                    <p>Keine Stunden-Daten vorhanden. Bitte einen neuen Backtest starten, um Stunden-Buckets zu erzeugen.</p>
                  </div>
                ) : hourPerformance.hasTrades ? (
                  <div className="grid grid--cols-2 grid--gap-lg" style={{ marginTop: "1.5rem" }}>
                    <div className="card card--compact">
                      <h4>Stunden-Performance ({detail?.hour_timezone ?? "UTC"})</h4>
                      <div style={{ width: "100%", height: "260px" }}>
                        <ResponsiveContainer>
                          <ComposedChart data={hourPerformance.data} margin={{ top: 10, right: 20, left: 0, bottom: 20 }}>
                            <CartesianGrid strokeDasharray="3 3" />
                            <XAxis dataKey="label" angle={-20} textAnchor="end" height={50} />
                            <YAxis />
                            <YAxis
                              yAxisId="win"
                              orientation="right"
                              tickFormatter={(value) => {
                                const num = typeof value === "number" ? value : Number(value);
                                return Number.isFinite(num) ? `${(num * 100).toFixed(0)}%` : "—";
                              }}
                            />
                            <Tooltip
                              formatter={(value: number, name: string) => {
                                if (name === "netPnl") return [formatNumber(value, 2), "PnL (Netto)"];
                                if (name === "winRate") return [`${formatNumber(value * 100, 2)}%`, "Win-Rate"];
                                if (name === "trades") return [value, "Trades"];
                                if (name === "pnlPerTradePct") {
                                  return [value == null ? "—" : `${formatNumber(value, 2)}%`, "PnL/Trade %"];
                                }
                                return [value, name];
                              }}
                              labelFormatter={(label) => `Stunde: ${label}`}
                            />
                            <Bar dataKey="netPnl" fill="#0ea5e9" opacity={0.7} />
                            <Line yAxisId="win" type="monotone" dataKey="winRate" stroke="#22c55e" strokeWidth={2} dot={false} />
                          </ComposedChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                    <div className="card card--compact">
                      <h4>Beste / Schwächste Stunden</h4>
                      <div className="table-wrapper">
                        <table className="table table--compact">
                          <thead>
                            <tr>
                              <th>Stunde</th>
                              <th>PnL</th>
                              <th>Trades</th>
                              <th>Win-Rate</th>
                            </tr>
                          </thead>
                          <tbody>
                            {hourPerformance.best.map((row) => (
                              <tr key={`hour-best-${row.hour}`}>
                                <td>{row.label}</td>
                                <td className={getPnLClass(row.netPnl)}>{formatNumber(row.netPnl, 2)}</td>
                                <td>{row.trades}</td>
                                <td>{formatPercent(row.winRate)}</td>
                              </tr>
                            ))}
                            {hourPerformance.worst.map((row) => (
                              <tr key={`hour-worst-${row.hour}`}>
                                <td>{row.label}</td>
                                <td className={getPnLClass(row.netPnl)}>{formatNumber(row.netPnl, 2)}</td>
                                <td>{row.trades}</td>
                                <td>{formatPercent(row.winRate)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="card card--compact" style={{ marginTop: "1.5rem" }}>
                    <h4>Stunden-Performance</h4>
                    <p>Keine Trades für diese Strategie in der Stunden-Ansicht.</p>
                  </div>
                )}

                {chartSeries.length > 0 && (
                  <div className="grid grid--cols-2 grid--gap-lg" style={{ marginTop: "1.5rem" }}>
                    {chartSeries.map((series) => (
                      <div key={`${series.symbol}-${series.side}`} className="card card--compact">
                        <h4>{series.symbol} · {series.side}</h4>
                        {series.data.length === 0 ? (
                          <p>Keine Daten im Zeitraum.</p>
                        ) : (
                          <div style={{ width: "100%", height: "240px" }}>
                            <ResponsiveContainer>
                              <LineChart data={series.data} margin={{ top: 10, right: 20, left: 0, bottom: 20 }}>
                                <CartesianGrid strokeDasharray="3 3" />
                                <XAxis dataKey="period" angle={-30} textAnchor="end" height={60} />
                                <YAxis />
                                <Tooltip
                                  formatter={(value: number, name: string) => {
                                    if (name === "pnl") {
                                      return [formatNumber(value, 2), "PnL (Netto)"];
                                    }
                                    return [value, name];
                                  }}
                                  labelFormatter={(label) => `Zeitraum: ${label}`}
                                />
                                <Line
                                  type="monotone"
                                  dataKey="pnl"
                                  stroke="#7429ff"
                                  strokeWidth={2}
                                  dot={false}
                                />
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
      )}

      {activeSection === "replays" && (
      <div className="card">
        <div className="card__header">
          <h3>Replay-Checks (Mini-Backtests)</h3>
          {replayLoading && <span className="badge badge--running">Lädt…</span>}
        </div>
        <p style={{ marginTop: 0 }}>
          Auswertung der Replay-Jobs für den aktuellen Run. Jeder abgeschlossene Live-Trade kann automatisch
          mit einem Mini-Backtest über denselben Zeitraum gegengeprüft werden.
        </p>
        <div className="form__row" style={{ alignItems: "flex-end", gap: "1rem", flexWrap: "wrap" }}>
          <label className="form__label" style={{ minWidth: "220px" }}>
            Filter (Strategie, Symbol, Status)
            <input
              type="text"
              value={replayFilter}
              onChange={(event) => setReplayFilter(event.target.value)}
              placeholder="z.B. donchian, btc, completed"
            />
          </label>
          <label className="form__label" style={{ minWidth: "180px" }}>
            Status
            <select value={replayStatus} onChange={(event) => setReplayStatus(event.target.value)}>
              <option value="all">Alle</option>
              <option value="completed">completed</option>
              <option value="failed">failed</option>
              <option value="deferred">deferred</option>
              <option value="queued">queued</option>
              <option value="running">running</option>
            </select>
          </label>
          <label className="form__label" style={{ minWidth: "180px" }}>
            Modus
            <select value={replayMode} onChange={(event) => setReplayMode(event.target.value)}>
              <option value="all">Alle</option>
              <option value="forced">forced</option>
              <option value="signal">signal</option>
            </select>
          </label>
          <button type="button" className="button button--secondary" onClick={loadTradeReplays}>
            Neu laden
          </button>
        </div>

        <div className="grid grid--cols-3 grid--gap-lg" style={{ marginTop: "1rem" }}>
          <div className="card card--compact">
            <h4>Queue</h4>
            <ul className="metrics-list">
              <li>Gesamt <strong>{replaySummary.total}</strong></li>
              <li>Queued <strong>{replaySummary.queued}</strong></li>
              <li>Running <strong>{replaySummary.running}</strong></li>
            </ul>
          </div>
          <div className="card card--compact">
            <h4>Ergebnisse</h4>
            <ul className="metrics-list">
              <li>Completed <strong>{replaySummary.completed}</strong></li>
              <li>Failed <strong>{replaySummary.failed}</strong></li>
              <li>Deferred <strong>{replaySummary.deferred}</strong></li>
            </ul>
          </div>
          <div className="card card--compact">
            <h4>Signal-Match</h4>
            <ul className="metrics-list">
              <li>Matched <strong>{replaySummary.matched}</strong></li>
              <li>Unmatched <strong>{replaySummary.unmatched}</strong></li>
              <li>Abdeckung <strong>
                {replaySummary.completed > 0 ? `${Math.round((replaySummary.matched / replaySummary.completed) * 100)}%` : "—"}
              </strong></li>
            </ul>
          </div>
        </div>

        {replayError && <div className="alert alert--error" style={{ marginTop: "1rem" }}>{replayError}</div>}
        {replayFiltered.length === 0 ? (
          <p style={{ marginTop: "1rem" }}>Keine Replay-Jobs gefunden.</p>
        ) : (
          <div className="table-wrapper" style={{ marginTop: "1rem" }}>
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Trade ID</th>
                  <th>Strategie</th>
                  <th>Symbol</th>
                  <th>Richtung</th>
                  <th>Modus</th>
                  <th>Status</th>
                  <th>Replay PnL</th>
                  <th>Entry Δ</th>
                  <th>Exit Δ</th>
                  <th>Match</th>
                  <th>Hinweis</th>
                </tr>
              </thead>
              <tbody>
                {replayFiltered.map((item) => {
                  const result = parseReplayResult(item);
                  const replayPnl = result?.pnl_net_est ?? result?.pnl ?? null;
                  const entryDelta = result?.entry_delta_sec ?? null;
                  const exitDelta = result?.exit_delta_sec ?? null;
                  const matched = typeof result?.matched === "boolean" ? (result?.matched ? "match" : "miss") : "—";
                  const statusLabel = (item.status ?? "unknown").toLowerCase();
                  return (
                    <tr key={`replay-${item.id}`}>
                      <td>{formatTimestamp(item.created_at)}</td>
                      <td>{item.trade_id ?? "—"}</td>
                      <td>{item.strategy ?? "—"}</td>
                      <td>{item.symbol ?? "—"}</td>
                      <td>{item.direction ?? "—"}</td>
                      <td>{item.mode ?? "—"}</td>
                      <td><span className={`badge badge--${statusLabel}`}>{item.status ?? "—"}</span></td>
                      <td className={getPnLClass(replayPnl ?? 0)}>{formatNumber(replayPnl, 2)}</td>
                      <td>{formatDeltaSeconds(entryDelta)}</td>
                      <td>{formatDeltaSeconds(exitDelta)}</td>
                      <td>{matched}</td>
                      <td title={item.error ?? ""}>{item.error ?? result?.error ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {replayTotal > replayItems.length && (
          <p style={{ marginTop: "0.75rem", opacity: 0.7 }}>
            Hinweis: Es werden nur die letzten {replayItems.length} von {replayTotal} Replay-Jobs angezeigt.
          </p>
        )}
      </div>
      )}

      {activeSection === "shadow" && (
      <div className="card">
        <div className="card__header">
          <h3>Shadow-Paper (Live → Kraken Demo)</h3>
          {shadowLoading && <span className="badge badge--running">Lädt…</span>}
        </div>
        <p style={{ marginTop: 0 }}>
          Live-Trades werden optional parallel als Demo-Trade gespiegelt und nach Live-Exit abgeglichen.
        </p>
        <div className="form__row" style={{ alignItems: "flex-end", gap: "1rem", flexWrap: "wrap" }}>
          <label className="form__label" style={{ minWidth: "180px" }}>
            Aktiv
            <select
              value={shadowConfig?.enabled ? "on" : "off"}
              onChange={async (event) => {
                if (!shadowConfig) return;
                setShadowConfigSaving(true);
                try {
                  const next = await updateShadowMirrorConfig({
                    enabled: event.target.value === "on",
                    close_with_live: shadowConfig.close_with_live,
                    max_open: shadowConfig.max_open,
                  });
                  setShadowConfig(next);
                } catch (err: any) {
                  setShadowError(err?.response?.data?.detail ?? "Shadow-Mirror Konfiguration konnte nicht gespeichert werden.");
                } finally {
                  setShadowConfigSaving(false);
                }
              }}
              disabled={!shadowConfig || shadowConfigSaving}
            >
              <option value="on">On</option>
              <option value="off">Off</option>
            </select>
          </label>
          <label className="form__label" style={{ minWidth: "200px" }}>
            Bei Live-Exit schließen
            <select
              value={shadowConfig?.close_with_live ? "yes" : "no"}
              onChange={async (event) => {
                if (!shadowConfig) return;
                setShadowConfigSaving(true);
                try {
                  const next = await updateShadowMirrorConfig({
                    enabled: shadowConfig.enabled,
                    close_with_live: event.target.value === "yes",
                    max_open: shadowConfig.max_open,
                  });
                  setShadowConfig(next);
                } catch (err: any) {
                  setShadowError(err?.response?.data?.detail ?? "Shadow-Mirror Konfiguration konnte nicht gespeichert werden.");
                } finally {
                  setShadowConfigSaving(false);
                }
              }}
              disabled={!shadowConfig || shadowConfigSaving}
            >
              <option value="yes">Ja</option>
              <option value="no">Nein</option>
            </select>
          </label>
          <label className="form__label" style={{ minWidth: "140px" }}>
            Max Open
            <input
              type="number"
              min={1}
              value={shadowConfig?.max_open ?? 50}
              onChange={async (event) => {
                if (!shadowConfig) return;
                const parsed = Math.max(1, Number(event.target.value || 1));
                setShadowConfigSaving(true);
                try {
                  const next = await updateShadowMirrorConfig({
                    enabled: shadowConfig.enabled,
                    close_with_live: shadowConfig.close_with_live,
                    max_open: parsed,
                  });
                  setShadowConfig(next);
                } catch (err: any) {
                  setShadowError(err?.response?.data?.detail ?? "Shadow-Mirror Konfiguration konnte nicht gespeichert werden.");
                } finally {
                  setShadowConfigSaving(false);
                }
              }}
              disabled={!shadowConfig || shadowConfigSaving}
            />
          </label>
          <label className="form__label" style={{ minWidth: "180px" }}>
            Status
            <select value={shadowStatus} onChange={(event) => setShadowStatus(event.target.value)}>
              <option value="all">Alle</option>
              <option value="open">open</option>
              <option value="closed">closed</option>
              <option value="failed">failed</option>
            </select>
          </label>
          <label className="form__label" style={{ minWidth: "220px" }}>
            Filter
            <input
              type="text"
              value={shadowFilter}
              onChange={(event) => setShadowFilter(event.target.value)}
              placeholder="z.B. donchian, BTCUSDT"
            />
          </label>
          <button type="button" className="button button--secondary" onClick={loadShadowMirrors}>
            Neu laden
          </button>
        </div>

        <div className="grid grid--cols-3 grid--gap-lg" style={{ marginTop: "1rem" }}>
          <div className="card card--compact">
            <h4>Total</h4>
            <ul className="metrics-list">
              <li>Einträge <strong>{shadowSummary.total}</strong></li>
              <li>Offen <strong>{shadowSummary.open}</strong></li>
            </ul>
          </div>
          <div className="card card--compact">
            <h4>Ergebnisse</h4>
            <ul className="metrics-list">
              <li>Closed <strong>{shadowSummary.closed}</strong></li>
              <li>Failed <strong>{shadowSummary.failed}</strong></li>
            </ul>
          </div>
          <div className="card card--compact">
            <h4>PnL Delta</h4>
            <ul className="metrics-list">
              <li>
                Avg Δ <strong>
                  {shadowFiltered.length
                    ? formatNumber(
                        shadowFiltered.reduce((sum, item) => sum + ((item.shadow_pnl ?? 0) - (item.live_pnl ?? 0)), 0) / shadowFiltered.length,
                        2
                      )
                    : "—"}
                </strong>
              </li>
            </ul>
          </div>
        </div>

        {shadowError && <div className="alert alert--error" style={{ marginTop: "1rem" }}>{shadowError}</div>}
        {shadowFiltered.length === 0 ? (
          <p style={{ marginTop: "1rem" }}>Keine Shadow-Mirror Einträge gefunden.</p>
        ) : (
          <div className="table-wrapper" style={{ marginTop: "1rem" }}>
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Status</th>
                  <th>Strategie</th>
                  <th>Symbol</th>
                  <th>Richtung</th>
                  <th>Live ID</th>
                  <th>Shadow ID</th>
                  <th>Live PnL</th>
                  <th>Shadow PnL</th>
                  <th>Delta</th>
                  <th>Live Exit</th>
                  <th>Shadow Exit</th>
                </tr>
              </thead>
              <tbody>
                {shadowFiltered.map((item) => {
                  const livePnl = item.live_pnl ?? null;
                  const shadowPnl = item.shadow_pnl ?? null;
                  const delta = livePnl != null && shadowPnl != null ? shadowPnl - livePnl : null;
                  const statusLabel = (item.status ?? "unknown").toLowerCase();
                  return (
                    <tr key={`shadow-${item.id}`}>
                      <td>{formatTimestamp(item.created_at)}</td>
                      <td><span className={`badge badge--${statusLabel}`}>{item.status ?? "—"}</span></td>
                      <td>{item.strategy ?? "—"}</td>
                      <td>{item.symbol ?? "—"}</td>
                      <td>{item.direction ?? "—"}</td>
                      <td>{item.live_trade_id ?? "—"}</td>
                      <td>{item.shadow_trade_id ?? "—"}</td>
                      <td className={getPnLClass(livePnl ?? 0)}>{formatNumber(livePnl, 2)}</td>
                      <td className={getPnLClass(shadowPnl ?? 0)}>{formatNumber(shadowPnl, 2)}</td>
                      <td className={getPnLClass(delta ?? 0)}>{formatNumber(delta, 2)}</td>
                      <td>{item.live_exit_reason ?? "—"}</td>
                      <td>{item.shadow_exit_reason ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {shadowTotal > shadowItems.length && (
          <p style={{ marginTop: "0.75rem", opacity: 0.7 }}>
            Hinweis: Es werden nur die letzten {shadowItems.length} von {shadowTotal} Shadow-Einträgen angezeigt.
          </p>
        )}
      </div>
      )}

      {activeSection === "templates" && (
      <div className="grid grid--two-columns">
        <div className="card">
          <h3>Template speichern</h3>
          <form onSubmit={handleSaveTemplate} className="form form--vertical">
            <label className="form__label">
              Quelle (Settings)
              <select
                value={selectedTemplate}
                onChange={(event) => setSelectedTemplate(event.target.value)}
              >
                <option value={CURRENT_TEMPLATE_SENTINEL}>Aktuelle Settings</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name ?? template.id}
                  </option>
                ))}
              </select>
              <small className="form__hint">Aktiv: {selectedTemplateLabel}</small>
            </label>
            <label className="form__label">
              Template-Name
              <input
                type="text"
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
                placeholder="Night Momentum 2026-01-07"
                required
              />
            </label>
            <label className="form__label">
              Beschreibung
              <input
                type="text"
                value={templateDescription}
                onChange={(event) => setTemplateDescription(event.target.value)}
                placeholder="Kurzbeschreibung"
              />
            </label>
            <label className="form__label">
              Tags (comma separated)
              <input
                type="text"
                value={templateTags}
                onChange={(event) => setTemplateTags(event.target.value)}
                placeholder="night, experiment"
              />
            </label>

            <div className="settings-form">
              <div className="settings-form__header">
                <h4>Einstellungen</h4>
                <div className="settings-form__controls">
                  <input
                    type="text"
                    className="settings-form__search"
                    placeholder="Parameter suchen..."
                    value={filterSearch}
                    onChange={(event) => setFilterSearch(event.target.value)}
                  />
                  <span className="settings-form__count">{visibleFieldCount} Felder</span>
                </div>
              </div>

              {!settingsPreview ? (
                <p>Keine Settings geladen.</p>
              ) : (
                <>
                  <div className="settings-table-wrapper">
                    <table className="table settings-table">
                      <thead>
                        <tr>
                          <th>Parameter</th>
                          <th>Wert</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredFields.map((field) => {
                          return (
                            <tr key={field.path}>
                              <td className="settings-table__label-cell">
                                <div className="settings-table__label">
                                  <strong>{field.displayName}</strong>
                                  <code className="settings-table__path">{field.path}</code>
                                </div>
                                {field.tooltip && <p className="settings-table__hint">{field.tooltip}</p>}
                              </td>
                              <td className="settings-table__value-cell">{buildFieldInput(field.path)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {filteredFields.length === 0 && (
                    <p className="settings-table__empty">Keine Felder passend zum Filter.</p>
                  )}
                  <details className="settings-raw">
                    <summary>Raw JSON (optional)</summary>
                    <textarea
                      className="settings-field__textarea"
                      value={settingsJson}
                      onChange={(event) => handleRawJsonChange(event.target.value)}
                      rows={10}
                      spellCheck={false}
                    />
                    {jsonError && <p className="settings-field__error">{jsonError}</p>}
                  </details>
                </>
              )}
            </div>
            {templateError && <div className="alert alert--error">{templateError}</div>}
            {templateSuccess && <div className="alert alert--success">{templateSuccess}</div>}
            <button className="button button--primary" type="submit" disabled={templateSaving}>
              {templateSaving ? "Speichert..." : "Template speichern"}
            </button>
          </form>
        </div>

        <div className="card">
          <h3>Templates</h3>
          {templates.length === 0 ? (
            <p>Noch keine Templates gespeichert.</p>
          ) : (
            <ul className="template-list">
              {templates.map((tpl) => (
                <li key={tpl.id}>
                  <div className="template-list__info">
                    <strong>{tpl.name}</strong>
                    {tpl.description && <span>{tpl.description}</span>}
                    {(tpl.tags ?? []).length > 0 && (
                      <span className="template-list__tags">{tpl.tags.join(", ")}</span>
                    )}
                  </div>
                  <div className="template-list__actions">
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => setSelectedTemplate(tpl.id)}
                    >
                      Laden
                    </button>
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => handleDeleteTemplate(tpl.id)}
                    >
                      Löschen
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      )}

      {activeSection === "runs" && (
      <div className="grid grid--two-columns">
        <div className="card">
          <h3>Neuen Backtest starten</h3>
          <form onSubmit={handleStart} className="form form--vertical">
            <label className="form__label">
              Template / Settings
              <select
                value={selectedTemplate}
                onChange={(event) => setSelectedTemplate(event.target.value)}
              >
                <option value={CURRENT_TEMPLATE_SENTINEL}>Aktuelle Settings</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name ?? template.id}
                  </option>
                ))}
              </select>
            </label>
            <div className="form__row">
              <label className="form__label">
                Startdatum
                <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required />
              </label>
              <label className="form__label">
                Enddatum
                <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required />
              </label>
            </div>
            <div className="form__row">
              <label className="form__label">
                Marktstart-Stunde (0-23, optional)
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={startHour ?? ""}
                  onChange={(event) => {
                    const val = event.target.value;
                    setStartHour(val === "" ? null : Math.min(23, Math.max(0, parseInt(val, 10) || 0)));
                  }}
                  placeholder="z.B. 8"
                />
              </label>
              <label className="form__label">
                Marktend-Stunde (0-23, optional)
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={endHour ?? ""}
                  onChange={(event) => {
                    const val = event.target.value;
                    setEndHour(val === "" ? null : Math.min(23, Math.max(0, parseInt(val, 10) || 0)));
                  }}
                  placeholder="z.B. 17"
                />
              </label>
            </div>
            <label className="form__label">
              Zeitzone für Stundenfilter
              <input
                type="text"
                value={hourTimezone}
                onChange={(event) => setHourTimezone(event.target.value)}
                placeholder="UTC oder Europe/Berlin"
              />
              <small className="form__hint">
                Wird nur genutzt, wenn Start-/End-Stunde gesetzt sind.
              </small>
            </label>
            <fieldset>
              <legend>Strategien (leer lassen = alle)</legend>
              <div>
                {availableStrategies.length === 0 ? (
                  <div className="alert alert--info" style={{ marginBottom: "0.5rem" }}>
                    Keine Strategien gefunden – bitte Settings/Template laden oder erneut versuchen.
                  </div>
                ) : (
                  availableStrategies.map((option) => (
                    <label key={option.id} style={{ display: "block", marginBottom: "0.25rem" }}>
                      <input
                        type="checkbox"
                        value={option.id}
                        checked={selectedStrategies.includes(option.id)}
                        onChange={(event) => handleStrategyToggle(option.id, event.target.checked)}
                      />
                      <span style={{ marginLeft: "0.5rem" }}>
                        {option.label}
                        {!option.enabled && <em> (deaktiviert)</em>}
                      </span>
                    </label>
                  ))
                )}
              </div>
              <div className="form__actions">
                <button type="button" className="button button--secondary" onClick={handleSelectAllStrategies} disabled={availableStrategies.length === 0}>
                  Alle
                </button>
                <button type="button" className="button button--secondary" onClick={handleClearStrategies} disabled={selectedStrategies.length === 0}>
                  Keine
                </button>
                <button
                  type="button"
                  className="button button--secondary"
                  onClick={() => {
                    // Re-lade die Template-Vorschau, falls die Strategieliste leer war.
                    void loadTemplatePreview(selectedTemplate);
                  }}
                >
                  Strategien neu laden
                </button>
              </div>
            </fieldset>
            <label className="form__label">
              Notizen
              <textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="optional" rows={2} />
            </label>
            <label className="form__label" style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={simulateModeController}
                onChange={(event) => setSimulateModeController(event.target.checked)}
              />
              <span>Mode Controller simulieren (zeigt Vergleich: Normal vs. Mit Mode Controller)</span>
            </label>
            <label className="form__label" style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={useModeControlThresholdOverride}
                onChange={(event) => setUseModeControlThresholdOverride(event.target.checked)}
              />
              <span>Mode-Controller Parameter für diesen Backtest überschreiben</span>
            </label>
            {useModeControlThresholdOverride && (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))",
                  gap: "0.5rem",
                  padding: "0.75rem",
                  border: "1px solid #ddd",
                  borderRadius: "8px",
                  background: "#fafafa",
                }}
              >
                {MODE_CONTROL_OVERRIDE_FIELDS.map((field) => (
                  <label key={field.key} className="form__label" style={{ margin: 0 }}>
                    {field.label}
                    <input
                      type="number"
                      step={field.step ?? "0.1"}
                      min={field.min}
                      value={modeControlThresholdDrafts[field.key] ?? ""}
                      onChange={(event) =>
                        {
                          setSelectedModeControlProfile("custom");
                          setModeControlThresholdDrafts((prev) => ({
                            ...prev,
                            [field.key]: event.target.value,
                          }));
                        }
                      }
                    />
                  </label>
                ))}
                <label className="form__label" style={{ margin: 0, gridColumn: "1 / -1" }}>
                  Profil (Schnellwahl)
                  <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
                    <select
                      value={selectedModeControlProfile}
                      onChange={(event) => applyModeControlProfile(event.target.value as ModeControlProfileKey)}
                      style={{ minWidth: "260px" }}
                    >
                      <option value="fast_current">Schnell (aktuelle Settings)</option>
                      <option value="balanced">Balanced</option>
                      <option value="conservative">Conservative</option>
                      <option value="custom">Custom (manuell)</option>
                    </select>
                    <button
                      type="button"
                      className="button button--secondary"
                      onClick={() => applyModeControlProfile("fast_current")}
                    >
                      Aktuell laden
                    </button>
                  </div>
                </label>
                <small className="form__hint" style={{ gridColumn: "1 / -1" }}>
                  Diese Werte werden beim Backtest als `settings_override.strategy.mode_control.thresholds` gesetzt.
                  Beim Speichern eines Templates werden sie ebenfalls übernommen.
                </small>
              </div>
            )}
            <label className="form__label" style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={flipAllBranches}
                onChange={(event) => setFlipAllBranches(event.target.checked)}
              />
              <span>Alle Strategiezweige invertieren (long/short Flip) für diesen Backtest</span>
            </label>
            <label className="form__label" style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={useAdaptiveEntry}
                onChange={(event) => setUseAdaptiveEntry(event.target.checked)}
              />
              <span>Adaptive Entry-Tightness (Auto-Switcher) im Backtest verwenden</span>
            </label>
            <label className="form__label">
              Globale Entry-Tightness (Override für Backtest)
              <input
                type="number"
                min="0.05"
                step="0.05"
                placeholder="z.B. 0.8"
                value={globalEntryTightness}
                onChange={(event) => setGlobalEntryTightness(event.target.value)}
                style={{ width: "140px" }}
              />
              <small className="form__hint">Optional: überschreibt strategy.global_entry_tightness nur für diesen Backtest.</small>
            </label>
            <label className="form__label" style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={cacheOnly}
                onChange={(event) => setCacheOnly(event.target.checked)}
              />
              <span>Nur Cache verwenden (kein Live-Download fehlender Daten)</span>
            </label>
            <label className="form__label">
              Datenquelle für Backtest
              <select
                value={backtestDataSource}
                onChange={(event) => setBacktestDataSource(event.target.value)}
                style={{ marginLeft: "0.5rem", padding: "0.25rem", minWidth: "160px" }}
              >
                <option value="BINANCE">BINANCE</option>
                <option value="KRAKEN">KRAKEN</option>
                <option value="KRAKEN_BINANCE_5S_EXIT">KRAKEN + BINANCE 5s (Exit)</option>
              </select>
              <small className="form__hint">
                Wählt, welche OHLCV-Datenquelle für den Backtest genutzt wird (Cache ist je Exchange getrennt).
              </small>
            </label>
            <label className="form__label">
              Sample-Ratio (0.05 - 1.0)
              <input
                type="number"
                min="0.05"
                max="1"
                step="0.05"
                value={sampleRatio}
                onChange={(event) => setSampleRatio(event.target.value)}
                style={{ width: "120px" }}
              />
              <small className="form__hint">Anteil des Zeitfensters, der tatsächlich gebacktestet wird (1.0 = voller Zeitraum).</small>
            </label>
            <label className="form__label">
              Parallele CPU-Cores
              <input
                type="number"
                min="1"
                max="8"
                value={maxParallelCores}
                onChange={(event) => setMaxParallelCores(Math.max(1, Math.min(8, parseInt(event.target.value) || 2)))}
                style={{ width: "100px" }}
              />
              <small style={{ display: "block", marginTop: "0.25rem", color: "#666" }}>
                Anzahl der parallelen CPU-Cores für die Verarbeitung mehrerer Instrumente (Standard: 2)
              </small>
            </label>
            {startError && <div className="alert alert--error">{startError}</div>}
            {startSuccess && <div className="alert alert--success">{startSuccess}</div>}
            <button className="button button--primary" type="submit" disabled={startLoading}>
              {startLoading ? "Backtest läuft..." : "Backtest starten"}
            </button>
          </form>
        </div>

        <div className="card">
          <h3>Settings-Vorschau</h3>
          {settingsPreview ? (
            <div className="settings-preview">
              <div className="table-wrapper">
                <table className="table table--compact">
                  <thead>
                    <tr>
                      <th>Feld</th>
                      <th>Wert</th>
                    </tr>
                  </thead>
                  <tbody>
                    {settingsFields.slice(0, 80).map((field) => {
                      const value = getNestedValue(settingsPreview, field.path);
                      return (
                        <tr key={field.path}>
                          <td>
                            <strong>{field.displayName}</strong>
                            <div className="table__path">{field.path}</div>
                            {tooltips[field.path] && <div className="table__hint">{tooltips[field.path]}</div>}
                          </td>
                          <td>
                            <code>{JSON.stringify(value ?? null)}</code>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {settingsFields.length > 80 && <p className="card__hint">Nur die ersten 80 Felder werden angezeigt.</p>}
            </div>
          ) : (
            <p>Keine Settings verfügbar.</p>
          )}
        </div>
      </div>
      )}

      {activeSection === "runs" && (
      <div className="card">
        <div className="card__header">
          <h3>Backtest-Läufe</h3>
          {listLoading && <span className="badge badge--running">Aktualisiere…</span>}
        </div>
        {listError && <div className="alert alert--error">{listError}</div>}
        {backtests.length === 0 ? (
          <p>Noch keine Backtests vorhanden.</p>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Status</th>
                    <th>Zeitraum</th>
                    <th>Datenabdeckung</th>
                    <th>Template</th>
                    <th>Gestartet</th>
                    <th>Fertig</th>
                    <th>Strategien</th>
                    <th>Notizen</th>
                    <th>Aktion</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedBacktests.map((bt) => (
                    <tr
                      key={bt.id}
                      className={selectedBacktestId === bt.id ? "table__row--active" : ""}
                    >
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{bt.id}</td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>
                        <span className={`badge badge--${bt.status}`}>{bt.status}</span>
                      </td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>
                        {bt.start_date} → {bt.end_date}
                      </td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>
                        {bt.data_coverage_pct != null ? `${formatNumber(bt.data_coverage_pct, 1)}%` : "—"}
                        {bt.sample_ratio != null && ` (Sample ${formatNumber(bt.sample_ratio * 100, 0)}%)`}
                      </td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{bt.template_name ?? (bt.template_id ?? "Aktuelle Settings")}</td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{formatTimestamp(bt.started_at)}</td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{formatTimestamp(bt.completed_at)}</td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{bt.strategy_filters?.length ? bt.strategy_filters.join(", ") : "Alle"}</td>
                      <td onClick={() => setSelectedBacktestId(bt.id)}>{bt.notes ?? "—"}</td>
                      <td>
                        <div className="table__actions">
                          <button type="button" onClick={() => setSelectedBacktestId(bt.id)}>
                            Anzeigen
                          </button>
                          {(bt.status === "running" || bt.status === "queued") && (
                            <button
                              type="button"
                              className="button-secondary"
                              onClick={(e) => {
                                e.stopPropagation();
                                void handleCancel(bt.id);
                              }}
                            >
                              Abbrechen
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {renderPagination(totalBacktestPages, backtestPage, setBacktestPage)}
          </>
        )}
      </div>
      )}

      {activeSection === "runs" && (
      <div className="grid grid--two-columns">
        <div className="card">
          <div className="card__header">
            <h3>Backtest-Details</h3>
            {detailLoading && <span className="badge badge--running">Lädt…</span>}
          </div>
          {detailError && <div className="alert alert--error">{detailError}</div>}
          {!detail && !detailError && <p>Bitte einen Backtest auswählen.</p>}
          {detail && (
            <div className="stack stack--vertical">
              <div className="backtest-summary">
                <p>
                  <strong>Status:</strong> <span className={`badge badge--${detail.status}`}>{detail.status}</span>
                </p>
                <p>
                  <strong>Zeitraum:</strong> {detail.start_date} → {detail.end_date}
                </p>
                <p>
                  <strong>Gestartet:</strong> {formatTimestamp(detail.started_at)} &nbsp; | &nbsp;
                  <strong>Fertig:</strong> {formatTimestamp(detail.completed_at)}
                </p>
                <p>
                  <strong>Datenabdeckung (Cache):</strong>{" "}
                  {detail.data_coverage_pct != null ? `${formatNumber(detail.data_coverage_pct, 2)}%` : "—"}
                  {detail.sample_ratio != null && (
                    <>
                      {" "}| <strong>Sample-Ratio:</strong> {formatNumber(detail.sample_ratio * 100, 0)}%
                    </>
                  )}
                </p>
                <p>
                  <strong>Notizen:</strong> {detail.notes ?? "—"}
                </p>
                <p>
                  <strong>Strategien:</strong> {detail.strategy_filters?.length ? detail.strategy_filters.join(", ") : "Alle"}
                </p>
                {detail.error && (
                  <div className="alert alert--error">
                    <strong>Fehler:</strong> {detail.error}
                  </div>
                )}
              </div>
              {detail && detail.status === "completed" && (
                <div className="stack stack--vertical">
                  {summarySlices ? (
                    <div className="stack stack--vertical">
                      {/* Gesamtergebnis - zeige beide Varianten wenn Mode Controller aktiviert */}
                      {(summarySlices as any).summary_normal && (summarySlices as any).summary_with_mode_controller ? (
                        <div className="card card--compact">
                          <h4>Gesamtergebnis (Vergleich)</h4>
                          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                            <div>
                              <h5>Normal (alle Trades)</h5>
                              <ul className="metrics-list">
                                <li>Trades <strong>{(summarySlices as any).summary_normal.trades}</strong></li>
                                <li>Wins <strong>{(summarySlices as any).summary_normal.wins}</strong></li>
                                <li>Win-Rate <strong>{formatPercent((summarySlices as any).summary_normal.win_rate)}</strong></li>
                                <li>
                                  PnL (Brutto){" "}
                                  <strong>
                                    {formatNumber(
                                      getGrossPnl((summarySlices as any).summary_normal),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  PnL (Netto){" "}
                                  <strong>
                                    {formatNumber(
                                      getNetPnl((summarySlices as any).summary_normal),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  Fees (aktuell){" "}
                                  <strong>
                                    {formatNumber(
                                      getCurrentFees((summarySlices as any).summary_normal),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  PnL (Original){" "}
                                  <strong style={{ opacity: 0.7 }}>
                                    {formatNumber((summarySlices as any).summary_normal.pnl, 2)}
                                  </strong>
                                </li>
                              </ul>
                            </div>
                            <div>
                              <h5>Mit Mode Controller (nur Live)</h5>
                              <ul className="metrics-list">
                                <li>Trades <strong>{(summarySlices as any).summary_with_mode_controller.trades}</strong></li>
                                <li>Wins <strong>{(summarySlices as any).summary_with_mode_controller.wins}</strong></li>
                                <li>Win-Rate <strong>{formatPercent((summarySlices as any).summary_with_mode_controller.win_rate)}</strong></li>
                                <li>
                                  PnL (Brutto){" "}
                                  <strong>
                                    {formatNumber(
                                      getGrossPnl((summarySlices as any).summary_with_mode_controller),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  PnL (Netto){" "}
                                  <strong>
                                    {formatNumber(
                                      getNetPnl((summarySlices as any).summary_with_mode_controller),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  Fees (aktuell){" "}
                                  <strong>
                                    {formatNumber(
                                      getCurrentFees((summarySlices as any).summary_with_mode_controller),
                                      2
                                    )}
                                  </strong>
                                </li>
                                <li>
                                  PnL (Original){" "}
                                  <strong style={{ opacity: 0.7 }}>
                                    {formatNumber((summarySlices as any).summary_with_mode_controller.pnl, 2)}
                                  </strong>
                                </li>
                              </ul>
                            </div>
                          </div>
                        </div>
                      ) : (
                        (() => {
                          const summaryAll = ((summarySlices as any).summary_normal ?? summarySlices.summary) as SummarySlice;
                          return (
                            <div className="card card--compact">
                              <h4>Gesamtergebnis</h4>
                              <ul className="metrics-list">
                                <li>Trades <strong>{summaryAll.trades}</strong></li>
                                <li>Wins <strong>{summaryAll.wins}</strong></li>
                                <li>Win-Rate <strong>{formatPercent(summaryAll.win_rate)}</strong></li>
                                <li>
                                  PnL (Brutto){" "}
                                  <strong>{formatNumber(getGrossPnl(summaryAll), 2)}</strong>
                                </li>
                                <li>
                                  PnL (Netto){" "}
                                  <strong>{formatNumber(getNetPnl(summaryAll), 2)}</strong>
                                </li>
                                <li>
                                  Fees (aktuell){" "}
                                  <strong>{formatNumber(getCurrentFees(summaryAll), 2)}</strong>
                                </li>
                                <li>
                                  PnL (Original){" "}
                                  <strong style={{ opacity: 0.7 }}>
                                    {formatNumber(summaryAll.pnl, 2)}
                                  </strong>
                                </li>
                              </ul>
                            </div>
                          );
                        })()
                      )}
                      {renderSliceTable(
                        "Nach Symbol",
                        summarySlices.by_symbol as SummarySlice[],
                        [
                          { key: "symbol", label: "Symbol", sortValue: (row) => row.symbol ?? "" },
                          { key: "trades", label: "Trades", sortValue: (row) => row.trades },
                          { key: "wins", label: "Wins", sortValue: (row) => row.wins },
                          { key: "win_rate", label: "Win-Rate", formatter: formatPercent, sortValue: (row) => row.win_rate },
                          {
                            key: "pnl",
                            label: "PnL (Netto)",
                            formatter: (value, row) => {
                              const netPnl = getNetPnl(row as { pnl: number; fees?: number });
                              return formatNumber(netPnl, 2);
                            },
                            sortValue: (row) => getNetPnl(row as { pnl: number; fees?: number })
                          },
                          {
                            key: "avg_notional",
                            label: "Ø Notional/Trade",
                            formatter: (value, row) => {
                              const avgNotional = getAvgNotional(row as SummarySlice);
                              return avgNotional == null ? "—" : formatNumber(avgNotional, 2);
                            },
                            sortValue: (row) => getAvgNotional(row as SummarySlice) ?? -Infinity
                          },
                          {
                            key: "pnl_per_trade",
                            label: "PnL/Trade (%)",
                            cellRenderer: (_, row) => {
                              const pnlPerTradePct = getPnlPerTradePct(row as SummarySlice);
                              return (
                                <span className={pnlPerTradePct == null ? "" : getPnLClass(pnlPerTradePct)}>
                                  {pnlPerTradePct == null ? "—" : `${formatNumber(pnlPerTradePct, 2)}%`}
                                </span>
                              );
                            },
                            sortValue: (row) => {
                              return getPnlPerTradePct(row as SummarySlice) ?? -Infinity;
                            }
                          },
                          {
                            key: "fees",
                            label: "Fees (aktuell)",
                            formatter: (value, row) => {
                              const currentFees = getCurrentFees(row as { pnl: number; fees?: number });
                              return formatNumber(currentFees, 2);
                            },
                            sortValue: (row) => getCurrentFees(row as { pnl: number; fees?: number })
                          }
                        ],
                        {
                          sortState: sliceSorts.by_symbol ?? null,
                          onSort: (key) => handleSliceSort("by_symbol", key)
                        }
                      )}
                      {renderSliceTable(
                        "Nach Strategie",
                        strategySummaryRows,
                        [
                          { key: "strategy", label: "Strategie", sortValue: (row) => row.strategy ?? "" },
                          { key: "trades", label: "Trades", sortValue: (row) => row.trades },
                          { key: "wins", label: "Wins", sortValue: (row) => row.wins },
                          { key: "win_rate", label: "Win-Rate", formatter: formatPercent, sortValue: (row) => row.win_rate },
                          {
                            key: "pnl_gross",
                            label: "PnL (Brutto)",
                            formatter: (value, row) => {
                              const grossPnl = getGrossPnl(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(grossPnl, 2);
                            },
                            sortValue: (row) => getGrossPnl(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "pnl",
                            label: "PnL (Netto)",
                            formatter: (value, row) => {
                              const netPnl = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(netPnl, 2);
                            },
                            sortValue: (row) => getNetPnl(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "avg_notional",
                            label: "Ø Notional/Trade",
                            formatter: (value, row) => {
                              const avgNotional = getAvgNotional(row as SummarySlice);
                              return avgNotional == null ? "—" : formatNumber(avgNotional, 2);
                            },
                            sortValue: (row) => getAvgNotional(row as SummarySlice) ?? -Infinity
                          },
                          {
                            key: "fees",
                            label: "Fees (aktuell)",
                            formatter: (value, row) => {
                              const currentFees = getCurrentFees(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(currentFees, 2);
                            },
                            sortValue: (row) => getCurrentFees(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "pnl_per_trade",
                            label: "PnL/Trade (%)",
                            cellRenderer: (_, row) => {
                              const pnlPerTradePct = getPnlPerTradePct(row as SummarySlice);
                              return (
                                <span className={pnlPerTradePct == null ? "" : getPnLClass(pnlPerTradePct)}>
                                  {pnlPerTradePct == null ? "—" : `${formatNumber(pnlPerTradePct, 2)}%`}
                                </span>
                              );
                            },
                            sortValue: (row) => {
                              return getPnlPerTradePct(row as SummarySlice) ?? -Infinity;
                            }
                          }
                        ],
                        {
                          sortState: sliceSorts.by_strategy ?? null,
                          onSort: (key) => handleSliceSort("by_strategy", key)
                        }
                      )}
                      {renderSliceTable(
                        "Nach Richtung",
                        summarySlices.by_side as SummarySlice[],
                        [
                          { key: "side", label: "Richtung", sortValue: (row) => row.side ?? "" },
                          { key: "trades", label: "Trades", sortValue: (row) => row.trades },
                          { key: "wins", label: "Wins", sortValue: (row) => row.wins },
                          { key: "win_rate", label: "Win-Rate", formatter: formatPercent, sortValue: (row) => row.win_rate },
                          {
                            key: "pnl_gross",
                            label: "PnL (Brutto)",
                            formatter: (value, row) => {
                              const grossPnl = getGrossPnl(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(grossPnl, 2);
                            },
                            sortValue: (row) => getGrossPnl(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "pnl",
                            label: "PnL (Netto)",
                            formatter: (value, row) => {
                              const netPnl = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(netPnl, 2);
                            },
                            sortValue: (row) => getNetPnl(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "avg_notional",
                            label: "Ø Notional/Trade",
                            formatter: (value, row) => {
                              const avgNotional = getAvgNotional(row as SummarySlice);
                              return avgNotional == null ? "—" : formatNumber(avgNotional, 2);
                            },
                            sortValue: (row) => getAvgNotional(row as SummarySlice) ?? -Infinity
                          },
                          {
                            key: "fees",
                            label: "Fees (aktuell)",
                            formatter: (value, row) => {
                              const currentFees = getCurrentFees(row as { pnl: number; fees?: number; trades?: number });
                              return formatNumber(currentFees, 2);
                            },
                            sortValue: (row) => getCurrentFees(row as { pnl: number; fees?: number; trades?: number })
                          },
                          {
                            key: "pnl_per_trade",
                            label: "PnL/Trade (%)",
                            cellRenderer: (_, row) => {
                              const pnlPerTradePct = getPnlPerTradePct(row as SummarySlice);
                              return (
                                <span className={pnlPerTradePct == null ? "" : getPnLClass(pnlPerTradePct)}>
                                  {pnlPerTradePct == null ? "—" : `${formatNumber(pnlPerTradePct, 2)}%`}
                                </span>
                              );
                            },
                            sortValue: (row) => {
                              return getPnlPerTradePct(row as SummarySlice) ?? -Infinity;
                            }
                          }
                        ],
                        {
                          sortState: sliceSorts.by_side ?? null,
                          onSort: (key) => handleSliceSort("by_side", key)
                        }
                      )}
                      {/* Zeige by_symbol_strategy_side: Vergleich (falls vorhanden) oder flache Liste */}
                      {(summarySlices as any).by_symbol_strategy_side && Array.isArray((summarySlices as any).by_symbol_strategy_side) && (summarySlices as any).by_symbol_strategy_side.length > 0 && (summarySlices as any).by_symbol_strategy_side[0].normal ? (
                        <div className="card card--compact">
                          <h4>Nach Strategie × Symbol × Richtung (Vergleich)</h4>
                          <div className="table-wrapper">
                            <table className="table table--compact">
                              <thead>
                                <tr>
                                  <th>Strategie</th>
                                  <th>Symbol</th>
                                  <th>Richtung</th>
                                  <th colSpan={5} style={{ textAlign: "center", borderLeft: "2px solid #ccc", borderRight: "2px solid #ccc" }}>Normal</th>
                                  <th colSpan={5} style={{ textAlign: "center" }}>Mit Mode Controller</th>
                                </tr>
                                <tr>
                                  <th></th>
                                  <th></th>
                                  <th></th>
                                  <th>Trades</th>
                                  <th>Wins</th>
                                  <th>Win-Rate</th>
                                  <th>PnL (Brutto)</th>
                                  <th>PnL (Netto)</th>
                                  <th>Fees (aktuell)</th>
                                  <th>Trades</th>
                                  <th>Wins</th>
                                  <th>Win-Rate</th>
                                  <th>PnL (Brutto)</th>
                                  <th>PnL (Netto)</th>
                                  <th>Fees (aktuell)</th>
                                </tr>
                              </thead>
                              <tbody>
                                {((summarySlices as any).by_symbol_strategy_side as Array<{
                                  symbol: string;
                                  strategy: string;
                                  side: string;
                                  normal: { trades: number; wins: number; win_rate: number; pnl: number; fees: number };
                                  with_mode_controller: { trades: number; wins: number; win_rate: number; pnl: number; fees: number };
                                }>).map((row, index) => (
                                  <tr key={`${row.strategy}-${row.symbol}-${row.side}-${index}`}>
                                    <td>{row.strategy}</td>
                                    <td>{row.symbol}</td>
                                    <td>{row.side}</td>
                                    <td>{row.normal.trades}</td>
                                    <td>{row.normal.wins}</td>
                                    <td>{formatPercent(row.normal.win_rate)}</td>
                                    <td>{formatNumber(getGrossPnl(row.normal), 2)}</td>
                                    <td className={getPnLClass(calculatePnLPerTrade(getNetPnl(row.normal), row.normal.trades))}>
                                      {formatNumber(getNetPnl(row.normal), 2)}
                                    </td>
                                    <td>{formatNumber(getCurrentFees(row.normal), 2)}</td>
                                    <td>{row.with_mode_controller.trades}</td>
                                    <td>{row.with_mode_controller.wins}</td>
                                    <td>{formatPercent(row.with_mode_controller.win_rate)}</td>
                                    <td>{formatNumber(getGrossPnl(row.with_mode_controller), 2)}</td>
                                    <td className={getPnLClass(calculatePnLPerTrade(getNetPnl(row.with_mode_controller), row.with_mode_controller.trades))}>
                                      {formatNumber(getNetPnl(row.with_mode_controller), 2)}
                                    </td>
                                    <td>{formatNumber(getCurrentFees(row.with_mode_controller), 2)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      ) : (summarySlices as any).by_symbol_strategy_side && Array.isArray((summarySlices as any).by_symbol_strategy_side) && (summarySlices as any).by_symbol_strategy_side.length > 0 ? (
                        <div className="card card--compact">
                          <h4>Nach Strategie × Symbol × Richtung</h4>
                          {renderSliceTable(
                            "",
                            symbolStrategySideRows as SummarySlice[],
                            [
                              { key: "strategy", label: "Strategie / Alias", sortValue: (row) => row.strategy ?? "" },
                              { key: "symbol", label: "Symbol", sortValue: (row) => row.symbol ?? "" },
                              { key: "side", label: "Richtung", sortValue: (row) => row.side ?? "" },
                              { key: "trades", label: "Trades", sortValue: (row) => row.trades },
                              { key: "wins", label: "Wins", sortValue: (row) => row.wins },
                              { key: "win_rate", label: "Win-Rate", formatter: formatPercent, sortValue: (row) => row.win_rate },
                              {
                                key: "pnl",
                                label: "PnL (Netto)",
                                cellRenderer: (_, row) => {
                                  const net = getNetPnl(row as { pnl: number; fees?: number; trades?: number });
                                  return <span className={getPnLClass(net)}>{formatNumber(net, 2)}</span>;
                                },
                                sortValue: (row) => getNetPnl(row as { pnl: number; fees?: number; trades?: number })
                              },
                              {
                                key: "avg_notional",
                                label: "Ø Notional/Trade",
                                formatter: (value, row) => {
                                  const avgNotional = getAvgNotional(row as SummarySlice);
                                  return avgNotional == null ? "—" : formatNumber(avgNotional, 2);
                                },
                                sortValue: (row) => getAvgNotional(row as SummarySlice) ?? -Infinity
                              },
                              {
                                key: "pnl_per_trade",
                                label: "PnL/Trade (%)",
                                cellRenderer: (_, row) => {
                                  const val = getPnlPerTradePct(row as SummarySlice);
                                  return (
                                    <span className={val == null ? "" : getPnLClass(val)}>
                                      {val == null ? "—" : `${formatNumber(val, 2)}%`}
                                    </span>
                                  );
                                },
                                sortValue: (row) => {
                                  return getPnlPerTradePct(row as SummarySlice) ?? -Infinity;
                                }
                              },
                              {
                                key: "fees",
                                label: "Fees (aktuell)",
                                cellRenderer: (_, row) => {
                                  const fees = getCurrentFees(row as { pnl: number; fees?: number; trades?: number });
                                  return formatNumber(fees, 2);
                                },
                                sortValue: (row) => getCurrentFees(row as { pnl: number; fees?: number; trades?: number })
                              }
                            ],
                            {
                              sortState: sliceSorts.by_symbol_strategy_side ?? null,
                              onSort: (key) => handleSliceSort("by_symbol_strategy_side", key)
                            }
                          )}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="alert alert--warning">
                      <strong>Hinweis:</strong> Backtest abgeschlossen, aber keine Trades gefunden. 
                      Mögliche Gründe: Keine Daten im Zeitraum, keine passenden Strategien, oder Filter haben alle Trades ausgeschlossen.
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card__header">
            <h3>Backtest-Log</h3>
            {logState.loading && <span className="badge badge--running">Lädt…</span>}
          </div>
          {logState.lines.length === 0 ? (
            <p>Kein Log verfügbar.</p>
          ) : (
            <pre className="log-viewer">{logState.lines.join("")}</pre>
          )}
          {logState.path && (
            <p className="card__hint">
              Quelle: <code>{logState.path}</code>
            </p>
          )}
        </div>
      </div>
      )}
    </div>
  );
}

import axios from "axios";

// BASE_URL sollte leer sein, damit relative URLs über den Vite-Proxy gehen
// Im Dev-Modus (Vite dev server) verwenden wir IMMER relative URLs über den Proxy
// Nur in Production (build) sollte VITE_API_BASE gesetzt sein
// BASE_URL sollte leer sein, damit relative URLs verwendet werden
// Im Dev-Modus: leer für Proxy
// Im Production: Wenn die Seite vom gleichen Server geladen wird (z.B. Port 8601),
// sollten relative URLs verwendet werden, um CORS-Probleme zu vermeiden
const BASE_URL = (() => {
  if (import.meta.env.DEV) {
    // Dev-Modus: Immer leer, damit Proxy verwendet wird
    return "";
  }
  
  // Production-Modus: Prüfe ob VITE_API_BASE gesetzt ist
  const envBase = import.meta.env.VITE_API_BASE;
  
  // Wenn nicht gesetzt oder leer, verwende relative URLs
  if (!envBase || envBase.trim() === "") {
    return "";
  }
  
  // Wenn VITE_API_BASE auf localhost zeigt, aber die Seite von einer anderen Origin geladen wird,
  // verwende relative URLs, um CORS-Probleme zu vermeiden
  const currentOrigin = window.location.origin;
  const isLocalhost = envBase.includes("127.0.0.1") || envBase.includes("localhost");
  const isCurrentLocalhost = currentOrigin.includes("127.0.0.1") || currentOrigin.includes("localhost");
  
  if (isLocalhost && !isCurrentLocalhost) {
    console.warn("VITE_API_BASE zeigt auf localhost, aber Seite wird von", currentOrigin, "geladen - verwende relative URLs");
    return "";
  }
  
  return envBase;
})();

// Debug: Zeige BASE_URL im Dev-Modus
if (import.meta.env.DEV) {
  console.log("API Client initialized (DEV mode) with BASE_URL:", BASE_URL || "(empty - using relative URLs via proxy)");
  if (BASE_URL) {
    console.warn("⚠️ WARNING: BASE_URL ist im Dev-Modus gesetzt! Das sollte leer sein für Proxy.");
  }
  // Prüfe ob wir über Proxy zugreifen können
  console.log("Frontend URL:", window.location.origin);
  console.log("Verwende relative URLs für API-Calls über Proxy");
}

export const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,  // Erhöht auf 30 Sekunden
  headers: {
    "Content-Type": "application/json",
  },
  // Stelle sicher, dass Credentials nicht gesendet werden (kann CORS-Probleme verursachen)
  withCredentials: false,
});

// Request Interceptor für Debugging
apiClient.interceptors.request.use(
  (config) => {
    if (import.meta.env.DEV) {
      console.debug("API Request:", config.method?.toUpperCase(), config.url, config.baseURL || "(relative)");
    }
    return config;
  },
  (error) => {
    console.error("Request error:", error);
    return Promise.reject(error);
  }
);

// Error Interceptor für besseres Error-Handling
apiClient.interceptors.response.use(
  (response) => {
    if (import.meta.env.DEV) {
      console.debug("API Response:", response.status, response.config.url);
    }
    return response;
  },
  (error) => {
    if (error.code === "ECONNABORTED") {
      console.error("Request timeout:", error.config?.url, error.config?.baseURL);
      return Promise.reject(new Error("Request timeout - Server antwortet nicht rechtzeitig"));
    }
    if (error.message === "Network Error" || error.code === "ERR_NETWORK") {
      const errorDetails = {
        url: error.config?.url,
        baseURL: error.config?.baseURL,
        method: error.config?.method,
        fullURL: error.config?.baseURL ? `${error.config.baseURL}${error.config.url}` : error.config?.url,
        fullError: error,
      };
      console.error("Network error:", errorDetails);
      
      // Prüfe ob BASE_URL gesetzt ist (sollte leer sein für Dev-Modus)
      if (error.config?.baseURL) {
        console.warn("⚠️ BASE_URL ist gesetzt:", error.config.baseURL, "- sollte leer sein für Dev-Modus (Proxy)");
        console.warn("Versuche direkten Zugriff statt Proxy - das könnte das Problem sein!");
      } else {
        console.info("✓ BASE_URL ist leer - verwende relative URLs über Proxy");
      }
      
      // Erstelle eine hilfreichere Fehlermeldung
      let errorMsg = `Network Error - Verbindung zum Server fehlgeschlagen.`;
      if (import.meta.env.DEV) {
        const expectedURL = `${window.location.origin}${errorDetails.url}`;
        errorMsg += ` URL: ${errorDetails.url}.`;
        errorMsg += ` Im Dev-Modus sollten API-Calls über den Vite-Proxy gehen (${expectedURL}).`;
        errorMsg += ` Bitte prüfen Sie, ob der Vite Dev-Server läuft und der Proxy korrekt konfiguriert ist.`;
        if (errorDetails.baseURL) {
          errorMsg += ` WARNUNG: BASE_URL ist gesetzt (${errorDetails.baseURL}) - sollte leer sein für Proxy!`;
        }
      } else {
        errorMsg += ` URL: ${errorDetails.fullURL || errorDetails.url}`;
      }
      errorMsg += ` Bitte prüfen Sie die Browser-Konsole für Details.`;
      
      return Promise.reject(new Error(errorMsg));
    }
    if (error.response) {
      // Server hat geantwortet, aber mit Fehler-Status
      console.error("API error:", error.response.status, error.response.data);
      return Promise.reject(new Error(`Server Error: ${error.response.status} - ${error.response.data?.detail || error.response.data?.message || "Unbekannter Fehler"}`));
    }
    console.error("Unknown error:", error);
    return Promise.reject(error);
  }
);

export interface BotStatus {
  run_id: string | null;
  status: string;
  started_at: string | null;
  heartbeat: string | null;
  runtime_seconds: number | null;
}

export async function fetchStatus(): Promise<BotStatus> {
  const { data } = await apiClient.get<BotStatus>("/api/status");
  return data;
}

export interface DashboardResponse {
  status: BotStatus | null;
  run: CurrentRunInfo | null;
  backtest: BacktestSnapshot | null;
  backtest_thread_status: BacktestThreadStatus | null;
  trades: CurrentTradesResponse;
  live_trades: CurrentTradesResponse;
  exchange_paper_trades?: CurrentTradesResponse;
  strategy_sim_trades?: CurrentTradesResponse;
  metrics: CurrentMetricsResponse;
  strategy_stats: StrategyStat[];
  strategy_stats_by_environment?: Record<string, StrategyStat[]>;
  mode_state: ModeStateSnapshot | null;
  settings: SettingsResponse | null;
  reference_backtest_summary?: BacktestSummaryPayload | null;
  reference_backtest_id?: string | null;
  errors?: Record<string, string>;
}

export async function fetchDashboard(
  tradesLimit = 100,
  metricsLimit = 100,
  includeSettings = true,
  referenceBacktestId?: string | null
): Promise<DashboardResponse> {
  const { data } = await apiClient.get<DashboardResponse>("/api/dashboard", {
    params: {
      trades_limit: tradesLimit,
      metrics_limit: metricsLimit,
      include_settings: includeSettings,
      reference_backtest_id: referenceBacktestId || undefined,
    },
  });
  return data;
}

export interface BacktestThreadStatus {
  run_id: string | null;
  thread_running: boolean;
  thread_id: number | null;
  thread_name?: string | null;
}

export async function fetchBacktestThreadStatus(): Promise<BacktestThreadStatus> {
  const { data } = await apiClient.get<BacktestThreadStatus>("/api/backtest/thread-status");
  return data;
}

export interface DeployRequest {
  settings: Record<string, unknown>;
  template_id?: string;
  template_name?: string;
  save_template?: boolean;
  template_description?: string;
  tags?: string[];
}

export interface DeployResponse {
  run_id: string;
  template_id?: string | null;
  template_name?: string | null;
  started_at: string;
}

export async function deploySettings(payload: DeployRequest): Promise<DeployResponse> {
  const { data } = await apiClient.post<DeployResponse>("/api/deploy", payload);
  return data;
}

export interface SettingsResponse {
  path: string;
  settings: Record<string, unknown>;
}

export interface DataHealthItem {
  timestamp: string;
  exchange: string;
  symbol: string;
  interval: string;
  window_start: string;
  window_end: string;
  expected_bars: number;
  actual_bars: number;
  coverage_pct: number;
  max_gap_seconds: number;
  last_bar_age_seconds: number;
  status: "ok" | "warn";
  notes?: string | null;
}

export interface DataHealthResponse {
  items: DataHealthItem[];
  summary: { total: number; warn: number; ok: number };
}

export interface SshAllowlistResponse {
  ips: string[];
  source: string;
  path?: string | null;
  allowed_user?: string;
  requires_public_key?: boolean;
}

export async function fetchCurrentSettings(): Promise<SettingsResponse> {
  const { data } = await apiClient.get<SettingsResponse>("/api/settings/current");
  return data;
}

export async function fetchCurrentDataHealth(): Promise<DataHealthResponse> {
  const { data } = await apiClient.get<DataHealthResponse>("/api/run/current/data-health");
  return data;
}

export async function fetchSshAllowlist(): Promise<SshAllowlistResponse> {
  const { data } = await apiClient.get<SshAllowlistResponse>("/api/security/ssh-allowlist");
  return data;
}

export async function updateSshAllowlist(ips: string[]): Promise<SshAllowlistResponse> {
  const { data } = await apiClient.post<SshAllowlistResponse>("/api/security/ssh-allowlist", { ips });
  return data;
}

export interface CPUStats {
  system: {
    cpu_percent: number;
    load_avg: [number, number, number];
    cpu_count: number;
  };
  processes: Array<{
    pid: number;
    name: string;
    cmdline: string;
    cpu_percent: number;
    memory_percent: number;
    type: string;
    thread_id?: number | null;
    thread_name?: string | null;
  }>;
  error?: string;
}

export async function fetchCPUStats(): Promise<CPUStats> {
  const { data } = await apiClient.get<CPUStats>("/api/system/cpu-stats");
  return data;
}

export interface ModePathState {
  current_mode: string;
  manual_enabled: boolean;
  manual_mode: string | null;
  auto_enabled: boolean;
  last_switch_reason?: string | null;
  last_switch_at?: string | null;
  cooldown_until?: string | null;
  auto_switch_count_date?: string | null;
  auto_switch_count: number;
  degraded_reason?: string | null;
}

export interface ModeStateSnapshot {
  global_auto_enabled: boolean;
  paths: Record<string, ModePathState>;
}

export interface ModeThresholdsConfig {
  cooldown_minutes?: number;
  max_auto_switches_per_day?: number;
  ema_period?: number;
  ema_threshold_pct?: number;
  noise_threshold_pct?: number;
  lookback_trades?: number;
}

export interface ModeControlConfig {
  enabled: boolean;
  default_mode: "live" | "test";
  auto_enabled_by_default: boolean;
  thresholds: ModeThresholdsConfig;
}

export async function fetchModeState(): Promise<ModeStateSnapshot> {
  const { data } = await apiClient.get<ModeStateSnapshot>("/api/trade-modes");
  return data;
}

export async function setGlobalModeAuto(enabled: boolean): Promise<ModeStateSnapshot> {
  const { data } = await apiClient.post<ModeStateSnapshot>("/api/trade-modes/auto", { enabled });
  return data;
}

export interface ForceTestModeResponse extends ModeStateSnapshot {
  affected_paths: number;
  switched_to_test: number;
}

export async function forceAllTradeModesToTest(): Promise<ForceTestModeResponse> {
  const { data } = await apiClient.post<ForceTestModeResponse>("/api/trade-modes/force-test", {
    disable_global_auto: true,
    disable_path_auto: true,
  });
  return data;
}

function encodePathSegment(segment: string): string {
  return encodeURIComponent(segment);
}

export async function setManualEnabled(
  strategy: string,
  symbol: string,
  direction: string,
  enabled: boolean
): Promise<{ path: string; state: ModePathState }> {
  const uri = `/api/trade-modes/${encodePathSegment(strategy)}/${encodePathSegment(symbol)}/${encodePathSegment(direction)}/manual`;
  const { data } = await apiClient.post<{ path: string; state: ModePathState }>(uri, { enabled });
  return data;
}

export async function setManualMode(
  strategy: string,
  symbol: string,
  direction: string,
  mode: "live" | "test" | null
): Promise<{ path: string; state: ModePathState }> {
  const uri = `/api/trade-modes/${encodePathSegment(strategy)}/${encodePathSegment(symbol)}/${encodePathSegment(direction)}/mode`;
  const { data } = await apiClient.post<{ path: string; state: ModePathState }>(uri, { mode });
  return data;
}

export async function setAutoEnabled(
  strategy: string,
  symbol: string,
  direction: string,
  enabled: boolean
): Promise<{ path: string; state: ModePathState }> {
  const uri = `/api/trade-modes/${encodePathSegment(strategy)}/${encodePathSegment(symbol)}/${encodePathSegment(direction)}/auto`;
  const { data } = await apiClient.post<{ path: string; state: ModePathState }>(uri, { enabled });
  return data;
}

export async function updateModeControlConfig(config: ModeControlConfig): Promise<ModeControlConfig> {
  const { data } = await apiClient.post<{ config: ModeControlConfig }>("/api/trade-modes/config", config);
  return data.config;
}

export interface AdaptiveEntryOverrideResponse {
  path: string;
  enabled: boolean | null;
  overrides: Record<string, boolean>;
}

export async function updateAdaptiveEntryOverride(
  strategy: string,
  symbol: string,
  direction: string,
  enabled: boolean | null
): Promise<AdaptiveEntryOverrideResponse> {
  const { data } = await apiClient.post<AdaptiveEntryOverrideResponse>("/api/strategy-control/adaptive-entry", {
    strategy,
    symbol,
    direction,
    enabled
  });
  return data;
}

export interface TemplateMeta {
  id: string;
  name: string;
  description?: string | null;
  tags: string[];
  schema_version?: string;
  created_at?: string;
  updated_at?: string;
}

export interface TemplateListResponse {
  items: TemplateMeta[];
}

export async function fetchTemplates(): Promise<TemplateListResponse> {
  const { data } = await apiClient.get<TemplateListResponse>("/api/templates");
  return data;
}

export interface CreateTemplatePayload {
  id?: string;
  name: string;
  description?: string | null;
  tags?: string[];
  payload: Record<string, unknown>;
}

export interface CreateTemplateResponse {
  id: string;
  name: string;
}

export async function createTemplate(payload: CreateTemplatePayload): Promise<CreateTemplateResponse> {
  const { data } = await apiClient.post<CreateTemplateResponse>("/api/templates", payload);
  return data;
}

// ---------------------------
// News trading subsystem
// ---------------------------

export interface NewsStatus {
  enabled: boolean;
  embedded?: boolean;
  symbols?: string[];
  pulse_interval?: number;
  state?: {
    heartbeat?: string | null;
    last_error?: string | null;
    last_pulse?: string | null;
    queue_depth?: number;
    processed_events?: number;
    updated_at?: string | null;
  };
}

export interface NewsFeedItem {
  event_id: string;
  headline: string;
  summary: string;
  url: string;
  impact: number;
  sentiment: number;
  urgency: number;
  confidence: number;
  tags: string[];
  symbols: string;
  published_at: string | null;
  created_at: string;
  signals: Array<NewsSignalItem>;
}

export interface NewsSignalItem {
  signal_id: string;
  event_id: string;
  rule_name: string;
  symbol: string;
  direction: string;
  confidence: number;
  horizon_minutes: number;
  size_usd: number;
  status: string;
  created_at: string;
}

export interface NewsPositionItem {
  position_id: string;
  symbol: string;
  direction: string;
  size: number;
  status: string;
  entry_price?: number | null;
  exit_price?: number | null;
  entered_at?: string | null;
  exited_at?: string | null;
  pnl?: number | null;
  notes?: string | null;
}

export async function fetchNewsStatus(): Promise<NewsStatus> {
  const { data } = await apiClient.get<NewsStatus>("/api/news/status");
  return data;
}

export async function triggerNewsScan(): Promise<{ status: string; result: Record<string, unknown> }>{
  const { data } = await apiClient.post<{ status: string; result: Record<string, unknown> }>("/api/news/scan", {});
  return data;
}

export async function fetchNewsFeed(limit = 40, symbol?: string): Promise<{ items: NewsFeedItem[] }>{
  const params: Record<string, unknown> = { limit };
  if (symbol) params.symbol = symbol;
  const { data } = await apiClient.get<{ items: NewsFeedItem[] }>("/api/news/feed", { params });
  return data;
}

export async function fetchNewsSignals(limit = 40, status?: string): Promise<{ items: NewsSignalItem[] }>{
  const params: Record<string, unknown> = { limit };
  if (status) params.status = status;
  const { data } = await apiClient.get<{ items: NewsSignalItem[] }>("/api/news/signals", { params });
  return data;
}

export async function fetchNewsPositions(limit = 40): Promise<{ items: NewsPositionItem[] }>{
  const { data } = await apiClient.get<{ items: NewsPositionItem[] }>("/api/news/positions", { params: { limit } });
  return data;
}

export async function fetchNewsSettings(): Promise<Record<string, unknown>> {
  const { data } = await apiClient.get<{ settings: Record<string, unknown> }>("/api/news/settings");
  return data.settings;
}

export interface TemplateDetailResponse {
  id: string;
  meta?: TemplateMeta;
  payload: Record<string, unknown>;
}

export async function fetchTemplate(templateId: string): Promise<TemplateDetailResponse> {
  const { data } = await apiClient.get<TemplateDetailResponse>(`/api/templates/${templateId}`);
  return data;
}

export async function deleteTemplate(templateId: string): Promise<void> {
  await apiClient.delete(`/api/templates/${templateId}`);
}

export interface RunMeta {
  id: string;
  template_id: string | null;
  template_name: string | null;
  started_at: string;
  ended_at: string | null;
  runtime_seconds: number | null;
  status: string;
  pnl_total: number | null;
  max_drawdown: number | null;
  sharpe_ratio: number | null;
  exposure_max: number | null;
  notes: string | null;
}

export interface StrategyStat {
  symbol: string;
  strategy: string;
  environment?: string;
  direction: string;
  pnl: number; // Legacy: net PnL (for backward compatibility)
  pnl_gross?: number;
  pnl_net?: number;
  fees?: number;
  trades: number;
  wins: number;
  win_rate: number;
  mean_return: number;
  pnl_per_trade: number;
  recent_5_pnl?: number; // PnL der letzten 5 Trades
}

export type RuntimeStrategyStat = StrategyStat;

export interface BacktestSummarySlice {
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
}

export interface BacktestSummaryPayload {
  summary: BacktestSummarySlice;
  by_symbol: BacktestSummarySlice[];
  by_strategy: BacktestSummarySlice[];
  by_side: BacktestSummarySlice[];
  by_strategy_side: BacktestSummarySlice[];
  by_symbol_side: BacktestSummarySlice[];
  by_symbol_strategy_side: BacktestSummarySlice[];
  by_day?: Array<BacktestSummarySlice & { day: string }>;
  summary_exchange_bound?: BacktestSummarySlice;
  mode_controller_summary_environment?: string;
  by_strategy_day?: Array<BacktestSummarySlice & { strategy: string; day: string }>;
  by_symbol_strategy_side_day?: Array<BacktestSummarySlice & { symbol: string; strategy: string; side: string; day: string }>;
  summary_normal?: BacktestSummarySlice;
  summary_with_mode_controller?: BacktestSummarySlice;
  by_symbol_all?: BacktestSummarySlice[];
  by_strategy_all?: BacktestSummarySlice[];
  by_side_all?: BacktestSummarySlice[];
  by_strategy_side_all?: BacktestSummarySlice[];
  by_symbol_side_all?: BacktestSummarySlice[];
  by_symbol_strategy_side_all?: BacktestSummarySlice[];
  by_day_all?: Array<BacktestSummarySlice & { day: string }>;
  by_strategy_day_all?: Array<BacktestSummarySlice & { strategy: string; day: string }>;
  by_symbol_strategy_side_day_all?: Array<BacktestSummarySlice & { symbol: string; strategy: string; side: string; day: string }>;
  by_hour?: Array<BacktestSummarySlice & { hour: number }>;
  by_strategy_hour?: Array<BacktestSummarySlice & { strategy: string; hour: number }>;
}

export interface BacktestSnapshot {
  run_id: string;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  summary?: BacktestSummaryPayload | null;
  error?: string | null;
}

export interface ManualBacktestMeta {
  id: string;
  template_id: string | null;
  template_name: string | null;
  start_date: string;
  end_date: string;
  status: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  notes: string | null;
  log_path: string | null;
  config_path: string | null;
  strategy_filters?: string[] | null;
  start_hour?: number | null;
  end_hour?: number | null;
  hour_timezone?: string | null;
  flip_all_branches?: boolean | null;
  summary?: BacktestSummaryPayload | null;
  error?: string | null;
  settings_snapshot?: Record<string, unknown> | null;
  sample_ratio?: number | null;
  data_coverage_pct?: number | null;
  backtest_type?: "manual" | "auto" | null;
  run_id?: string | null;
  window_start?: string | null;
  window_end?: string | null;
}

export interface RunListResponse {
  items: RunMeta[];
  total: number;
}

export async function fetchRuns(limit = 50, offset = 0): Promise<RunListResponse> {
  const { data } = await apiClient.get<RunListResponse>("/api/runs", { params: { limit, offset } });
  return data;
}

export interface RunDetailResponse {
  run: RunMeta;
  stats: StrategyStat[];
  summary: { prompt_hash: string; summary: string; model: string; created_at: string } | null;
  settings: Record<string, unknown> | null;
  backtest: BacktestSnapshot | null;
}

export async function fetchRun(runId: string): Promise<RunDetailResponse> {
  const { data } = await apiClient.get<RunDetailResponse>(`/api/runs/${runId}`);
  return data;
}

export interface TradeOutcomeStrategySummary {
  strategy: string;
  analyzed_trades: number;
  realized_pnl_avg: number | null;
  delta_pnl_1h_avg: number | null;
  delta_pnl_3h_avg: number | null;
  delta_pnl_6h_avg: number | null;
  delta_pnl_12h_avg: number | null;
  best_extra_pnl_avg: number | null;
  worst_extra_pnl_avg: number | null;
  time_to_best_minutes_avg: number | null;
  time_to_worst_minutes_avg: number | null;
  positive_delta_12h_count: number;
  window_complete_count: number;
  positive_delta_12h_rate: number;
  coverage_pct_avg: number | null;
}

export interface TradeOutcomeTradeRow {
  trade_id: number;
  symbol: string;
  strategy: string;
  environment: string;
  direction: string;
  timestamp: string;
  exit_timestamp: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  realized_pnl: number;
  exit_reason: string | null;
  exit_source: string | null;
  window_complete: number;
  coverage_pct: number | null;
  delta_pnl_1h: number | null;
  delta_pnl_3h: number | null;
  delta_pnl_6h: number | null;
  delta_pnl_12h: number | null;
  best_pnl_after_exit: number | null;
  worst_pnl_after_exit: number | null;
  time_to_best_minutes: number | null;
  time_to_worst_minutes: number | null;
  bars_available: number | null;
  bars_expected: number | null;
}

export interface TradeOutcomeCurvePoint {
  timestamp: string;
  price: number;
  pnl_from_entry: number;
  delta_vs_exit: number;
  minutes_after_exit: number;
}

export interface TradeOutcomePricePathPoint {
  timestamp: string;
  price: number;
  phase: string;
  marker: string | null;
  minutes_from_entry: number;
  minutes_from_exit: number;
  pnl_from_entry: number;
  delta_vs_exit: number;
}

export interface TradeOutcomeTradeDetail extends TradeOutcomeTradeRow {
  window_hours: number;
  resolution: string;
  available_until: string | null;
  bars_available: number;
  bars_expected: number;
  best_price_after_exit: number | null;
  worst_price_after_exit: number | null;
  curve: TradeOutcomeCurvePoint[];
  price_path: TradeOutcomePricePathPoint[];
}

export async function fetchTradeOutcomeSummary(
  runId: string,
  includeSpecialExits = false,
  includeStrategySim = false
): Promise<TradeOutcomeStrategySummary[]> {
  const { data } = await apiClient.get<{ run_id: string; items: TradeOutcomeStrategySummary[] }>(
    `/api/runs/${runId}/trade-outcomes/summary`,
    { params: { include_special_exits: includeSpecialExits, include_strategy_sim: includeStrategySim } }
  );
  return data.items;
}

export async function fetchTradeOutcomesForStrategy(
  runId: string,
  strategy: string,
  limit = 100,
  includeSpecialExits = false,
  includeStrategySim = false
): Promise<TradeOutcomeTradeRow[]> {
  const { data } = await apiClient.get<{ run_id: string; strategy: string; items: TradeOutcomeTradeRow[] }>(
    `/api/runs/${runId}/trade-outcomes/trades`,
    { params: { strategy, limit, include_special_exits: includeSpecialExits, include_strategy_sim: includeStrategySim } }
  );
  return data.items;
}

export async function fetchTradeOutcomeDetail(runId: string, tradeId: number): Promise<TradeOutcomeTradeDetail> {
  const { data } = await apiClient.get<{ run_id: string; trade: TradeOutcomeTradeDetail }>(
    `/api/runs/${runId}/trade-outcomes/trades/${tradeId}`
  );
  return data.trade;
}

export interface BacktestListResponse {
  items: ManualBacktestMeta[];
  active: Record<string, string>;
}

export async function listBacktests(limit = 50, offset = 0): Promise<BacktestListResponse> {
  const { data } = await apiClient.get<BacktestListResponse>("/api/backtests", { params: { limit, offset } });
  return data;
}

// Alias für konsistente Namensgebung im Frontend
export const fetchBacktests = listBacktests;

export interface BacktestCompareMeta {
  id: string;
  template_name: string | null;
  template_id: string | null;
  start_date: string;
  end_date: string;
  completed_at: string | null;
  created_at: string;
  notes: string | null;
}

export interface BacktestCompareMetric {
  trades: number;
  wins: number;
  win_rate: number;
  pnl: number;
  fees: number;
  pnl_per_trade: number;
  avg_notional?: number | null;
  pnl_per_trade_pct?: number | null;
}

export interface BacktestCompareBranch {
  strategy: string;
  alias: string;
  symbol: string;
  direction: string;
  metrics: BacktestCompareMetric[];
}

export interface BacktestCompareResponse {
  backtests: BacktestCompareMeta[];
  branches: BacktestCompareBranch[];
  mode: string;
  missing_ids?: string[];
  skipped_ids?: string[];
}

export async function fetchBacktestCompare(
  limit = 5,
  mode = "normal",
  backtestIds?: string[]
): Promise<BacktestCompareResponse> {
  const params: Record<string, string | number> = { limit, mode };
  if (backtestIds && backtestIds.length > 0) {
    params.backtest_ids = backtestIds.join(",");
  }
  const { data } = await apiClient.get<BacktestCompareResponse>("/api/backtests/compare", {
    params,
  });
  return data;
}

export interface StartBacktestRequest {
  template_id?: string | null;
  start_date: string;
  end_date: string;
  notes?: string | null;
  settings_override?: Record<string, unknown>;
  data_source?: string | null;
  strategy_names?: string[];
  simulate_mode_controller?: boolean;
  max_parallel_cores?: number;
  start_hour?: number | null;
  end_hour?: number | null;
  hour_timezone?: string | null;
  flip_all_branches?: boolean;
  use_adaptive_entry?: boolean;
  cache_only?: boolean;
  sample_ratio?: number;
  global_entry_tightness?: number | null;
}

export interface StartBacktestResponse {
  backtest: ManualBacktestMeta;
}

export async function startBacktest(payload: StartBacktestRequest): Promise<StartBacktestResponse> {
  const { data } = await apiClient.post<StartBacktestResponse>("/api/backtests/start", payload);
  return data;
}

export interface CancelBacktestResponse {
  status: string;
  backtest: ManualBacktestMeta | null;
}

export async function cancelBacktest(backtestId: string): Promise<CancelBacktestResponse> {
  const { data } = await apiClient.post<CancelBacktestResponse>(`/api/backtest/${backtestId}/cancel`);
  return data;
}

export interface BacktestDetailResponse {
  backtest: ManualBacktestMeta;
}

export async function fetchBacktest(backtestId: string): Promise<BacktestDetailResponse> {
  const { data } = await apiClient.get<BacktestDetailResponse>(`/api/backtests/${backtestId}`);
  return data;
}

export interface BacktestLogResponse {
  path: string;
  path_absolute: string;
  lines: string[];
}

export async function fetchBacktestLog(backtestId: string, lines = 200): Promise<BacktestLogResponse> {
  const { data } = await apiClient.get<BacktestLogResponse>(`/api/backtests/${backtestId}/log`, { params: { lines } });
  return data;
}

export async function deleteRun(runId: string): Promise<void> {
  await apiClient.delete(`/api/runs/${runId}`);
}

export interface LogFile {
  name: string;
  path: string;
  size: number;
  modified_at: number;
}

export interface LogListResponse {
  items: LogFile[];
}

export async function fetchLogs(): Promise<LogListResponse> {
  const { data } = await apiClient.get<LogListResponse>("/api/logs/list");
  return data;
}

export interface LogTailResponse {
  path: string;
  lines: string[];
}

export async function tailLog(path: string, lines = 200): Promise<LogTailResponse> {
  const { data } = await apiClient.get<LogTailResponse>("/api/logs/tail", { params: { path, lines } });
  return data;
}

export interface SettingsSchemaResponse {
  schema: Record<string, unknown>;
  tooltips: Record<string, string>;
}

export interface MultiTestMeta {
  id: string;
  template_id?: string | null;
  template_name?: string | null;
  strategy_name?: string | null;
  start_date: string;
  end_date: string;
  status: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  settings_snapshot?: Record<string, unknown> | null;
  params: Record<string, unknown>;
  output_dir?: string | null;
  output_dir_absolute?: string | null;
  log_path?: string | null;
  log_path_absolute?: string | null;
  sample_ratio?: number | null;
  max_combos?: number | null;
  notes?: string | null;
  config_path?: string | null;
  config_path_absolute?: string | null;
  error?: string | null;
  total_combos?: number | null;
  completed_combos?: number | null;
  remaining_combos?: number | null;
}

export interface MultiTestResult {
  multitest_id: string;
  strategy_name: string;
  combo_id: string;
  leaderboard_rank: number;
  pnl: number;
  trades: number;
  winrate: number;
  pnl_long: number;
  winrate_long: number;
  pnl_short: number;
  winrate_short: number;
  pnl_per_trade?: number | null;
  pnl_per_trade_long?: number | null;
  pnl_per_trade_short?: number | null;
  params: Record<string, unknown>;
}

export interface MultiTestListResponse {
  items: MultiTestMeta[];
  active: MultiTestMeta | null;
}

export interface MultiTestDetailResponse {
  meta: MultiTestMeta;
  results: MultiTestResult[];
}

export interface MultiTestStartPayload {
  template_id?: string | null;
  strategy_name?: string | null;
  start_date: string;
  end_date: string;
  sample_ratio?: number | null;
  max_combos?: number | null;
  combo_timeout?: number | null;
  instrument_timeout?: number | null;
  notes?: string | null;
  settings_override?: Record<string, unknown> | null;
  extra_cli_args?: Record<string, unknown> | null;
}

export interface MultiTestLogResponse {
  path: string;
  path_absolute: string;
  lines: string[];
}

export interface CancelMultiTestResponse {
  status: string;
  multitest?: MultiTestMeta | null;
}

export interface MultiTestConfigResponse {
  config: Record<string, unknown>;
}

export interface MultiTestPreset {
  id: string;
  name: string;
  description?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface MultiTestPresetListResponse {
  items: MultiTestPreset[];
}

export interface MultiTestPresetUpsertResponse {
  preset: MultiTestPreset;
}

export interface MultiTestPresetRequest {
  id?: string;
  name: string;
  description?: string | null;
  payload: Record<string, unknown>;
}

export interface MultiTestEstimateResponse {
  estimate: {
    total: number | null;
    strategies: Array<{
      name: string;
      alias?: string | null;
      base: number;
      effective: number;
      truncated: boolean;
    }>;
  };
}

export interface MultiTestParamGrid {
  global: Record<string, unknown[]>;
  strategies: Record<string, Record<string, unknown[]>>;
}

export async function listMultiTests(limit = 50, offset = 0): Promise<MultiTestListResponse> {
  const { data } = await apiClient.get<MultiTestListResponse>("/api/multitest", { params: { limit, offset } });
  return data;
}

export async function fetchMultiTest(multitestId: string): Promise<MultiTestDetailResponse> {
  const { data } = await apiClient.get<MultiTestDetailResponse>(`/api/multitest/${multitestId}`);
  return data;
}

export async function startMultiTest(payload: MultiTestStartPayload): Promise<{ multitest: MultiTestMeta }> {
  const { data } = await apiClient.post<{ multitest: MultiTestMeta }>("/api/multitest/start", payload);
  return data;
}

export async function cancelMultiTest(multitestId: string): Promise<CancelMultiTestResponse> {
  const { data } = await apiClient.post<CancelMultiTestResponse>(`/api/multitest/${multitestId}/cancel`);
  return data;
}

export async function fetchMultiTestLog(multitestId: string, lines = 200): Promise<MultiTestLogResponse> {
  const { data } = await apiClient.get<MultiTestLogResponse>(`/api/multitest/${multitestId}/log`, { params: { lines } });
  return data;
}

export async function fetchMultiTestConfig(multitestId: string): Promise<MultiTestConfigResponse> {
  const { data } = await apiClient.get<MultiTestConfigResponse>(`/api/multitest/${multitestId}/config`);
  return data;
}

export async function fetchMultiTestPresets(): Promise<MultiTestPresetListResponse> {
  const { data } = await apiClient.get<MultiTestPresetListResponse>('/api/multitest/presets');
  return data;
}

export async function upsertMultiTestPreset(payload: MultiTestPresetRequest): Promise<MultiTestPresetUpsertResponse> {
  const { data } = await apiClient.post<MultiTestPresetUpsertResponse>('/api/multitest/presets', payload);
  return data;
}

export async function deleteMultiTestPreset(presetId: string): Promise<void> {
  await apiClient.delete(`/api/multitest/presets/${presetId}`);
}

export async function estimateMultiTest(payload: MultiTestStartPayload): Promise<MultiTestEstimateResponse> {
  const { data } = await apiClient.post<MultiTestEstimateResponse>('/api/multitest/estimate', payload);
  return data;
}

export async function fetchMultiTestParamGrid(): Promise<MultiTestParamGrid> {
  const { data } = await apiClient.get<MultiTestParamGrid>('/api/multitest/param-grid');
  return data;
}

export async function updateMultiTestParamGrid(payload: MultiTestParamGrid): Promise<MultiTestParamGrid> {
  const { data } = await apiClient.post<MultiTestParamGrid>('/api/multitest/param-grid', payload);
  return data;
}

export async function resetMultiTestParamGrid(): Promise<MultiTestParamGrid> {
  const { data } = await apiClient.delete<MultiTestParamGrid>('/api/multitest/param-grid');
  return data;
}

export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessage {
  role: ChatRole;
  content: string;
  created_at?: string;
}

export interface ChatRequestPayload {
  message: string;
  session_id?: string;
  history?: ChatMessage[];
}

export interface ChatResponsePayload {
  reply: string;
  session_id: string;
  finish_reason?: string;
  usage?: Record<string, unknown>;
}

export async function sendChatMessage(payload: ChatRequestPayload): Promise<ChatResponsePayload> {
  const { data } = await apiClient.post<ChatResponsePayload>("/api/chat", payload, {
    timeout: 5 * 60 * 1000
  });
  return data;
}

export interface ChatSessionSummary {
  session_id: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview?: string;
}

export interface ChatSessionDetail {
  session_id: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
}

export async function fetchChatSessions(): Promise<ChatSessionSummary[]> {
  const { data } = await apiClient.get<{ items: ChatSessionSummary[] }>("/api/chat/sessions");
  return data.items;
}

export async function fetchChatSession(sessionId: string): Promise<ChatSessionDetail> {
  const { data } = await apiClient.get<ChatSessionDetail>(`/api/chat/sessions/${sessionId}`);
  return data;
}

export interface ChatSessionStatus {
  session_id: string;
  status: string;
  is_running: boolean;
  last_error?: string;
  message_count: number;
  updated_at: string;
}

export async function fetchChatSessionStatus(sessionId: string): Promise<ChatSessionStatus> {
  const { data } = await apiClient.get<ChatSessionStatus>(`/api/chat/sessions/${sessionId}/status`);
  return data;
}

export async function fetchSettingsSchema(): Promise<SettingsSchemaResponse> {
  const { data } = await apiClient.get<SettingsSchemaResponse>("/api/settings/schema");
  return data;
}

export async function fetchVisibleSettingsFields(): Promise<string[]> {
  const { data } = await apiClient.get<{ hidden: string[] }>("/api/settings/visible-fields");
  return data.hidden ?? [];
}

export async function updateVisibleSettingsFields(hidden: string[]): Promise<void> {
  await apiClient.post("/api/settings/visible-fields", { hidden });
}

export interface CurrentRunInfo {
  run_id: string | null;
  run_dir: string;
  db_path: string;
}

export interface CurrentRunResponse {
  run: CurrentRunInfo | null;
  backtest: BacktestSnapshot | null;
}

export async function fetchCurrentRun(): Promise<CurrentRunResponse> {
  const { data } = await apiClient.get<CurrentRunResponse>("/api/run/current");
  return data;
}

export interface CurrentTradesResponse {
  items: Array<Record<string, unknown>>;
  aggregated_pnl: number;
  total?: number;
}

export async function fetchCurrentTrades(
  limit = 100,
  afterRowid?: number,
  environment?: string,
  beforeRowid?: number
): Promise<CurrentTradesResponse> {
  const params: Record<string, unknown> = { limit };
  if (afterRowid != null) params.after_rowid = afterRowid;
  if (beforeRowid != null) params.before_rowid = beforeRowid;
  if (environment) {
    params.environment = environment;
  }
  const { data } = await apiClient.get<CurrentTradesResponse>("/api/run/current/trades", { params });
  return data;
}

export interface CurrentMetricsResponse {
  items: Array<Record<string, unknown>>;
  total?: number;
}

export async function fetchCurrentMetrics(limit = 100, afterRowid?: number): Promise<CurrentMetricsResponse> {
  const { data } = await apiClient.get<CurrentMetricsResponse>("/api/run/current/metrics", {
    params: { limit, after_rowid: afterRowid }
  });
  return data;
}

export async function fetchCurrentStrategyStats(): Promise<StrategyStat[]> {
  const { data } = await apiClient.get<{ items: StrategyStat[] }>("/api/run/current/strategy-stats");
  return data.items;
}

export interface StrategyHistoryEntry {
  run: RunMeta;
  stats: StrategyStat[];
}

export interface StrategyHistoryResponse {
  items: StrategyHistoryEntry[];
  total: number;
}

export async function fetchStrategyHistory(limit = 10, offset = 0): Promise<StrategyHistoryResponse> {
  const { data } = await apiClient.get<StrategyHistoryResponse>("/api/analytics/strategy-history", {
    params: { limit, offset }
  });
  return data;
}

// Strategy Control API

export interface StrategyBranch {
  strategy: string;
  variation: string;
  direction: string;
  symbol: string;
  pipeline_index: number;
  params: Record<string, unknown>;
  current_params: Record<string, unknown> | null;
  flip_mode: string;
}

export interface StrategyBranchesResponse {
  branches: StrategyBranch[];
}

export interface StrategyBranchDetail {
  strategy: string;
  variation: string;
  direction: string;
  symbol: string;
  params: Record<string, unknown>;
  current_params: Record<string, unknown> | null;
}

export interface StrategyParameterInfo {
  description: string;
  default: unknown;
}

export interface StrategyMetadata {
  description: string;
  parameters: Record<string, StrategyParameterInfo>;
}

export interface StrategyMetadataResponse {
  strategies: Record<string, StrategyMetadata>;
}

export interface DisabledBranchResponse {
  disabled_branches: string[];
}

export interface DisabledBranchRequest {
  strategy: string;
  symbol: string;
  direction: string;
  enabled: boolean;
}

export interface BacktestTrade {
  timestamp: string;
  price: number;
  entry_price: number;
  exit_price: number;
  direction: string;
  pnl: number;
}

export interface BacktestTradesResponse {
  trades: BacktestTrade[];
  backtest_id: string | null;
  backtest_start?: string;
  backtest_end?: string;
}

export async function fetchStrategyBranches(): Promise<StrategyBranchesResponse> {
  const { data } = await apiClient.get<StrategyBranchesResponse>("/api/strategy-control/branches");
  return data;
}

export async function fetchStrategyBranch(
  strategy: string,
  variation: string,
  direction: string,
  symbol: string
): Promise<StrategyBranchDetail> {
  const { data } = await apiClient.get<StrategyBranchDetail>(
    `/api/strategy-control/branch/${encodeURIComponent(strategy)}/${encodeURIComponent(variation)}/${encodeURIComponent(direction)}/${encodeURIComponent(symbol)}`
  );
  return data;
}

export async function updateStrategyBranch(
  strategy: string,
  variation: string,
  direction: string,
  symbol: string,
  params: Record<string, unknown>
): Promise<StrategyBranchDetail> {
  const { data } = await apiClient.post<StrategyBranchDetail>(
    `/api/strategy-control/branch/${encodeURIComponent(strategy)}/${encodeURIComponent(variation)}/${encodeURIComponent(direction)}/${encodeURIComponent(symbol)}`,
    { params: params }
  );
  return data;
}

export async function fetchStrategyMetadata(): Promise<StrategyMetadataResponse> {
  const { data } = await apiClient.get<StrategyMetadataResponse>("/api/strategy-control/strategies");
  return data;
}

export async function fetchBacktestTrades(strategy: string, symbol: string): Promise<BacktestTradesResponse> {
  const { data } = await apiClient.get<BacktestTradesResponse>(
    `/api/strategy-control/backtest-trades/${encodeURIComponent(strategy)}/${encodeURIComponent(symbol)}`
  );
  return data;
}

export interface TradeReplayItem {
  id: number;
  trade_id: number;
  run_id?: string | null;
  strategy?: string | null;
  symbol?: string | null;
  direction?: string | null;
  mode?: string | null;
  status?: string | null;
  attempts?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  next_attempt_at?: string | null;
  settings_path?: string | null;
  result_json?: string | null;
  result?: Record<string, any> | null;
  error?: string | null;
  rowid?: number | null;
}

export interface TradeReplayResponse {
  items: TradeReplayItem[];
  total: number;
}

export interface ShadowMirrorItem {
  id: number;
  rowid?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  run_id?: string | null;
  status?: string | null;
  live_trade_id?: number | null;
  shadow_trade_id?: number | null;
  strategy?: string | null;
  symbol?: string | null;
  direction?: string | null;
  live_entry_timestamp?: string | null;
  live_exit_timestamp?: string | null;
  live_entry_price?: number | null;
  live_exit_price?: number | null;
  live_pnl?: number | null;
  live_exit_reason?: string | null;
  shadow_entry_timestamp?: string | null;
  shadow_exit_timestamp?: string | null;
  shadow_entry_price?: number | null;
  shadow_exit_price?: number | null;
  shadow_pnl?: number | null;
  shadow_exit_reason?: string | null;
  entry_error?: string | null;
  close_error?: string | null;
  notes?: string | null;
}

export interface ShadowMirrorResponse {
  items: ShadowMirrorItem[];
  total: number;
}

export interface ShadowMirrorConfig {
  enabled: boolean;
  close_with_live: boolean;
  max_open: number;
}

export async function fetchTradeReplays(params?: {
  limit?: number;
  status?: string;
  mode?: string;
  strategy?: string;
  symbol?: string;
}): Promise<TradeReplayResponse> {
  const { data } = await apiClient.get<TradeReplayResponse>("/api/run/current/trade-replays", { params });
  return data;
}

export async function fetchShadowMirrors(params?: {
  limit?: number;
  status?: string;
  strategy?: string;
  symbol?: string;
}): Promise<ShadowMirrorResponse> {
  const { data } = await apiClient.get<ShadowMirrorResponse>("/api/run/current/shadow-mirrors", { params });
  return data;
}

export async function fetchShadowMirrorConfig(): Promise<ShadowMirrorConfig> {
  const { data } = await apiClient.get<ShadowMirrorConfig>("/api/settings/shadow-mirror");
  return data;
}

export async function updateShadowMirrorConfig(payload: Partial<ShadowMirrorConfig> & { enabled: boolean }): Promise<ShadowMirrorConfig> {
  const { data } = await apiClient.post<ShadowMirrorConfig>("/api/settings/shadow-mirror", payload);
  return data;
}

export async function updateDisabledBranch(
  strategy: string,
  symbol: string,
  direction: string,
  enabled: boolean
): Promise<DisabledBranchResponse> {
  const { data } = await apiClient.post<DisabledBranchResponse>("/api/strategy-control/disabled-branches", {
    strategy,
    symbol,
    direction,
    enabled
  } as DisabledBranchRequest);
  return data;
}

// Auto-Optimizer API
export interface AutoOptimizerStatus {
  running: boolean;
  current_test: {
    strategy: string;
    symbol?: string;
    direction?: string;
    params: string[];
    test_id: string;
    combos: number;
    mode?: string;
    llm_reasoning?: string;
    llm_model?: string;
    feedback_round?: number;
  } | null;
  last_update: string | null;
  debug?: {
    other_tests_running?: boolean;
    available_cores?: number;
    available_branches_count?: number;
    next_branch?: string;
    optimizer_internal_running?: boolean;
    branches_error?: string;
    debug_error?: string;
    paused_reason?: string;
    consecutive_timeouts?: number;
    timeout_warning?: string;
    last_cycle_debug?: {
      error?: string;
      available_branches?: number;
      timestamp?: string;
    };
  };
  last_test_id: string | null;
  tests_completed: number;
  tests_accepted: number;
  focused_strategies: string[];
}

export interface AutoOptimizerTestResult {
  test_id: string;
  strategy_name: string;
  tested_params: Record<string, unknown>;
  param_values: Record<string, unknown>;
  pnl: number;
  trades: number;
  pnl_per_trade: number;
  winrate: number;
  pnl_long: number;
  winrate_long: number;
  trades_long: number;
  pnl_short: number;
  winrate_short: number;
  trades_short: number;
  timestamp: string;
  accepted: boolean;
  test_date_range: string;
  // LLM-Metadaten (optional)
  llm_reasoning?: string | null;
  llm_model?: string | null;
  feedback_round?: number | null;
  optimization_mode?: string | null;
  score?: number | null;
  max_drawdown?: number | null;
  profit_factor?: number | null;
}

export interface AutoOptimizerResultsResponse {
  results: AutoOptimizerTestResult[];
  total_count?: number;
  limit?: number;
  offset?: number;
}

export interface AutoOptimizerHistoryResponse {
  history: AutoOptimizerTestResult[];
  total_count?: number;
  limit?: number;
  offset?: number;
}

export interface AutoOptimizerBestParamsResponse {
  best_params: Record<string, Record<string, unknown>>;
  best_params_by_mode?: Record<string, Record<string, Record<string, unknown>>>;
}

export async function fetchAutoOptimizerStatus(): Promise<AutoOptimizerStatus> {
  const { data } = await apiClient.get<AutoOptimizerStatus>("/api/auto-optimizer/status");
  return data;
}

export async function fetchAutoOptimizerResults(
  strategyName?: string,
  limit = 10,
  offset = 0
): Promise<AutoOptimizerResultsResponse> {
  const params = new URLSearchParams();
  if (strategyName) params.append("strategy_name", strategyName);
  params.append("limit", limit.toString());
  params.append("offset", offset.toString());
  const { data } = await apiClient.get<AutoOptimizerResultsResponse>(`/api/auto-optimizer/results?${params}`);
  return data;
}

export async function fetchAutoOptimizerHistory(
  limit = 50,
  offset = 0
): Promise<AutoOptimizerHistoryResponse> {
  const params = new URLSearchParams();
  params.append("limit", limit.toString());
  params.append("offset", offset.toString());
  const { data } = await apiClient.get<AutoOptimizerHistoryResponse>(`/api/auto-optimizer/history?${params}`);
  return data;
}

export async function fetchAutoOptimizerBestParams(): Promise<AutoOptimizerBestParamsResponse> {
  const { data } = await apiClient.get<AutoOptimizerBestParamsResponse>("/api/auto-optimizer/best-params");
  return data;
}

export interface BranchOverview {
  strategy_name: string;
  symbol: string | null;
  direction: string | null;
  params: Record<string, unknown>;
  metrics: {
    pnl: number;
    trades: number;
    pnl_per_trade: number;
    winrate: number;
    test_id: string;
    score: number | null;
    max_drawdown: number | null;
    profit_factor: number | null;
  };
  last_update: string;
  test_date_range: string;
  llm_reasoning: string | null;
}

export interface BranchesOverviewResponse {
  branches: BranchOverview[];
  count: number;
}

export async function fetchBranchesOverview(): Promise<BranchesOverviewResponse> {
  const { data } = await apiClient.get<BranchesOverviewResponse>("/api/auto-optimizer/branches-overview");
  return data;
}

export async function startAutoOptimizer(): Promise<{ status: string; message: string }> {
  const { data } = await apiClient.post<{ status: string; message: string }>("/api/auto-optimizer/start");
  return data;
}

export async function stopAutoOptimizer(): Promise<{ status: string; message: string }> {
  const { data } = await apiClient.post<{ status: string; message: string }>("/api/auto-optimizer/stop");
  return data;
}

export interface AutoOptimizerLogResponse {
  lines: string[];
  total_lines: number;
  showing: number;
  error?: string;
}

export async function fetchAutoOptimizerLog(lines = 200): Promise<AutoOptimizerLogResponse> {
  const { data } = await apiClient.get<AutoOptimizerLogResponse>(`/api/auto-optimizer/log?lines=${lines}`);
  return data;
}

export interface FocusedStrategiesResponse {
  focused_strategies: string[];
  available_strategies: string[];
  strategies_with_params: string[];
}

export async function fetchFocusedStrategies(): Promise<FocusedStrategiesResponse> {
  const { data } = await apiClient.get<FocusedStrategiesResponse>("/api/auto-optimizer/focused-strategies");
  return data;
}

export async function setFocusedStrategies(strategies: string[]): Promise<{ status: string; message: string; focused_strategies: string[] }> {
  const { data } = await apiClient.post<{ status: string; message: string; focused_strategies: string[] }>(
    "/api/auto-optimizer/focused-strategies",
    { strategies }
  );
  return data;
}

export interface LLMDecision {
  test_id: string;
  strategy_name: string;
  timestamp: string;
  param_values: Record<string, unknown>;
  llm_reasoning: string | null;
  llm_model: string | null;
  feedback_round: number | null;
  optimization_mode: string | null;
  score: number | null;
  pnl_per_trade: number;
  trades: number;
  winrate: number;
  accepted: boolean;
}

export interface LLMDecisionsResponse {
  decisions: LLMDecision[];
}

export async function fetchLLMDecisions(
  strategyName?: string,
  limit = 50
): Promise<LLMDecisionsResponse> {
  const params = new URLSearchParams();
  if (strategyName) params.append("strategy_name", strategyName);
  params.append("limit", limit.toString());
  const { data } = await apiClient.get<LLMDecisionsResponse>(`/api/auto-optimizer/llm-decisions?${params}`);
  return data;
}

export interface ScoreTimelinePoint {
  timestamp: string;
  strategy_name: string;
  score: number | null;
  pnl_per_trade: number;
  trades: number;
  winrate: number;
  accepted: boolean;
  optimization_mode: string | null;
}

export interface ScoreTimelineResponse {
  timeline: ScoreTimelinePoint[];
}

export async function fetchScoreTimeline(
  strategyName?: string,
  limit = 100
): Promise<ScoreTimelineResponse> {
  const params = new URLSearchParams();
  if (strategyName) params.append("strategy_name", strategyName);
  params.append("limit", limit.toString());
  const { data } = await apiClient.get<ScoreTimelineResponse>(`/api/auto-optimizer/score-timeline?${params}`);
  return data;
}

export interface ParameterVariation {
  values: number[];
  min: number | null;
  max: number | null;
  avg: number | null;
  count: number;
}

export interface ParameterVariationsResponse {
  variations: Record<string, ParameterVariation>;
}

export async function fetchParameterVariations(
  strategyName?: string
): Promise<ParameterVariationsResponse> {
  const params = new URLSearchParams();
  if (strategyName) params.append("strategy_name", strategyName);
  const { data } = await apiClient.get<ParameterVariationsResponse>(`/api/auto-optimizer/parameter-variations?${params}`);
  return data;
}

export interface BaselineComparison {
  baseline: {
    strategy_name: string;
    param_values: Record<string, unknown>;
    pnl_per_trade: number;
    trades: number;
    winrate: number;
    score: number | null;
    max_drawdown: number | null;
    profit_factor: number | null;
  } | null;
  best_optimized: {
    strategy_name: string;
    param_values: Record<string, unknown>;
    pnl_per_trade: number;
    trades: number;
    winrate: number;
    score: number | null;
    max_drawdown: number | null;
    profit_factor: number | null;
    optimization_mode: string | null;
  } | null;
  improvement: {
    score_improvement_pct: number;
    pnl_per_trade_improvement: number;
    trades_improvement: number;
    winrate_improvement: number;
  } | null;
}

export interface BaselineComparisonResponse {
  baseline: BaselineComparison["baseline"];
  best_optimized: BaselineComparison["best_optimized"];
  improvement: BaselineComparison["improvement"];
}

export async function fetchBaselineComparison(
  strategyName?: string
): Promise<BaselineComparisonResponse> {
  const params = new URLSearchParams();
  if (strategyName) params.append("strategy_name", strategyName);
  const { data } = await apiClient.get<BaselineComparisonResponse>(`/api/auto-optimizer/baseline-comparison?${params}`);
  return data;
}

export interface CpuStats {
  system: {
    cpu_percent: number;
    load_avg: [number, number, number];
    cpu_count: number;
  };
  processes: Array<{
    pid: number;
    name: string;
    cmdline: string;
    cpu_percent: number;
    memory_percent: number;
    type: "backtest" | "multitest" | "other";
    thread_id?: number;
    thread_name?: string;
    cpu_percent_raw?: number | null;
    cores_used?: number | null;
    worker_count?: number | null;
  }>;
  error?: string;
}

export async function fetchCpuStats(): Promise<CpuStats> {
  const { data } = await apiClient.get<CpuStats>("/api/system/cpu-stats");
  return data;
}

// History Data Collector API
export interface HistoryCollectorStatus {
  running: boolean;
  last_activity: string | null;
  last_error: string | null;
  stats: {
    gaps_filled: number;
    new_data_fetched: number;
    errors: number;
  };
  config: HistoryCollectorConfig;
}

export interface HistoryCollectorConfig {
  enabled: boolean;
  start_date: string;
  intervals: string[];
  symbols: string[];
  fetch_interval_seconds: number;
  data_source: string;
  max_gap_hours: number;
}

export interface HistoryCollectorLogsResponse {
  logs: string[];
  count: number;
}

export async function fetchHistoryCollectorStatus(): Promise<HistoryCollectorStatus> {
  const { data } = await apiClient.get<HistoryCollectorStatus>("/api/history-collector/status");
  return data;
}

export async function fetchHistoryCollectorLogs(limit = 100): Promise<HistoryCollectorLogsResponse> {
  const { data } = await apiClient.get<HistoryCollectorLogsResponse>("/api/history-collector/logs", {
    params: { limit },
  });
  return data;
}

export async function fetchHistoryCollectorConfig(): Promise<{ config: HistoryCollectorConfig }> {
  const { data } = await apiClient.get<{ config: HistoryCollectorConfig }>("/api/history-collector/config");
  return data;
}

export async function setHistoryCollectorConfig(
  config: Partial<HistoryCollectorConfig>
): Promise<{ status: string; config: HistoryCollectorConfig }> {
  const { data } = await apiClient.post<{ status: string; config: HistoryCollectorConfig }>(
    "/api/history-collector/config",
    config
  );
  return data;
}

export async function startHistoryCollector(): Promise<{ status: string; message: string }> {
  const { data } = await apiClient.post<{ status: string; message: string }>("/api/history-collector/start");
  return data;
}

export async function stopHistoryCollector(): Promise<{ status: string; message: string }> {
  const { data } = await apiClient.post<{ status: string; message: string }>("/api/history-collector/stop");
  return data;
}

export interface PriceHistoryPoint {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PriceHistoryResponse {
  symbol: string;
  data: PriceHistoryPoint[];
  interval: string;
  start: string;
  end: string;
  count: number;
}

export async function fetchPriceHistory(
  symbol: string = "BTCUSDT",
  months: number = 3,
  interval?: string,
  span?: string,
  maxPoints?: number
): Promise<PriceHistoryResponse> {
  const { data } = await apiClient.get<PriceHistoryResponse>("/api/price-history", {
    params: { symbol, months, interval, span, max_points: maxPoints }
  });
  return data;
}

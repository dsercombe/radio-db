import { useCallback, useEffect, useMemo, useState } from "react";
import {
    fetchDashboard,
    fetchBacktests,
    fetchCurrentTrades,
    type BotStatus,
    type StrategyStat,
    type BacktestSnapshot,
    type BacktestThreadStatus,
    type SettingsResponse,
    type ModeStateSnapshot,
    type BacktestSummaryPayload,
    type BacktestSummarySlice,
} from "../api/client";
import { PriceChart } from "../components/PriceChart";
import { PriceChartCanvas } from "../components/PriceChartCanvas";
import { useVisibilityPolling } from "../hooks/useVisibilityPolling";

const PNL_PER_TRADE_THRESHOLD = 0.001; // 0.1%
const LIVE_TRADES_PAGE_SIZE = 10;

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
 * 
 * @param grossPnl Brutto-PNL (vor Fees)
 * @param feeRate Fee-Rate (z.B. 0.002 für 0.2% bei Spot)
 * @param trades Anzahl der Trades (für bessere Schätzung)
 * @returns {netPnl: number, fees: number} Netto-PNL und Fees
 */
const calculateNetPnlAndFees = (grossPnl: number, feeRate: number, trades: number = 0): { netPnl: number; fees: number } => {
    if (!Number.isFinite(grossPnl) || !Number.isFinite(feeRate) || grossPnl === 0) {
        return { netPnl: grossPnl, fees: 0 };
    }
    
    // Bessere Schätzung: Verwende sowohl PNL als auch Anzahl der Trades
    // PNL ist typischerweise 1-5% des Notional, aber bei vielen Trades
    // können wir eine bessere Schätzung machen
    
    let estimatedNotional: number;
    
    if (trades > 0) {
        // Bei vielen Trades: Schätze Notional basierend auf durchschnittlichem Trade-Volumen
        // Durchschnittlicher PNL pro Trade
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

const tightnessClass = (value: number): string => {
    if (!Number.isFinite(value)) return "";
    if (value > 1.05) return "text-negative"; // >1 = strenger Einstieg
    if (value < 0.95) return "text-positive"; // <1 = lockerer Einstieg
    return "";
};

/**
 * Extrahiert Fee-Rate aus Settings.
 * 
 * Diese Funktion verwendet die Settings als "Master Settings" für die Fee-Berechnung.
 * Die Settings werden aus /api/settings/current geladen (settings.json).
 * Wenn Fee-Settings in den Settings vorhanden sind, werden diese verwendet.
 * Wenn nicht, werden Default-Werte verwendet (0.2% für Spot, 0.08% für Futures).
 * 
 * @param settings Settings-Objekt (aus /api/settings/current - Master Settings)
 * @returns Fee-Rate (z.B. 0.002 für 0.2% bei Spot)
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

export function DashboardPage(): JSX.Element {
    const [status, setStatus] = useState<BotStatus | null>(null);
    const [loadingStatus, setLoadingStatus] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);

    const [runInfo, setRunInfo] = useState<{ run_id: string | null; run_dir: string; db_path: string } | null>(null);
    const [trades, setTrades] = useState<Array<Record<string, unknown>>>([]);
    const [metrics, setMetrics] = useState<Array<Record<string, unknown>>>([]);
    const [aggregatedPnlLive, setAggregatedPnlLive] = useState<number>(0);
    const [aggregatedPnlTotal, setAggregatedPnlTotal] = useState<number>(0);
    const [totalTradesCount, setTotalTradesCount] = useState<number>(0);
    const [totalMetricsCount, setTotalMetricsCount] = useState<number>(0);
    const [liveTrades, setLiveTrades] = useState<Array<Record<string, unknown>>>([]);
    const [liveTradesPage, setLiveTradesPage] = useState<number>(1);
    const [liveTradesNewestRowid, setLiveTradesNewestRowid] = useState<number | null>(null);
    const [liveTradesOldestRowid, setLiveTradesOldestRowid] = useState<number | null>(null);
    const [liveTradesTotalCount, setLiveTradesTotalCount] = useState<number>(0);
    const [strategyStats, setStrategyStats] = useState<StrategyStat[]>([]);
    const [backtest, setBacktest] = useState<BacktestSnapshot | null>(null);
    const [referenceBacktestSummary, setReferenceBacktestSummary] = useState<BacktestSummaryPayload | null>(null);
    const [referenceBacktestId, setReferenceBacktestId] = useState<string | null>(null);
    const [referenceBacktestOptions, setReferenceBacktestOptions] = useState<Array<{ id: string; label: string }>>([]);
    const [backtestThreadStatus, setBacktestThreadStatus] = useState<BacktestThreadStatus | null>(null);
    const [settings, setSettings] = useState<SettingsResponse | null>(null);
    const [modeState, setModeState] = useState<ModeStateSnapshot | null>(null);
    const [dashboardLoaded, setDashboardLoaded] = useState<boolean>(false);

    const loadDashboard = useCallback(async () => {
        if (!dashboardLoaded) {
            setLoadingStatus(true);
        }
        try {
            const data = await fetchDashboard(100, 100, true, referenceBacktestId);
            if (data.errors && Object.keys(data.errors).length > 0) {
                console.warn("Dashboard partial errors:", data.errors);
            }
            setStatus(data.status ?? null);
            setRunInfo(data.run ?? null);
            setBacktest(data.backtest ?? null);
            setReferenceBacktestSummary(data.reference_backtest_summary ?? null);
            if (data.reference_backtest_id) {
                setReferenceBacktestId((prev) => prev ?? data.reference_backtest_id ?? null);
            }
            setBacktestThreadStatus(data.backtest_thread_status ?? null);
            setModeState(data.mode_state ?? null);
            setSettings(data.settings ?? null);

            if (!data.run) {
                setTrades([]);
                setLiveTrades([]);
                setMetrics([]);
                setAggregatedPnlLive(0);
                setAggregatedPnlTotal(0);
                setTotalTradesCount(0);
                setTotalMetricsCount(0);
                setStrategyStats([]);
                setBacktest(null);
                setReferenceBacktestSummary(null);
                return;
            }

            const tradesData = data.trades?.items ?? [];
            setTrades([...tradesData].reverse());
            setAggregatedPnlLive(data.live_trades?.aggregated_pnl ?? 0);
            setLiveTradesTotalCount(data.live_trades?.total ?? data.live_trades?.items?.length ?? 0);
            setAggregatedPnlTotal(data.trades?.aggregated_pnl ?? 0);
            setTotalTradesCount(data.trades?.total ?? data.trades?.items?.length ?? 0);
            setTotalMetricsCount(data.metrics?.total ?? data.metrics?.items?.length ?? 0);

            const metricsData = data.metrics?.items ?? [];
            setMetrics([...metricsData].reverse());
            setStrategyStats(data.strategy_stats ?? []);
            setError(null);
        } catch (err) {
            console.error("Fehler beim Laden des Dashboards:", err);
            let errorMessage = "Unbekannter Fehler";
            if (err instanceof Error) {
                errorMessage = err.message;
            } else if (typeof err === "string") {
                errorMessage = err;
            } else {
                errorMessage = JSON.stringify(err);
            }
            setError(`Dashboard konnte nicht geladen werden: ${errorMessage}`);
        } finally {
            setLoadingStatus(false);
            setDashboardLoaded(true);
        }
    }, [dashboardLoaded, referenceBacktestId]);

    const mergeLiveTrades = useCallback((incoming: Array<Record<string, unknown>>) => {
        if (!incoming.length) return;
        setLiveTrades((prev) => {
            const byId = new Map<number, Record<string, unknown>>();
            prev.forEach((t) => {
                const id = Number((t.id ?? t.rowid) as any ?? NaN);
                if (Number.isFinite(id)) byId.set(id, t);
            });
            incoming.forEach((t) => {
                const id = Number((t.id ?? t.rowid) as any ?? NaN);
                if (Number.isFinite(id)) byId.set(id, t);
            });
            return Array.from(byId.entries())
                // newest first
                .sort((a, b) => b[0] - a[0])
                .map(([, t]) => t);
        });

        const rowids = incoming
            .map((t) => Number((t.id ?? t.rowid) as any ?? NaN))
            .filter((n) => Number.isFinite(n)) as number[];
        if (rowids.length) {
            const maxId = Math.max(...rowids);
            const minId = Math.min(...rowids);
            setLiveTradesNewestRowid((prev) => (prev == null ? maxId : Math.max(prev, maxId)));
            setLiveTradesOldestRowid((prev) => (prev == null ? minId : Math.min(prev, minId)));
        }
    }, []);

    const loadLiveTradesInitial = useCallback(async () => {
        try {
            const resp = await fetchCurrentTrades(300, undefined, "live");
            setLiveTradesTotalCount(resp.total ?? resp.items?.length ?? 0);
            mergeLiveTrades(resp.items ?? []);
            if (typeof resp.aggregated_pnl === "number") {
                setAggregatedPnlLive(resp.aggregated_pnl);
            }
        } catch (err) {
            console.warn("Failed to load initial live trades:", err);
        }
    }, [mergeLiveTrades]);

    const loadLiveTradesDelta = useCallback(async () => {
        try {
            const after = liveTradesNewestRowid ?? undefined;
            const resp = await fetchCurrentTrades(300, after, "live");
            setLiveTradesTotalCount(resp.total ?? resp.items?.length ?? 0);
            mergeLiveTrades(resp.items ?? []);
            if (typeof resp.aggregated_pnl === "number") {
                setAggregatedPnlLive(resp.aggregated_pnl);
            }
        } catch (err) {
            console.warn("Failed to load live trades delta:", err);
        }
    }, [liveTradesNewestRowid, mergeLiveTrades]);

    const loadLiveTradesOlder = useCallback(async () => {
        try {
            if (liveTradesOldestRowid == null) return;
            const resp = await fetchCurrentTrades(300, undefined, "live", liveTradesOldestRowid);
            setLiveTradesTotalCount(resp.total ?? resp.items?.length ?? 0);
            mergeLiveTrades(resp.items ?? []);
        } catch (err) {
            console.warn("Failed to load older live trades:", err);
        }
    }, [liveTradesOldestRowid, mergeLiveTrades]);

    const loadReferenceBacktests = useCallback(async () => {
        try {
            const { items } = await fetchBacktests(50, 0);
            const options = (items || []).map((bt) => {
                const labelParts: string[] = [];
                const typeLabel = bt.backtest_type === "auto" ? "Auto" : "Manuell";
                labelParts.push(typeLabel);
                if (bt.start_date && bt.end_date) {
                    labelParts.push(`${bt.start_date} → ${bt.end_date}`);
                } else if (bt.window_start && bt.window_end) {
                    labelParts.push(`${bt.window_start} → ${bt.window_end}`);
                }
                if (bt.template_name) {
                    labelParts.push(bt.template_name);
                }
                if (bt.notes) {
                    labelParts.push(bt.notes);
                }
                const label = labelParts.filter(Boolean).join(" · ") || bt.id;
                return { id: bt.id, label };
            });
            setReferenceBacktestOptions(options);
        } catch (err) {
            console.warn("Konnte Backtests für Referenz nicht laden:", err);
            setReferenceBacktestOptions([]);
        }
    }, []);

    const liveTradesPageCount = useMemo(() => {
        return Math.max(1, Math.ceil(liveTrades.length / LIVE_TRADES_PAGE_SIZE));
    }, [liveTrades.length]);

    useEffect(() => {
        setLiveTradesPage((prev) => Math.min(prev, liveTradesPageCount));
    }, [liveTradesPageCount]);

    const pagedLiveTrades = useMemo(() => {
        const start = (liveTradesPage - 1) * LIVE_TRADES_PAGE_SIZE;
        return liveTrades.slice(start, start + LIVE_TRADES_PAGE_SIZE);
    }, [liveTrades, liveTradesPage]);

    const liveTradesPageNumbers = useMemo(() => {
        const maxButtons = 5;
        const half = Math.floor(maxButtons / 2);
        let start = Math.max(1, liveTradesPage - half);
        let end = Math.min(liveTradesPageCount, start + maxButtons - 1);
        start = Math.max(1, end - maxButtons + 1);
        const pages: number[] = [];
        for (let i = start; i <= end; i += 1) {
            pages.push(i);
        }
        return pages;
    }, [liveTradesPage, liveTradesPageCount]);

    useVisibilityPolling(loadDashboard, {
        intervalMs: 30_000,
        initialDelayMs: 200,
        backoffFactor: 2,
        maxIntervalMs: 5 * 60_000,
    });

    useVisibilityPolling(loadLiveTradesDelta, {
        intervalMs: 30_000,
        initialDelayMs: 500,
        backoffFactor: 2,
        maxIntervalMs: 5 * 60_000,
    });

    useEffect(() => {
        if (!runInfo?.db_path) return;
        setLiveTrades([]);
        setLiveTradesNewestRowid(null);
        setLiveTradesOldestRowid(null);
        void loadLiveTradesInitial();
    }, [runInfo?.db_path, loadLiveTradesInitial]);

    useEffect(() => {
        const stored = localStorage.getItem("dashboard_reference_backtest_id");
        if (stored && !referenceBacktestId) {
            setReferenceBacktestId(stored);
        }
        void loadReferenceBacktests();
    }, [loadReferenceBacktests, referenceBacktestId]);

    useEffect(() => {
        if (referenceBacktestId) {
            localStorage.setItem("dashboard_reference_backtest_id", referenceBacktestId);
        }
    }, [referenceBacktestId]);

    // Dashboard-Backtest-Card zeigt nur Auto-Backtests des aktuellen Runs.

    const latestTrades = useMemo(() => trades.slice(-100).reverse(), [trades]);
    const latestMetrics = useMemo(() => metrics.slice(-100).reverse(), [metrics]);
    const latestTightness = useMemo(() => {
        const map = new Map<string, { strategy: string; symbol: string; direction: string; tightness: number; timestamp: string; bias?: string }>();
        metrics.forEach((metric) => {
            const tight = Number(metric.entry_tightness ?? NaN);
            if (!Number.isFinite(tight)) return;
            const ts = (metric.timestamp as string | undefined) || "";
            const strategy = (metric.strategy as string) || "unknown";
            const symbol = (metric.symbol as string) || "unknown";
            const direction = (metric.direction as string)?.toLowerCase() || (metric.bias as string)?.toLowerCase() || "n/a";
            const key = `${strategy}|${symbol}|${direction}`;
            const existing = map.get(key);
            if (!existing || ts > existing.timestamp) {
                map.set(key, { strategy, symbol, direction, tightness: tight, timestamp: ts, bias: metric.bias as string | undefined });
            }
        });
        return Array.from(map.values()).sort((a, b) => {
            if (a.strategy === b.strategy) {
                if (a.symbol === b.symbol) {
                    return a.direction.localeCompare(b.direction);
                }
                return a.symbol.localeCompare(b.symbol);
            }
            return a.strategy.localeCompare(b.strategy);
        });
    }, [metrics]);
    const strategyStatsBySymbol = useMemo(() => {
        const grouped = new Map<string, StrategyStat[]>();
        strategyStats.forEach((stat) => {
            const symbol = stat.symbol || "unknown";
            const list = grouped.get(symbol);
            if (list) {
                list.push(stat);
            } else {
                grouped.set(symbol, [stat]);
            }
        });
        return Array.from(grouped.entries())
            .map(([symbol, statsForSymbol]) => ({
                symbol,
                stats: [...statsForSymbol].sort((a, b) => {
                    const byStrategy = a.strategy.localeCompare(b.strategy);
                    if (byStrategy !== 0) {
                        return byStrategy;
                    }
                    if (a.direction === b.direction) return 0;
                    return a.direction === "long" ? -1 : 1;
                }),
            }))
            .sort((a, b) => a.symbol.localeCompare(b.symbol));
    }, [strategyStats]);

    const formatIso = (value?: string | null): string => {
        if (!value) return "—";
        try {
            return new Date(value).toLocaleString();
        } catch {
            return value;
        }
    };

    const backtestDetails = backtest?.summary;
    const backtestSummary = (backtestDetails as any)?.summary_normal ?? backtestDetails?.summary;
    const backtestSymbolStrategySide =
        (backtestDetails as any)?.by_symbol_strategy_side_all ??
        backtestDetails?.by_symbol_strategy_side ??
        [];
    const referenceBacktestDetails = referenceBacktestSummary ?? backtestDetails;
    const referenceSymbolStrategySide =
        ((referenceBacktestDetails as any)?.by_symbol_strategy_side_all ??
            referenceBacktestDetails?.by_symbol_strategy_side ??
            []) as BacktestSummarySlice[];
    const buildBacktestMap = (rows: BacktestSummarySlice[]) => {
        const map = new Map<string, BacktestSummarySlice>();
        const add = (key: string, row: BacktestSummarySlice) => {
            if (!map.has(key)) {
                map.set(key, row);
            }
        };
        rows.forEach((row) => {
            const symbol = String(row.symbol ?? "").toUpperCase();
            const strategy = String(row.strategy ?? "");
            const side = String(row.side ?? "").toLowerCase();
            if (!symbol || !strategy) {
                return;
            }
            add(`${symbol}||${strategy}||${side}`, row);
            add(`${symbol}||${strategy}`, row);
            const suffixMatch = strategy.match(/_(long|short)$/i);
            if (suffixMatch) {
                const suffixSide = suffixMatch[1].toLowerCase();
                add(`${symbol}||${strategy}||${suffixSide}`, row);
                const baseStrategy = strategy.replace(/_(long|short)$/i, "");
                add(`${symbol}||${baseStrategy}||${suffixSide}`, row);
                add(`${symbol}||${baseStrategy}`, row);
            }
        });
        return map;
    };

    const referenceSymbolStrategySideMap = buildBacktestMap(referenceSymbolStrategySide || []);
    const backtestSymbolStrategySideLive =
        (referenceBacktestDetails as any)?.by_symbol_strategy_side ?? backtestDetails?.by_symbol_strategy_side ?? [];
    const backtestSymbolStrategySideLiveMap = buildBacktestMap(backtestSymbolStrategySideLive || []);

    // Ensure active live paths are represented in backtest maps even if no trades occurred.
    // This avoids displaying "—" for active branches that simply had 0 backtest trades.
    const ensureBacktestRowsForStats = (
        map: Map<string, BacktestSummarySlice>,
        statsList: StrategyStat[],
    ): Map<string, BacktestSummarySlice> => {
        const out = new Map(map);
        const makeZeroRow = (symbol: string, strategy: string, side: string): BacktestSummarySlice => ({
            symbol,
            strategy,
            side,
            trades: 0,
            wins: 0,
            win_rate: 0,
            pnl: 0,
            fees: 0,
        });
        for (const stat of statsList) {
            const statSymbol = String(stat.symbol ?? "").toUpperCase();
            const strategy = String(stat.strategy ?? "");
            const direction = String(stat.direction ?? "").toLowerCase();
            const side = direction === "buy" ? "long" : direction === "sell" ? "short" : direction;
            if (!statSymbol || !strategy || !side) {
                continue;
            }
            const exactKey = `${statSymbol}||${strategy}||${side}`;
            if (out.has(exactKey)) {
                continue;
            }
            const baseStrategy = strategy.replace(/_(long|short)$/i, "");
            const fallbackKeys = [
                `${statSymbol}||${strategy}`,
                `${statSymbol}||${baseStrategy}||${side}`,
                `${statSymbol}||${baseStrategy}`,
            ];
            const matched = fallbackKeys.some((key) => out.has(key));
            if (!matched) {
                out.set(exactKey, makeZeroRow(statSymbol, strategy, side));
            }
        }
        return out;
    };
    const referenceSymbolStrategySideMapWithZeros = ensureBacktestRowsForStats(referenceSymbolStrategySideMap, strategyStats);
    const backtestSymbolStrategySideLiveMapWithZeros = ensureBacktestRowsForStats(backtestSymbolStrategySideLiveMap, strategyStats);
    
    // Berechne Fee-Rate aus Settings
    const feeRate = useMemo(() => getFeeRateFromSettings(settings), [settings]);
    
    // Helper-Funktion: Berechnet Netto-PNL, Brutto-PNL und Fees für einen Row
    const getPnlValues = (row: { pnl: number; fees?: number; trades?: number }): { gross: number; net: number; fees: number } => {
        // Wenn Backend bereits Werte berechnet hat, verwende diese (Fallback)
        if ((row as any).pnl_net != null && (row as any).fees_current != null) {
            const gross = (row as any).pnl_net + (row as any).fees_current;
            return {
                gross,
                net: (row as any).pnl_net,
                fees: (row as any).fees_current,
            };
        }
        
        // Berechne Brutto-PNL: Netto-PNL (alt) + Fees (alt)
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
        
        // Berechne neuen Netto-PNL und Fees basierend auf aktuellen Fee-Settings (Fallback)
        const trades = row.trades ?? 0;
        const { netPnl, fees } = calculateNetPnlAndFees(grossPnl, feeRate, trades);
        
        return {
            gross: grossPnl,
            net: netPnl,
            fees,
        };
    };

    const getAvgNotional = (row: { avg_notional?: number } | null | undefined): number | null => {
        if (!row) return null;
        const value = (row as any).avg_notional;
        return Number.isFinite(value ?? NaN) ? (value as number) : null;
    };

    const getPnlPerTradePct = (row: { pnl_per_trade_pct?: number; avg_notional?: number; pnl: number; fees?: number; trades?: number } | null | undefined): number | null => {
        if (!row) return null;
        const trades = row.trades ?? 0;
        if (!Number.isFinite(trades) || trades <= 0) {
            return null;
        }
        const direct = (row as any).pnl_per_trade_pct;
        if (Number.isFinite(direct ?? NaN)) {
            return direct as number;
        }
        const avgNotional = getAvgNotional(row);
        if (!avgNotional) return null;
        const netPnl = getPnlValues(row as { pnl: number; fees?: number; trades?: number }).net;
        const pnlPerTrade = calculatePnLPerTrade(netPnl, trades);
        return (pnlPerTrade / avgNotional) * 100;
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

    const mapBacktestBadge = (status: string | undefined): string => {
        switch (status) {
            case "running":
                return "badge--running";
            case "completed":
                return "badge--completed";
            case "failed":
                return "badge--failed";
            default:
                return "badge--idle";
        }
    };

    const formatPct = (value: number): string => `${(value * 100).toFixed(1)}%`;

    return (
        <>
            <style>{`
                @keyframes pulse {
                    0%, 100% {
                        opacity: 1;
                    }
                    50% {
                        opacity: 0.5;
                    }
                }
            `}</style>
            <section className="page">
            <header className="page__header">
                <div>
                    <h2>Live Dashboard</h2>
                    <p>Kurzübersicht zum aktuellen Bot-Run.</p>
                </div>
            </header>

            {loadingStatus && <div className="card">Lade...</div>}
            {error && (
                <div className="card card--error" style={{ padding: "15px", backgroundColor: "#fee", color: "#c00", borderRadius: "4px", marginBottom: "20px" }}>
                    <strong>Fehler:</strong> {error}
                    <button 
                        onClick={() => {
                            setError(null);
                            window.location.reload();
                        }}
                        style={{ 
                            marginLeft: "10px", 
                            padding: "5px 10px", 
                            backgroundColor: "#c00", 
                            color: "#fff", 
                            border: "none", 
                            borderRadius: "4px", 
                            cursor: "pointer" 
                        }}
                    >
                        Seite neu laden
                    </button>
                </div>
            )}

            {status && (
                <div className="grid grid--cols-2 grid--gap-lg">
                    <div className="card">
                        <h3>Status</h3>
                        <ul className="kv-list">
                            <li>
                                <span>Run-ID</span>
                                <strong>{status.run_id ?? "—"}</strong>
                            </li>
                            <li>
                                <span>Status</span>
                                <strong className={`badge badge--${status.status}`}>{status.status}</strong>
                            </li>
                            <li>
                                <span>Startzeit</span>
                                <strong>{status.started_at ?? "—"}</strong>
                            </li>
                            <li>
                                <span>Heartbeat</span>
                                <strong>{status.heartbeat ?? "—"}</strong>
                            </li>
                            <li>
                                <span>Laufzeit</span>
                                <strong>{status.runtime_seconds ? `${(status.runtime_seconds / 60).toFixed(1)} min` : "—"}</strong>
                            </li>
                        </ul>
                    </div>

                    <div className="card">
                        <h3>Live Kennzahlen</h3>
                        <ul className="kv-list">
                            <li>
                                <span>Aggregierter PnL (nur Live-Trades)</span>
                                <strong>{aggregatedPnlLive.toFixed(2)}</strong>
                            </li>
                            <li>
                                <span>PnL Live + Test (alle Trades)</span>
                                <strong>{aggregatedPnlTotal.toFixed(2)}</strong>
                            </li>
                            <li>
                                <span>Trades (Run gesamt)</span>
                                <strong>{totalTradesCount}</strong>
                            </li>
                            <li>
                                <span>Metrics (Run gesamt)</span>
                                <strong>{totalMetricsCount}</strong>
                            </li>
                        </ul>
                    </div>

            </div>
        )}

            <PriceChart symbols={["BTCUSDT", "ETHUSDT"]} months={3} />

            <div className="card" style={{ marginBottom: "2rem" }}>
                <h3>🚀 LIVE Trade Tracker (Kraken)</h3>
                <p style={{ marginBottom: "1rem", opacity: 0.8 }}>
                    Nur bestätigte Live-Trades von Kraken Futures mit gültiger Kraken Order ID
                </p>
                <p style={{ marginBottom: "1rem", fontSize: "0.85em", opacity: 0.7 }}>
                    Geladen: {liveTrades.length} / {liveTradesTotalCount}
                    {liveTradesTotalCount > liveTrades.length ? (
                        <>
                            {" "}
                            <button
                                type="button"
                                className="button button--small"
                                onClick={() => void loadLiveTradesOlder()}
                                style={{ marginLeft: "0.5rem" }}
                            >
                                Ältere laden
                            </button>
                        </>
                    ) : null}
                </p>
                {liveTrades.length === 0 ? (
                    <p>Noch keine Live-Trades auf Kraken mit gültiger Order ID gefunden.</p>
                ) : (
                    <div className="table-wrapper">
                        <table className="table">
                            <thead>
                                <tr>
                                    <th>Zeit</th>
                                    <th>Strategie</th>
                                    <th>Symbol</th>
                                    <th>Richtung</th>
                                    <th>Menge</th>
                                    <th>Entry Price</th>
                                    <th>Exit Price</th>
                                    <th>Status</th>
                                    <th>PnL (Netto)</th>
                                    <th>Fees</th>
                                    <th>Kraken Order ID</th>
                                </tr>
                            </thead>
                            <tbody>
                                {pagedLiveTrades.map((trade) => {
                                    const pnlNet = Number(trade.pnl_net ?? trade.pnl ?? 0);
                                    const fees = Number(trade.fees ?? 0);
                                    const entryPrice = Number(trade.entry_price ?? 0);
                                    const exitPrice = trade.exit_price !== null ? Number(trade.exit_price ?? 0) : null;
                                    const isOpen = exitPrice === null;
                                    const timestamp = trade.timestamp as string;
                                    const entryTime = trade.timestamp as string;
                                    const exitTime = trade.exit_timestamp as string | null;
                                    
                                    // Formatiere Datum und Zeit schön
                                    const formatDateTime = (isoString: string | null | undefined) => {
                                        if (!isoString) return "—";
                                        try {
                                            const date = new Date(isoString);
                                            const dateStr = date.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
                                            const timeStr = date.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                                            return `${dateStr} ${timeStr}`;
                                        } catch {
                                            return isoString;
                                        }
                                    };
                                    
                                    return (
                                        <tr key={String((trade.id as any) ?? (trade.rowid as any) ?? `${trade.strategy ?? "unknown"}-${trade.timestamp ?? ""}`)}>
                                            <td>
                                                {exitTime 
                                                    ? `${formatDateTime(entryTime)} → ${formatDateTime(exitTime)}`
                                                    : formatDateTime(entryTime)
                                                }
                                            </td>
                                            <td>{trade.strategy as string}</td>
                                            <td><strong>{trade.symbol as string}</strong></td>
                                            <td>
                                                <span className={`badge badge--${(trade.direction as string)?.toLowerCase() === "buy" ? "success" : "danger"}`}>
                                                    {(trade.direction as string)?.toUpperCase()}
                                                </span>
                                            </td>
                                            <td>{Number(trade.quantity ?? 0).toFixed(4)}</td>
                                            <td><strong>{entryPrice.toFixed(2)}</strong></td>
                                            <td>
                                                {exitPrice !== null ? (
                                                    <strong>{exitPrice.toFixed(2)}</strong>
                                                ) : (
                                                    <span style={{ opacity: 0.5 }}>—</span>
                                                )}
                                            </td>
                                            <td>
                                                {isOpen ? (
                                                    <span className="badge badge--warning">OFFEN</span>
                                                ) : (
                                                    <span className="badge badge--success">GESCHLOSSEN</span>
                                                )}
                                            </td>
                                            <td className={getPnLClass(pnlNet)}>
                                                {exitPrice !== null ? (
                                                    <strong>{pnlNet.toFixed(2)}</strong>
                                                ) : (
                                                    <span style={{ opacity: 0.5 }}>—</span>
                                                )}
                                            </td>
                                            <td>{exitPrice !== null ? fees.toFixed(2) : <span style={{ opacity: 0.5 }}>—</span>}</td>
                                            <td>
                                                {(() => {
                                                    const notes = trade.notes as string | null | undefined;
                                                    if (notes) {
                                                        // Case-insensitive Suche nach Kraken Order ID
                                                        const orderIdMatch = notes.match(/kraken\s+order\s+id:\s*([^\s\n]+)/i);
                                                        if (orderIdMatch && orderIdMatch[1]) {
                                                            return (
                                                                <code style={{ fontSize: "0.85em", backgroundColor: "#f5f5f5", padding: "2px 6px", borderRadius: "3px" }}>
                                                                    {orderIdMatch[1]}
                                                                </code>
                                                            );
                                                        }
                                                    }
                                                    // Dieser Fall sollte eigentlich nie auftreten, da der Filter bereits greift
                                                    return <span style={{ opacity: 0.5, color: "#f57c00", fontSize: "0.85em" }}>—</span>;
                                                })()}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                        {liveTrades.length > LIVE_TRADES_PAGE_SIZE && (
                            <div className="pagination" style={{ marginTop: "1rem" }}>
                                <button
                                    type="button"
                                    className="pagination__item"
                                    onClick={() => setLiveTradesPage((prev) => Math.max(1, prev - 1))}
                                    disabled={liveTradesPage === 1}
                                    style={liveTradesPage === 1 ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                                >
                                    ‹
                                </button>
                                {liveTradesPageNumbers.map((page) => (
                                    <button
                                        key={`live-trades-page-${page}`}
                                        type="button"
                                        className={`pagination__item ${page === liveTradesPage ? "pagination__item--active" : ""}`}
                                        onClick={() => setLiveTradesPage(page)}
                                    >
                                        {page}
                                    </button>
                                ))}
                                <button
                                    type="button"
                                    className="pagination__item"
                                    onClick={() => setLiveTradesPage((prev) => Math.min(liveTradesPageCount, prev + 1))}
                                    disabled={liveTradesPage === liveTradesPageCount}
                                    style={liveTradesPage === liveTradesPageCount ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                                >
                                    ›
                                </button>
                                <span style={{ marginLeft: "0.5rem", fontSize: "0.85em", opacity: 0.7 }}>
                                    Seite {liveTradesPage} / {liveTradesPageCount}
                                </span>
                            </div>
                        )}
                    </div>
                )}
            </div>

            <div className="card">
                <h3>Strategie-Metriken (Live)</h3>
                <p style={{ marginBottom: "1rem", fontSize: "0.9em", opacity: 0.8 }}>
                    💡 <strong>Hinweis:</strong> Auto-Switch basiert auf EMA (Exponential Moving Average). 
                    Strategien werden zu LIVE geschaltet, wenn der aktuelle PnL/Trade deutlich über dem EMA liegt 
                    (EMA + Threshold). Thresholds können in Settings → Mode Controller angepasst werden.
                </p>
                {strategyStatsBySymbol.length === 0 ? (
                    <p>Noch keine Strategie-Statistiken verfügbar.</p>
                ) : (
                    strategyStatsBySymbol.map(({ symbol, stats }) => (
                        <div key={`strategy-stats-${symbol}`}>
                            <h4>{symbol}</h4>
                            <div className="table-wrapper">
                                <table className="table">
                                    <thead>
                                        <tr>
                                            <th>Strategie</th>
                                            <th>Bias</th>
                                            <th>Mode</th>
                                            <th>PnL (Brutto)</th>
                                            <th>PnL (Netto)</th>
                                            <th>Trades</th>
                                            <th>Wins</th>
                                            <th>Win-Rate</th>
                                            <th>PnL/Trade (Netto)</th>
                                            <th>Bcktst PnL/T All</th>
                                            <th>Bcktst PnL/T Live</th>
                                            <th>PnL (letzte 5 Trades)</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {stats.map((stat, index) => {
                                            const pnlGross = stat.pnl_gross ?? stat.pnl;
                                            const pnlNet = stat.pnl_net ?? stat.pnl;
                                            const pnlPerTrade = stat.pnl_per_trade ?? stat.mean_return;
                                            const recent5Pnl = stat.recent_5_pnl ?? 0;
                                            
                                            // Finde Mode-Informationen aus modeState
                                            const directionKey = stat.direction.toLowerCase() === "buy" ? "long" : stat.direction.toLowerCase() === "sell" ? "short" : stat.direction.toLowerCase();
                                            const statSymbol = String(stat.symbol ?? symbol ?? "").toUpperCase();
                                            const pathKey = `${stat.strategy}|${statSymbol}|${directionKey}`;
                                            const pathState = modeState?.paths?.[pathKey];
                                            const lookupKey = `${statSymbol}||${stat.strategy}||${directionKey}`;
                                            const baseStrategyKey = stat.strategy.replace(/_(long|short)$/i, "");
                                            const referenceRow =
                                                referenceSymbolStrategySideMapWithZeros.get(lookupKey) ??
                                                referenceSymbolStrategySideMapWithZeros.get(`${statSymbol}||${stat.strategy}`) ??
                                                referenceSymbolStrategySideMapWithZeros.get(`${statSymbol}||${baseStrategyKey}||${directionKey}`) ??
                                                referenceSymbolStrategySideMapWithZeros.get(`${statSymbol}||${baseStrategyKey}`);
                                            const backtestRow =
                                                backtestSymbolStrategySideLiveMapWithZeros.get(lookupKey) ??
                                                backtestSymbolStrategySideLiveMapWithZeros.get(`${statSymbol}||${stat.strategy}`) ??
                                                backtestSymbolStrategySideLiveMapWithZeros.get(`${statSymbol}||${baseStrategyKey}||${directionKey}`) ??
                                                backtestSymbolStrategySideLiveMapWithZeros.get(`${statSymbol}||${baseStrategyKey}`);
                                            const referencePnlPerTradePct = referenceRow
                                                ? getPnlPerTradePct(referenceRow)
                                                : null;
                                            const backtestPnlPerTradePct = backtestRow
                                                ? getPnlPerTradePct(backtestRow)
                                                : null;
                                            // Wenn manual_mode gesetzt ist und sich von current_mode unterscheidet, verwende manual_mode
                                            const displayMode = (pathState?.manual_mode && pathState.manual_mode !== pathState.current_mode)
                                                ? pathState.manual_mode
                                                : (pathState?.current_mode ?? "test");
                                            const modeBadgeClass = displayMode === "live" ? "badge badge--live" : "badge badge--test";
                                            
                                            return (
                                                <tr key={`${symbol}-${stat.strategy}-${stat.direction}-${index}`}>
                                                    <td>{stat.strategy}</td>
                                                    <td>{stat.direction}</td>
                                                    <td>
                                                        <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                                                            <span 
                                                                className={modeBadgeClass} 
                                                                title={
                                                                    pathState?.degraded_reason 
                                                                        ? `Nicht auf LIVE geschaltet: ${pathState.degraded_reason.replace(/_/g, " ")}`
                                                                        : pathState?.last_switch_reason ?? ""
                                                                }
                                                            >
                                                                {displayMode.toUpperCase()}
                                                            </span>
                                                            {pathState?.degraded_reason && displayMode === "test" && (
                                                                <div style={{ fontSize: "0.7em", color: "#f57c00", fontStyle: "italic" }}>
                                                                    {(() => {
                                                                        const reason = pathState.degraded_reason.replace(/_/g, " ");
                                                                        // Übersetze häufige Gründe in verständliche Texte
                                                                        if (reason.includes("no positive impulse")) {
                                                                            return "⚠️ Keine positiven Impulse";
                                                                        }
                                                                        if (reason.includes("insufficient test trades")) {
                                                                            return "⚠️ Zu wenige Test-Trades";
                                                                        }
                                                                        if (reason.includes("only noise trades")) {
                                                                            return "⚠️ Nur Noise-Trades";
                                                                        }
                                                                        if (reason.includes("cooldown")) {
                                                                            return "⏱️ Noch im Cooldown";
                                                                        }
                                                                        return `⚠️ ${reason}`;
                                                                    })()}
                                                                </div>
                                                            )}
                                                        </div>
                                                    </td>
                                                    <td className={getPnLClass(pnlGross)}>{pnlGross.toFixed(2)}</td>
                                                    <td className={getPnLClass(pnlNet)}>{pnlNet.toFixed(2)}</td>
                                                    <td>{stat.trades}</td>
                                                    <td>{Math.round(stat.wins ?? stat.win_rate * stat.trades)}</td>
                                                    <td>{(stat.win_rate * 100).toFixed(1)}%</td>
                                                    <td className={getPnLClass(pnlPerTrade)}>
                                                        {pnlPerTrade.toFixed(2)}
                                                    </td>
                                                    <td className={referencePnlPerTradePct == null ? "" : getPnLClass(referencePnlPerTradePct)}>
                                                        {referencePnlPerTradePct == null ? "—" : `${referencePnlPerTradePct.toFixed(2)}%`}
                                                    </td>
                                                    <td className={backtestPnlPerTradePct == null ? "" : getPnLClass(backtestPnlPerTradePct)}>
                                                        {backtestPnlPerTradePct == null ? "—" : `${backtestPnlPerTradePct.toFixed(2)}%`}
                                                    </td>
                                                    <td className={getPnLClass(recent5Pnl)}>
                                                        {recent5Pnl.toFixed(2)}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    ))
                )}
            </div>

            <div className="card">
                <h3>Backtest (letzte 90 Tage)</h3>
                <div style={{ marginBottom: "12px", display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
                    <label style={{ fontSize: "0.85em", opacity: 0.8 }}>Referenz‑Backtest</label>
                    <select
                        value={referenceBacktestId ?? ""}
                        onChange={(event) => {
                            const next = event.target.value || null;
                            setReferenceBacktestId(next);
                            void loadDashboard();
                        }}
                    >
                        <option value="">(Settings‑Default)</option>
                        {referenceBacktestOptions.map((opt) => (
                            <option key={opt.id} value={opt.id}>
                                {opt.label}
                            </option>
                        ))}
                    </select>
                </div>
                {!backtest ? (
                    <p>Kein Backtest-Snapshot verfügbar. Falls gerade einer läuft, wird er automatisch geladen.</p>
                ) : (
                    <>
                        <ul className="kv-list">
                            <li>
                                <span>Status</span>
                                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                    <strong className={`badge ${mapBacktestBadge(backtest.status)}`}>
                                        {backtest.status}
                                    </strong>
                                    {backtest.status === "running" && (
                                        <>
                                            {backtestThreadStatus?.thread_running ? (
                                                <span
                                                    style={{
                                                        display: "inline-block",
                                                        width: "12px",
                                                        height: "12px",
                                                        borderRadius: "50%",
                                                        backgroundColor: "#4caf50",
                                                        animation: "pulse 2s infinite",
                                                    }}
                                                    title={`Thread läuft (ID: ${backtestThreadStatus.thread_id})`}
                                                />
                                            ) : (
                                                <span
                                                    style={{
                                                        display: "inline-block",
                                                        width: "12px",
                                                        height: "12px",
                                                        borderRadius: "50%",
                                                        backgroundColor: "#ff9800",
                                                        animation: "pulse 2s infinite",
                                                    }}
                                                    title="Thread-Status unbekannt; laut Backtest-API läuft der Job"
                                                />
                                            )}
                                        </>
                                    )}
                                </div>
                            </li>
                            <li>
                                <span>Zeitraum</span>
                                <strong>
                                    {backtest.window_start?.slice(0, 10) ?? "—"} &rarr;{" "}
                                    {backtest.window_end?.slice(0, 10) ?? "—"}
                                </strong>
                            </li>
                            <li>
                                <span>Gestartet</span>
                                <strong>{formatIso(backtest.started_at)}</strong>
                            </li>
                            <li>
                                <span>Fertig</span>
                                <strong>{formatIso(backtest.completed_at)}</strong>
                            </li>
                        </ul>
                        {backtest.status === "completed" && backtestSummary ? (
                            <>
                                <ul className="kv-list">
                                    <li>
                                        <span>PnL (Brutto)</span>
                                        <strong>
                                            {getGrossPnl(backtestSummary).toFixed(2)}
                                        </strong>
                                    </li>
                                    <li>
                                        <span>PnL (Netto)</span>
                                        <strong>
                                            {getNetPnl(backtestSummary).toFixed(2)}
                                        </strong>
                                    </li>
                                    <li>
                                        <span>Fees (aktuell)</span>
                                        <strong>
                                            {getCurrentFees(backtestSummary).toFixed(2)}
                                        </strong>
                                    </li>
                                    <li>
                                        <span>PnL (Original)</span>
                                        <strong style={{ opacity: 0.7 }}>
                                            {backtestSummary.pnl.toFixed(2)}
                                        </strong>
                                    </li>
                                    <li>
                                        <span>Trades</span>
                                        <strong>{backtestSummary.trades}</strong>
                                    </li>
                                    <li>
                                        <span>Win-Rate</span>
                                        <strong>{formatPct(backtestSummary.win_rate)}</strong>
                                    </li>
                                </ul>

                                <h4>Pro Symbol · Strategie · Richtung</h4>
                                {backtestSymbolStrategySide.length === 0 ? (
                                    <p>Keine detaillierten Kombinationen vorhanden.</p>
                                ) : (
                                    <div className="table-wrapper">
                                        <table className="table">
                                            <thead>
                                                <tr>
                                                    <th>Symbol</th>
                                                    <th>Strategie</th>
                                                    <th>Richtung</th>
                                                    <th>Trades</th>
                                                    <th>Wins</th>
                                                    <th>Win-Rate</th>
                                                    <th>PnL (Brutto)</th>
                                                    <th>PnL (Netto)</th>
                                                    <th>Fees (aktuell)</th>
                                                    <th>Ø Notional/Trade</th>
                                                    <th>PnL/Trade (%)</th>
                                                    <th>Live Trades</th>
                                                    <th>Live PnL</th>
                                                    <th>Live PnL/Trade (%)</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {backtestSymbolStrategySide.map((row, index) => {
                                                    const pnlValues = getPnlValues(row);
                                                    const avgNotional = getAvgNotional(row);
                                                    const pnlPerTradePct = getPnlPerTradePct(row);
                                                    const liveRow = backtestSymbolStrategySideLiveMap.get(
                                                        `${row.symbol ?? ""}||${row.strategy ?? ""}||${row.side ?? ""}`
                                                    );
                                                    const liveNet = liveRow ? getNetPnl(liveRow) : 0;
                                                    const livePnlPerTradePct = liveRow ? getPnlPerTradePct(liveRow) : null;
                                                    return (
                                                        <tr key={`${row.symbol}-${row.strategy}-${row.side}-${index}`}>
                                                            <td>{row.symbol}</td>
                                                            <td>{row.strategy}</td>
                                                            <td>{row.side}</td>
                                                            <td>{row.trades}</td>
                                                            <td>{row.wins}</td>
                                                            <td>{formatPct(row.win_rate)}</td>
                                                            <td>{pnlValues.gross.toFixed(2)}</td>
                                                            <td>{pnlValues.net.toFixed(2)}</td>
                                                            <td>{pnlValues.fees.toFixed(2)}</td>
                                                            <td>{avgNotional == null ? "—" : avgNotional.toFixed(2)}</td>
                                                            <td className={pnlPerTradePct == null ? "" : getPnLClass(pnlPerTradePct)}>
                                                                {pnlPerTradePct == null ? "—" : `${pnlPerTradePct.toFixed(2)}%`}
                                                            </td>
                                                            <td>{liveRow?.trades ?? 0}</td>
                                                            <td className={getPnLClass(liveNet)}>
                                                                {liveNet.toFixed(2)}
                                                            </td>
                                                            <td className={livePnlPerTradePct == null ? "" : getPnLClass(livePnlPerTradePct)}>
                                                                {livePnlPerTradePct == null ? "—" : `${livePnlPerTradePct.toFixed(2)}%`}
                                                            </td>
                                                        </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </>
                        ) : backtest.status === "failed" ? (
                            <p>Backtest fehlgeschlagen: {backtest.error ?? "Unbekannter Fehler"}</p>
                        ) : (
                            <p>Backtest läuft – Ergebnisse folgen automatisch.</p>
                        )}
                    </>
                )}
            </div>

            <div className="grid grid--cols-2 grid--gap-lg">
                <div className="card">
                    <h3>Letzte Trades (alle)</h3>
                    {trades.length === 0 ? (
                        <p>Noch keine Trades im aktuellen Run.</p>
                    ) : (
                        <div className="table-wrapper">
                            <table className="table">
                                <thead>
                                    <tr>
                                        <th>Zeit</th>
                                        <th>Symbol</th>
                                        <th>Richtung</th>
                                        <th>Menge</th>
                                        <th>Entry</th>
                                        <th>Exit</th>
                                        <th>PnL (Brutto)</th>
                                        <th>PnL (Netto)</th>
                                        <th>Fees</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {latestTrades.map((trade) => {
                                        const pnlGross = Number(trade.pnl_gross ?? trade.pnl ?? 0);
                                        const pnlNet = Number(trade.pnl_net ?? trade.pnl ?? 0);
                                        const fees = Number(trade.fees ?? 0);
                                        return (
                                            <tr key={trade.rowid as number}>
                                                <td>{(trade.timestamp as string)?.slice(11, 19)}</td>
                                                <td>{trade.symbol as string}</td>
                                                <td>{trade.direction as string}</td>
                                                <td>{Number(trade.quantity ?? 0).toFixed(4)}</td>
                                                <td>{Number(trade.entry_price ?? 0).toFixed(2)}</td>
                                                <td>{trade.exit_price !== null ? Number(trade.exit_price ?? 0).toFixed(2) : "—"}</td>
                                                <td className={getPnLClass(pnlGross)}>{pnlGross.toFixed(2)}</td>
                                                <td className={getPnLClass(pnlNet)}>{pnlNet.toFixed(2)}</td>
                                                <td>{fees.toFixed(2)}</td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>

                <div className="card">
                    <h3>Letzte Metrics</h3>
                    {latestMetrics.length === 0 ? (
                        <p>Noch keine Metrics vorhanden.</p>
                    ) : (
                        <div className="table-wrapper">
                            <table className="table">
                                <thead>
                                    <tr>
                                        <th>Zeit</th>
                                        <th>Symbol</th>
                                        <th>Strategie</th>
                                        <th>Bias</th>
                                        <th>Close</th>
                                        <th>VWAP</th>
                                        <th>RSI</th>
                                        <th>Tightness</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {latestMetrics.map((metric) => (
                                        <tr key={metric.rowid as number}>
                                            <td>{(metric.timestamp as string | null)?.slice(11, 19) ?? "—"}</td>
                                            <td>{metric.symbol as string}</td>
                                            <td>{metric.strategy as string}</td>
                                            <td>{metric.bias as string}</td>
                                            <td>{metric.close !== null ? Number(metric.close ?? 0).toFixed(2) : "—"}</td>
                                            <td>{metric.vwap !== null ? Number(metric.vwap ?? 0).toFixed(2) : "—"}</td>
                                            <td>{metric.rsi !== null ? Number(metric.rsi ?? 0).toFixed(2) : "—"}</td>
                                            <td className={tightnessClass(Number(metric.entry_tightness ?? NaN))}>
                                                {Number.isFinite(Number(metric.entry_tightness))
                                                    ? Number(metric.entry_tightness).toFixed(2)
                                                    : "—"}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>

            </div>

            <div className="card">
                <h3>Aktuelle Entry-Tightness je Zweig</h3>
                {latestTightness.length === 0 ? (
                    <p>Noch keine Tightness-Daten verfügbar.</p>
                ) : (
                    <div className="table-wrapper">
                        <table className="table">
                            <thead>
                                <tr>
                                    <th>Strategie</th>
                                    <th>Symbol</th>
                                    <th>Richtung</th>
                                    <th>Bias</th>
                                    <th>Tightness</th>
                                    <th>Zeit</th>
                                </tr>
                            </thead>
                            <tbody>
                                {latestTightness.map((row) => (
                                    <tr key={`${row.strategy}|${row.symbol}|${row.direction}`}>
                                        <td>{row.strategy}</td>
                                        <td>{row.symbol}</td>
                                        <td>{row.direction}</td>
                                        <td>{row.bias ?? "—"}</td>
                                        <td className={tightnessClass(row.tightness)}>{row.tightness.toFixed(2)}</td>
                                        <td>{row.timestamp ? row.timestamp.slice(11, 19) : "—"}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            </section>
        </>
    );
}

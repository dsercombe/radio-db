import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";

import {
  fetchRuns,
  fetchRun,
  fetchStrategyHistory,
  fetchTradeOutcomeSummary,
  fetchTradeOutcomesForStrategy,
  fetchTradeOutcomeDetail,
  type RunMeta,
  type StrategyHistoryEntry,
  type StrategyStat,
  type TradeOutcomeStrategySummary,
  type TradeOutcomeTradeRow,
  type TradeOutcomeTradeDetail,
  deleteRun
} from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";

const PNL_PER_TRADE_THRESHOLD = 0.2;

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

export function AnalyticsPage(): JSX.Element {
  const PAGE_SIZE = 10;
  const [chartRuns, setChartRuns] = useState<RunMeta[]>([]);
  const [runs, setRuns] = useState<RunMeta[]>([]);
  const [runTotal, setRunTotal] = useState<number>(0);
  const [runPage, setRunPage] = useState<number>(1);
  const [runLoading, setRunLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof fetchRun>> | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [outcomeSummary, setOutcomeSummary] = useState<TradeOutcomeStrategySummary[]>([]);
  const [outcomeSummaryLoading, setOutcomeSummaryLoading] = useState<boolean>(false);
  const [outcomeSummaryError, setOutcomeSummaryError] = useState<string | null>(null);
  const [selectedOutcomeStrategy, setSelectedOutcomeStrategy] = useState<string | null>(null);
  const [outcomeTrades, setOutcomeTrades] = useState<TradeOutcomeTradeRow[]>([]);
  const [outcomeTradesLoading, setOutcomeTradesLoading] = useState<boolean>(false);
  const [outcomeTradesError, setOutcomeTradesError] = useState<string | null>(null);
  const [selectedOutcomeTradeId, setSelectedOutcomeTradeId] = useState<number | null>(null);
  const [outcomeTradeDetail, setOutcomeTradeDetail] = useState<TradeOutcomeTradeDetail | null>(null);
  const [outcomeTradeDetailLoading, setOutcomeTradeDetailLoading] = useState<boolean>(false);
  const [outcomeTradeDetailError, setOutcomeTradeDetailError] = useState<string | null>(null);
  const [includeSpecialOutcomeExits, setIncludeSpecialOutcomeExits] = useState<boolean>(false);
  const [includeStrategySimOutcomes, setIncludeStrategySimOutcomes] = useState<boolean>(true);
  const [strategyHistory, setStrategyHistory] = useState<StrategyHistoryEntry[]>([]);
  const [strategyTotal, setStrategyTotal] = useState<number>(0);
  const [runToDelete, setRunToDelete] = useState<RunMeta | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [strategyPage, setStrategyPage] = useState<number>(1);
  const [strategyLoading, setStrategyLoading] = useState<boolean>(true);
  const [strategyError, setStrategyError] = useState<string | null>(null);

  const formatIso = (value?: string | null): string => {
    if (!value) return "—";
    try {
      return new Date(value).toLocaleString();
    } catch {
      return value;
    }
  };

  const formatPct = (value: number): string => `${(value * 100).toFixed(1)}%`;
  const formatNumber = (value: number | null | undefined, digits = 2): string => {
    if (value === null || value === undefined || !Number.isFinite(value)) {
      return "—";
    }
    return value.toFixed(digits);
  };
  const formatMinutes = (value: number | null | undefined): string => {
    if (value === null || value === undefined || !Number.isFinite(value)) {
      return "—";
    }
    if (value >= 60) {
      return `${(value / 60).toFixed(1)}h`;
    }
    return `${value.toFixed(0)}m`;
  };
  const formatRelativeMinutes = (value: number | null | undefined): string => {
    if (value === null || value === undefined || !Number.isFinite(value)) {
      return "—";
    }
    const sign = value < 0 ? "-" : "+";
    const abs = Math.abs(value);
    if (abs >= 60) {
      return `${sign}${(abs / 60).toFixed(1)}h`;
    }
    return `${sign}${abs.toFixed(0)}m`;
  };
  const addMinutesToIso = (isoValue: string | null | undefined, minutes: number | null | undefined): string => {
    if (!isoValue || minutes === null || minutes === undefined || !Number.isFinite(minutes)) {
      return "—";
    }
    const base = new Date(isoValue);
    if (Number.isNaN(base.getTime())) {
      return "—";
    }
    const next = new Date(base.getTime() + minutes * 60 * 1000);
    return next.toLocaleString();
  };

  const mapBacktestBadge = (status?: string): string => {
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

  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetchRuns(100, 0);
        setChartRuns(data.items);
      } catch (err) {
        console.error(err);
        setError("Runs konnten nicht geladen werden.");
      }
    };
    load();
  }, []);

  useEffect(() => {
    const load = async () => {
      try {
        setRunLoading(true);
        const data = await fetchRuns(PAGE_SIZE, (runPage - 1) * PAGE_SIZE);
        setRuns(data.items);
        setRunTotal(data.total);
        setError(null);
      } catch (err) {
        console.error(err);
        setError("Runs konnten nicht geladen werden.");
      } finally {
        setRunLoading(false);
      }
    };
    load();
  }, [runPage]);

  useEffect(() => {
    const load = async () => {
      try {
        setStrategyLoading(true);
        const data = await fetchStrategyHistory(PAGE_SIZE, (strategyPage - 1) * PAGE_SIZE);
        setStrategyHistory(data.items);
        setStrategyTotal(data.total);
        setStrategyError(null);
      } catch (err) {
        console.error(err);
        setStrategyError("Strategiehistorie konnte nicht geladen werden.");
      } finally {
        setStrategyLoading(false);
      }
    };
    load();
  }, [strategyPage]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(runTotal / PAGE_SIZE));
    if (runPage > totalPages) {
      setRunPage(totalPages);
    }
  }, [runTotal, runPage, PAGE_SIZE]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(strategyTotal / PAGE_SIZE));
    if (strategyPage > totalPages) {
      setStrategyPage(totalPages);
    }
  }, [strategyTotal, strategyPage, PAGE_SIZE]);

  useEffect(() => {
    if (!selectedRun) {
      setSummary(null);
      setSummaryError(null);
      setOutcomeSummary([]);
      setOutcomeSummaryError(null);
      setSelectedOutcomeStrategy(null);
      setOutcomeTrades([]);
      setOutcomeTradesError(null);
      setSelectedOutcomeTradeId(null);
      setOutcomeTradeDetail(null);
      setOutcomeTradeDetailError(null);
      return;
    }
    const load = async () => {
      try {
        setOutcomeSummaryLoading(true);
        const [detail, outcomeItems] = await Promise.all([
          fetchRun(selectedRun),
          fetchTradeOutcomeSummary(selectedRun, includeSpecialOutcomeExits, includeStrategySimOutcomes)
        ]);
        setSummary(detail);
        setSummaryError(null);
        setOutcomeSummary(outcomeItems);
        setOutcomeSummaryError(null);
        setSelectedOutcomeStrategy((current) => {
          if (current && outcomeItems.some((item) => item.strategy === current)) {
            return current;
          }
          return outcomeItems[0]?.strategy ?? null;
        });
      } catch (err) {
        console.error(err);
        setSummaryError("Summary konnte nicht geladen werden.");
        setOutcomeSummary([]);
        setOutcomeSummaryError("Post-Exit-Analyse konnte nicht geladen werden.");
      } finally {
        setOutcomeSummaryLoading(false);
      }
    };
    load();
  }, [selectedRun, includeSpecialOutcomeExits, includeStrategySimOutcomes]);

  useEffect(() => {
    if (!selectedRun || !selectedOutcomeStrategy) {
      setOutcomeTrades([]);
      setOutcomeTradesError(null);
      setSelectedOutcomeTradeId(null);
      return;
    }
    const load = async () => {
      try {
        setOutcomeTradesLoading(true);
        const items = await fetchTradeOutcomesForStrategy(
          selectedRun,
          selectedOutcomeStrategy,
          100,
          includeSpecialOutcomeExits,
          includeStrategySimOutcomes
        );
        setOutcomeTrades(items);
        setOutcomeTradesError(null);
        setSelectedOutcomeTradeId((current) => {
          if (current && items.some((item) => item.trade_id === current)) {
            return current;
          }
          return items[0]?.trade_id ?? null;
        });
      } catch (err) {
        console.error(err);
        setOutcomeTrades([]);
        setOutcomeTradesError("Trade-Liste der Post-Exit-Analyse konnte nicht geladen werden.");
      } finally {
        setOutcomeTradesLoading(false);
      }
    };
    load();
  }, [selectedRun, selectedOutcomeStrategy, includeSpecialOutcomeExits, includeStrategySimOutcomes]);

  useEffect(() => {
    if (!selectedRun || selectedOutcomeTradeId === null) {
      setOutcomeTradeDetail(null);
      setOutcomeTradeDetailError(null);
      return;
    }
    const load = async () => {
      try {
        setOutcomeTradeDetailLoading(true);
        const detail = await fetchTradeOutcomeDetail(selectedRun, selectedOutcomeTradeId);
        setOutcomeTradeDetail(detail);
        setOutcomeTradeDetailError(null);
      } catch (err) {
        console.error(err);
        setOutcomeTradeDetail(null);
        setOutcomeTradeDetailError("Trade-Detail der Post-Exit-Analyse konnte nicht geladen werden.");
      } finally {
        setOutcomeTradeDetailLoading(false);
      }
    };
    load();
  }, [selectedRun, selectedOutcomeTradeId]);

  const selectedOutcomeSummary = useMemo(
    () => outcomeSummary.find((item) => item.strategy === selectedOutcomeStrategy) ?? null,
    [outcomeSummary, selectedOutcomeStrategy]
  );

  const outcomePricePathChart = useMemo(() => {
    if (!outcomeTradeDetail?.price_path || outcomeTradeDetail.price_path.length === 0) {
      return null;
    }
    const data = outcomeTradeDetail.price_path.map((point) => ({
      ...point,
      axis_minutes: point.minutes_from_entry,
      price_line: point.marker ? null : point.price,
      marker_price: point.marker ? point.price : null
    }));
    const entryPoint = data.find((point) => point.marker === "entry");
    const exitPoint = data.find((point) => point.marker === "exit");
    const prices = data
      .map((point) => Number(point.price_line ?? point.price))
      .filter((value) => Number.isFinite(value));
    const pnlValues = data
      .flatMap((point) => [Number(point.pnl_from_entry), Number(point.delta_vs_exit)])
      .filter((value) => Number.isFinite(value));
    const buildDomain = (values: number[], paddingRatio: number) => {
      if (values.length === 0) {
        return ["auto", "auto"] as const;
      }
      const min = Math.min(...values);
      const max = Math.max(...values);
      if (min === max) {
        const padding = Math.max(Math.abs(min) * paddingRatio, 1);
        return [min - padding, max + padding] as const;
      }
      const padding = (max - min) * paddingRatio;
      return [min - padding, max + padding] as const;
    };
    return {
      data,
      entryAxis: entryPoint?.axis_minutes ?? 0,
      exitAxis: exitPoint?.axis_minutes ?? 0,
      priceDomain: buildDomain(prices, 0.08),
      pnlDomain: buildDomain(pnlValues, 0.12)
    };
  }, [outcomeTradeDetail]);

  const chartData = useMemo(
    () =>
      chartRuns
        .filter((run) => run.pnl_total !== null)
        .map((run) => ({
          run_id: run.id,
          started_at: run.started_at,
          pnl: run.pnl_total ?? 0,
          drawdown: run.max_drawdown ?? 0
        }))
        .reverse(),
    [chartRuns]
  );

  const totalRunPages = Math.max(1, Math.ceil(runTotal / PAGE_SIZE));
  const totalStrategyPages = Math.max(1, Math.ceil(strategyTotal / PAGE_SIZE));

  const handleDeleteRun = async (run: RunMeta) => {
    setDeleteError(null);
    setDeleting(true);
    try {
      await deleteRun(run.id);
      setRunToDelete(null);
      const data = await fetchRuns(PAGE_SIZE, (runPage - 1) * PAGE_SIZE);
      setRuns(data.items);
      setRunTotal(data.total);
      const chartDataResponse = await fetchRuns(100, 0);
      setChartRuns(chartDataResponse.items);
      const historyResponse = await fetchStrategyHistory(PAGE_SIZE, (strategyPage - 1) * PAGE_SIZE);
      setStrategyHistory(historyResponse.items);
      setStrategyTotal(historyResponse.total);
    } catch (err) {
      console.error(err);
      setDeleteError('Run konnte nicht gelöscht werden.');
    } finally {
      setDeleting(false);
    }
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

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Analytics</h2>
          <p>Vergangene Runs, Strategiekennzahlen und GPT-Auswertungen.</p>
        </div>
      </header>

      {!selectedRun && (
        <>
          {error && <div className="card card--error">{error}</div>}

          <div className="card">
            <h3>Performance-Trend</h3>
            {chartData.length === 0 ? (
              <p>Noch keine Runs vorhanden.</p>
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="started_at" tickFormatter={(value) => value?.slice(5, 16) ?? value} />
                  <YAxis yAxisId="left" stroke="#2563eb" />
                  <YAxis yAxisId="right" orientation="right" stroke="#ef4444" />
                  <Tooltip />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="pnl" stroke="#3b82f6" dot={false} name="PnL" />
                  <Line yAxisId="right" type="monotone" dataKey="drawdown" stroke="#ef4444" dot={false} name="Drawdown" />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>

      <div className="card">
        <h3>Run-Historie</h3>
        {runLoading ? (
          <p>Lade Runs...</p>
        ) : runs.length === 0 ? (
          <p>Noch keine Runs vorhanden.</p>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="table table--clickable">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th>Template</th>
                    <th>Start</th>
                    <th>Ende</th>
                    <th>Laufzeit (min)</th>
                    <th>PnL</th>
                    <th>Drawdown</th>
                    <th>Sharpe</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <div className="table__cell-actions">
                          <button type="button" className="link-button" onClick={() => setSelectedRun(run.id)}>Details</button>
                          <button type="button" className="link-button link-button--danger" onClick={() => setRunToDelete(run)}>Löschen</button>
                        </div>
                        <div>{run.id}</div>
                      </td>
                      <td>
                        <span className={`badge badge--${run.status}`}>{run.status}</span>
                      </td>
                      <td>{run.template_name ?? "—"}</td>
                      <td>{run.started_at ?? "—"}</td>
                      <td>{run.ended_at ?? "—"}</td>
                      <td>{run.runtime_seconds ? (run.runtime_seconds / 60).toFixed(1) : "—"}</td>
                      <td>{run.pnl_total !== null ? run.pnl_total.toFixed(2) : "—"}</td>
                      <td>{run.max_drawdown !== null ? run.max_drawdown.toFixed(2) : "—"}</td>
                      <td>{run.sharpe_ratio !== null ? run.sharpe_ratio.toFixed(2) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {renderPagination(totalRunPages, runPage, setRunPage)}
          </>
        )}
      </div>

      <div className="card">
        <h3>Strategie-Historie</h3>
        {strategyError && <div className="card card--error">{strategyError}</div>}
        {strategyLoading ? (
          <p>Lade Strategie-Statistiken...</p>
        ) : strategyHistory.length === 0 ? (
          <p>Noch keine Strategie-Daten vorhanden.</p>
        ) : (
          <>
            <div className="table-wrapper">
              <table className="table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Start</th>
                    <th>Symbol</th>
                    <th>Strategie</th>
                    <th>Bias</th>
                    <th>PnL</th>
                    <th>Trades</th>
                    <th>Wins</th>
                    <th>Win-Rate</th>
                    <th>PnL/Trade</th>
                  </tr>
                </thead>
                <tbody>
                  {strategyHistory.flatMap((entry) =>
                    entry.stats.length === 0
                      ? [
                          <tr key={`${entry.run.id}-empty`}>
                            <td>{entry.run.id}</td>
                            <td>{entry.run.started_at ?? "—"}</td>
                            <td colSpan={8}>Keine Strategie-Daten gespeichert.</td>
                          </tr>
                        ]
                      : entry.stats.map((stat, index) => (
                          <tr key={`${entry.run.id}-${stat.symbol}-${stat.strategy}-${stat.direction}`}>
                            <td>{index === 0 ? entry.run.id : ""}</td>
                            <td>{index === 0 ? entry.run.started_at ?? "—" : ""}</td>
                            <td>{stat.symbol}</td>
                            <td>{stat.strategy}</td>
                            <td>{stat.direction}</td>
                            <td className={getPnLClass(stat.pnl)}>{stat.pnl.toFixed(2)}</td>
                            <td>{stat.trades}</td>
                            <td>{Math.round(stat.wins ?? stat.win_rate * stat.trades)}</td>
                            <td>{(stat.win_rate * 100).toFixed(1)}%</td>
                            <td className={getPnLClass(stat.pnl_per_trade ?? stat.mean_return)}>
                              {(stat.pnl_per_trade ?? stat.mean_return).toFixed(2)}
                            </td>
                          </tr>
                        ))
                  )}
                </tbody>
              </table>
            </div>
            {renderPagination(totalStrategyPages, strategyPage, setStrategyPage)}
          </>
        )}
      </div>

          {deleteError && <div className="card card--error">{deleteError}</div>}
          {runToDelete && (
            <ConfirmDialog
              open={!!runToDelete}
              title="Run löschen"
              description={(
                <p>Run <strong>{runToDelete.id}</strong> wird endgültig aus der Historie gelöscht. Fortfahren?</p>
              )}
              confirmLabel={deleting ? "Lösche..." : "Run löschen"}
              cancelLabel="Abbrechen"
              onConfirm={() => !deleting && handleDeleteRun(runToDelete)}
              onCancel={() => !deleting && setRunToDelete(null)}
            />
          )}
        </>
      )}
      {selectedRun && (
        <div className="card">
          <div className="page__header">
            <div>
              <h3>Run {selectedRun}</h3>
              <p>Detailanalyse inkl. Performance, Post-Exit und Backtest.</p>
            </div>
            <button type="button" className="button button--ghost" onClick={() => setSelectedRun(null)}>
              Zurück zur Übersicht
            </button>
          </div>
        {summaryError && <div className="card card--error">{summaryError}</div>}
        {summary ? (
          <>
            <div className="card">
              <h4>Metriken</h4>
              <ul className="kv-list">
                <li>
                  <span>PnL</span>
                  <strong>{summary.run.pnl_total !== null ? summary.run.pnl_total.toFixed(2) : "—"}</strong>
                </li>
                <li>
                  <span>Drawdown</span>
                  <strong>{summary.run.max_drawdown !== null ? summary.run.max_drawdown.toFixed(2) : "—"}</strong>
                </li>
                <li>
                  <span>Sharpe</span>
                  <strong>{summary.run.sharpe_ratio !== null ? summary.run.sharpe_ratio.toFixed(2) : "—"}</strong>
                </li>
              </ul>
            </div>
            {(() => {
              const runStats = summary.stats ?? [];
              if (runStats.length === 0) {
                return (
                  <div className="card">
                    <h4>Run Performance</h4>
                    <p>Noch keine Strategie-Trades für diesen Run gespeichert.</p>
                  </div>
                );
              }

              const ensureWins = (stat: StrategyStat): number => stat.wins ?? Math.round(stat.win_rate * stat.trades);

              const aggregate = runStats.reduce(
                (acc, stat) => {
                  acc.pnl += stat.pnl;
                  acc.trades += stat.trades;
                  acc.wins += ensureWins(stat);
                  return acc;
                },
                { pnl: 0, trades: 0, wins: 0 }
              );
              const aggregateWinRate = aggregate.trades ? aggregate.wins / aggregate.trades : 0;
              const aggregatePnlPerTrade = aggregate.trades ? aggregate.pnl / aggregate.trades : 0;

              const groupBy = <T extends { pnl: number; trades: number; wins: number }>(
                keyFn: (stat: StrategyStat) => string,
                seedFn: (stat: StrategyStat) => T
              ) => {
                const map = new Map<string, T>();
                runStats.forEach((stat) => {
                  const key = keyFn(stat);
                  const wins = ensureWins(stat);
                  const base = map.get(key);
                  if (base) {
                    base.pnl += stat.pnl;
                    base.trades += stat.trades;
                    base.wins += wins;
                  } else {
                    const seed = seedFn(stat);
                    seed.pnl += stat.pnl;
                    seed.trades += stat.trades;
                    seed.wins += wins;
                    map.set(key, seed);
                  }
                });
                return Array.from(map.values()).map((item) => ({
                  ...item,
                  win_rate: item.trades ? item.wins / item.trades : 0,
                  pnl_per_trade: item.trades ? item.pnl / item.trades : 0
                }));
              };

              const bySymbolSide = groupBy(
                (stat) => `${stat.symbol}|${stat.direction}`,
                (stat) => ({ symbol: stat.symbol, side: stat.direction, pnl: 0, trades: 0, wins: 0 })
              ).sort((a, b) => (a.symbol === b.symbol ? a.side.localeCompare(b.side) : a.symbol.localeCompare(b.symbol)));

              const byStrategySide = groupBy(
                (stat) => `${stat.strategy}|${stat.direction}`,
                (stat) => ({ strategy: stat.strategy, side: stat.direction, pnl: 0, trades: 0, wins: 0 })
              ).sort((a, b) => (a.strategy === b.strategy ? a.side.localeCompare(b.side) : a.strategy.localeCompare(b.strategy)));

              const bySymbolStrategySide = runStats
                .map((stat) => ({
                  symbol: stat.symbol,
                  strategy: stat.strategy,
                  side: stat.direction,
                  pnl: stat.pnl,
                  trades: stat.trades,
                  wins: ensureWins(stat),
                  win_rate: stat.win_rate,
                  pnl_per_trade: stat.pnl_per_trade ?? stat.mean_return
                }))
                .sort((a, b) => {
                  if (a.symbol !== b.symbol) return a.symbol.localeCompare(b.symbol);
                  if (a.strategy !== b.strategy) return a.strategy.localeCompare(b.strategy);
                  return a.side.localeCompare(b.side);
                });

              return (
                <div className="card">
                  <h4>Run Performance</h4>
                  <ul className="kv-list">
                    <li>
                      <span>PnL</span>
                      <strong className={getPnLClass(aggregate.pnl)}>{aggregate.pnl.toFixed(2)}</strong>
                    </li>
                    <li>
                      <span>Trades</span>
                      <strong>{aggregate.trades}</strong>
                    </li>
                    <li>
                      <span>Win-Rate</span>
                      <strong>{(aggregateWinRate * 100).toFixed(1)}%</strong>
                    </li>
                    <li>
                      <span>PnL/Trade</span>
                      <strong className={getPnLClass(aggregatePnlPerTrade)}>{aggregatePnlPerTrade.toFixed(2)}</strong>
                    </li>
                  </ul>

                  <h5>Pro Symbol &amp; Richtung</h5>
                  <div className="table-wrapper">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Symbol</th>
                          <th>Richtung</th>
                          <th>Trades</th>
                          <th>Wins</th>
                          <th>Win-Rate</th>
                          <th>PnL</th>
                          <th>PnL/Trade</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bySymbolSide.map((row) => (
                          <tr key={`${row.symbol}-${row.side}`}>
                            <td>{row.symbol}</td>
                            <td>{row.side}</td>
                            <td>{row.trades}</td>
                            <td>{Math.round(row.wins)}</td>
                            <td>{(row.win_rate * 100).toFixed(1)}%</td>
                            <td className={getPnLClass(row.pnl)}>{row.pnl.toFixed(2)}</td>
                            <td className={getPnLClass(row.pnl_per_trade)}>{row.pnl_per_trade.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <h5>Pro Strategie &amp; Richtung</h5>
                  <div className="table-wrapper">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Strategie</th>
                          <th>Richtung</th>
                          <th>Trades</th>
                          <th>Wins</th>
                          <th>Win-Rate</th>
                          <th>PnL</th>
                          <th>PnL/Trade</th>
                        </tr>
                      </thead>
                      <tbody>
                        {byStrategySide.map((row) => (
                          <tr key={`${row.strategy}-${row.side}`}>
                            <td>{row.strategy}</td>
                            <td>{row.side}</td>
                            <td>{row.trades}</td>
                            <td>{Math.round(row.wins)}</td>
                            <td>{(row.win_rate * 100).toFixed(1)}%</td>
                            <td className={getPnLClass(row.pnl)}>{row.pnl.toFixed(2)}</td>
                            <td className={getPnLClass(row.pnl_per_trade)}>{row.pnl_per_trade.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <h5>Pro Symbol · Strategie · Richtung</h5>
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
                          <th>PnL</th>
                          <th>PnL/Trade</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bySymbolStrategySide.map((row) => (
                          <tr key={`${row.symbol}-${row.strategy}-${row.side}`}>
                            <td>{row.symbol}</td>
                            <td>{row.strategy}</td>
                            <td>{row.side}</td>
                            <td>{row.trades}</td>
                            <td>{Math.round(row.wins)}</td>
                            <td>{(row.win_rate * 100).toFixed(1)}%</td>
                            <td className={getPnLClass(row.pnl)}>{row.pnl.toFixed(2)}</td>
                            <td className={getPnLClass(row.pnl_per_trade)}>{row.pnl_per_trade.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })()}
            <div className="card">
              <h4>Post-Exit Analyse (12h)</h4>
              <label className="mode-control-toggle" style={{ marginBottom: "0.75rem" }}>
                <input
                  type="checkbox"
                  checked={includeSpecialOutcomeExits}
                  onChange={(event) => setIncludeSpecialOutcomeExits(event.target.checked)}
                />
                Sonder-Exits (`REJECTED`, `BRACKET_FAILED`) einbeziehen
              </label>
              <label className="mode-control-toggle" style={{ marginBottom: "0.75rem", marginLeft: "1rem" }}>
                <input
                  type="checkbox"
                  checked={includeStrategySimOutcomes}
                  onChange={(event) => setIncludeStrategySimOutcomes(event.target.checked)}
                />
                `strategy_sim` einbeziehen
              </label>
              {outcomeSummaryError && <div className="card card--error">{outcomeSummaryError}</div>}
              {outcomeTradesError && <div className="card card--error">{outcomeTradesError}</div>}
              {outcomeTradeDetailError && <div className="card card--error">{outcomeTradeDetailError}</div>}
              {outcomeSummaryLoading ? (
                <p>Lade Post-Exit-Analyse...</p>
              ) : outcomeSummary.length === 0 ? (
                <p>Noch keine geschlossenen Trades mit Outcome-Analyse vorhanden.</p>
              ) : (
                <>
                  <ul className="kv-list">
                    <li>
                      <span>Strategien mit Analyse</span>
                      <strong>{outcomeSummary.length}</strong>
                    </li>
                    <li>
                      <span>Ausgewählte Strategie</span>
                      <strong>{selectedOutcomeSummary?.strategy ?? "—"}</strong>
                    </li>
                    <li>
                      <span>Analysierte Trades</span>
                      <strong>{selectedOutcomeSummary?.analyzed_trades ?? "—"}</strong>
                    </li>
                    <li>
                      <span>12h-Fenster</span>
                      <strong>12h</strong>
                    </li>
                  </ul>

                  <h5>Strategie-Zusammenfassung</h5>
                  <div className="table-wrapper">
                    <table className="table table--clickable">
                      <thead>
                        <tr>
                          <th>Strategie</th>
                          <th>Trades</th>
                          <th>Ø Realized</th>
                          <th>Ø Delta 1h</th>
                          <th>Ø Delta 3h</th>
                          <th>Ø Delta 12h</th>
                          <th>Ø Best Extra</th>
                          <th>Ø Worst Extra</th>
                          <th>Ø Zeit bis Best</th>
                          <th>12h vollständig</th>
                          <th>Ø Coverage</th>
                        </tr>
                      </thead>
                      <tbody>
                        {outcomeSummary.map((item) => {
                          const active = item.strategy === selectedOutcomeStrategy;
                          return (
                            <tr
                              key={item.strategy}
                              className={active ? "table__row--selected" : ""}
                              onClick={() => setSelectedOutcomeStrategy(item.strategy)}
                            >
                              <td>{item.strategy}</td>
                              <td>{item.analyzed_trades}</td>
                              <td className={getPnLClass(item.realized_pnl_avg ?? 0)}>{formatNumber(item.realized_pnl_avg)}</td>
                              <td className={getPnLClass(item.delta_pnl_1h_avg ?? 0)}>{formatNumber(item.delta_pnl_1h_avg)}</td>
                              <td className={getPnLClass(item.delta_pnl_3h_avg ?? 0)}>{formatNumber(item.delta_pnl_3h_avg)}</td>
                              <td className={getPnLClass(item.delta_pnl_12h_avg ?? 0)}>{formatNumber(item.delta_pnl_12h_avg)}</td>
                              <td className={getPnLClass(item.best_extra_pnl_avg ?? 0)}>{formatNumber(item.best_extra_pnl_avg)}</td>
                              <td className={getPnLClass(item.worst_extra_pnl_avg ?? 0)}>{formatNumber(item.worst_extra_pnl_avg)}</td>
                              <td>{formatMinutes(item.time_to_best_minutes_avg)}</td>
                              <td>{item.window_complete_count}/{item.analyzed_trades}</td>
                              <td>{formatPct((item.coverage_pct_avg ?? 0) / 100)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  {selectedOutcomeSummary && (
                    <>
                      <ul className="kv-list">
                        <li>
                          <span>Ø Delta 12h</span>
                          <strong className={getPnLClass(selectedOutcomeSummary.delta_pnl_12h_avg ?? 0)}>
                            {formatNumber(selectedOutcomeSummary.delta_pnl_12h_avg)}
                          </strong>
                        </li>
                        <li>
                          <span>Ø Best Extra</span>
                          <strong className={getPnLClass(selectedOutcomeSummary.best_extra_pnl_avg ?? 0)}>
                            {formatNumber(selectedOutcomeSummary.best_extra_pnl_avg)}
                          </strong>
                        </li>
                        <li>
                          <span>Ø Worst Extra</span>
                          <strong className={getPnLClass(selectedOutcomeSummary.worst_extra_pnl_avg ?? 0)}>
                            {formatNumber(selectedOutcomeSummary.worst_extra_pnl_avg)}
                          </strong>
                        </li>
                        <li>
                          <span>Ø Zeit bis Best</span>
                          <strong>{formatMinutes(selectedOutcomeSummary.time_to_best_minutes_avg)}</strong>
                        </li>
                        <li>
                          <span>Positive Delta 12h</span>
                          <strong>{selectedOutcomeSummary.positive_delta_12h_count}/{selectedOutcomeSummary.analyzed_trades}</strong>
                        </li>
                      </ul>

                      <h5>Trades: {selectedOutcomeSummary.strategy}</h5>
                      {outcomeTradesLoading ? (
                        <p>Lade Outcome-Trades...</p>
                      ) : outcomeTrades.length === 0 ? (
                        <p>Keine analysierten Trades für diese Strategie vorhanden.</p>
                      ) : (
                        <div className="table-wrapper">
                          <table className="table table--clickable">
                            <thead>
                              <tr>
                                <th>Trade</th>
                                <th>Symbol</th>
                                <th>Env</th>
                                <th>Richtung</th>
                                <th>Exit</th>
                                <th>Exit-Zeit</th>
                                <th>Best @</th>
                                <th>Worst @</th>
                                <th>Realized</th>
                                <th>Delta 1h</th>
                                <th>Delta 3h</th>
                                <th>Delta 12h</th>
                                <th>Best Extra</th>
                                <th>12h vollständig</th>
                                <th>Coverage</th>
                              </tr>
                            </thead>
                            <tbody>
                              {outcomeTrades.map((trade) => {
                                const active = trade.trade_id === selectedOutcomeTradeId;
                                const bestExtra = trade.best_pnl_after_exit - trade.realized_pnl;
                                const bestAt = addMinutesToIso(trade.exit_timestamp, trade.time_to_best_minutes);
                                const worstAt = addMinutesToIso(trade.exit_timestamp, trade.time_to_worst_minutes);
                                return (
                                  <tr
                                    key={trade.trade_id}
                                    className={active ? "table__row--selected" : ""}
                                    onClick={() => setSelectedOutcomeTradeId(trade.trade_id)}
                                  >
                                    <td>{trade.trade_id}</td>
                                    <td>{trade.symbol}</td>
                                    <td>{trade.environment}</td>
                                    <td>{trade.direction}</td>
                                    <td>{trade.exit_reason ?? "—"}</td>
                                    <td>{formatIso(trade.exit_timestamp)}</td>
                                    <td>{bestAt}</td>
                                    <td>{worstAt}</td>
                                    <td className={getPnLClass(trade.realized_pnl)}>{trade.realized_pnl.toFixed(2)}</td>
                                    <td className={getPnLClass(trade.delta_pnl_1h ?? 0)}>
                                      {trade.delta_pnl_1h !== null ? trade.delta_pnl_1h.toFixed(2) : "—"}
                                    </td>
                                    <td className={getPnLClass(trade.delta_pnl_3h ?? 0)}>
                                      {trade.delta_pnl_3h !== null ? trade.delta_pnl_3h.toFixed(2) : "—"}
                                    </td>
                                    <td className={getPnLClass(trade.delta_pnl_12h ?? 0)}>
                                      {trade.delta_pnl_12h !== null ? trade.delta_pnl_12h.toFixed(2) : "—"}
                                    </td>
                                    <td className={getPnLClass(bestExtra)}>{bestExtra.toFixed(2)}</td>
                                    <td>{trade.bars_available !== null && trade.bars_expected !== null && trade.bars_available >= trade.bars_expected ? "ja" : "nein"}</td>
                                    <td>
                                      {formatPct((trade.coverage_pct ?? 0) / 100)}
                                      {" "}
                                      ({trade.bars_available ?? 0}/{trade.bars_expected ?? 0})
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}

                      <h5>Trade-Detail</h5>
                      {outcomeTradeDetailLoading ? (
                        <p>Lade Trade-Detail...</p>
                      ) : !outcomeTradeDetail ? (
                        <p>Trade auswählen, um den 12h-Verlauf nach Exit zu sehen.</p>
                      ) : (
                        <>
                          <ul className="kv-list">
                            <li>
                              <span>Trade</span>
                              <strong>{outcomeTradeDetail.trade_id}</strong>
                            </li>
                            <li>
                              <span>Symbol</span>
                              <strong>{outcomeTradeDetail.symbol}</strong>
                            </li>
                            <li>
                              <span>Entry-Zeit</span>
                              <strong>{formatIso(outcomeTradeDetail.timestamp)}</strong>
                            </li>
                            <li>
                              <span>Entry-Preis</span>
                              <strong>{formatNumber(outcomeTradeDetail.entry_price, 4)}</strong>
                            </li>
                            <li>
                              <span>Exit-Zeit</span>
                              <strong>{formatIso(outcomeTradeDetail.exit_timestamp)}</strong>
                            </li>
                            <li>
                              <span>Exit-Preis</span>
                              <strong>{formatNumber(outcomeTradeDetail.exit_price, 4)}</strong>
                            </li>
                            <li>
                              <span>Realized</span>
                              <strong className={getPnLClass(outcomeTradeDetail.realized_pnl)}>
                                {outcomeTradeDetail.realized_pnl.toFixed(2)}
                              </strong>
                            </li>
                            <li>
                              <span>Best Extra</span>
                              <strong
                                className={getPnLClass(
                                  outcomeTradeDetail.best_pnl_after_exit - outcomeTradeDetail.realized_pnl
                                )}
                              >
                                {(outcomeTradeDetail.best_pnl_after_exit - outcomeTradeDetail.realized_pnl).toFixed(2)}
                              </strong>
                            </li>
                            <li>
                              <span>Worst Extra</span>
                              <strong
                                className={getPnLClass(
                                  outcomeTradeDetail.worst_pnl_after_exit - outcomeTradeDetail.realized_pnl
                                )}
                              >
                                {(outcomeTradeDetail.worst_pnl_after_exit - outcomeTradeDetail.realized_pnl).toFixed(2)}
                              </strong>
                            </li>
                            <li>
                              <span>Zeit bis Best</span>
                              <strong>{formatMinutes(outcomeTradeDetail.time_to_best_minutes)}</strong>
                            </li>
                            <li>
                              <span>Best @</span>
                              <strong>{addMinutesToIso(outcomeTradeDetail.exit_timestamp, outcomeTradeDetail.time_to_best_minutes)}</strong>
                            </li>
                            <li>
                              <span>Zeit bis Worst</span>
                              <strong>{formatMinutes(outcomeTradeDetail.time_to_worst_minutes)}</strong>
                            </li>
                            <li>
                              <span>Worst @</span>
                              <strong>{addMinutesToIso(outcomeTradeDetail.exit_timestamp, outcomeTradeDetail.time_to_worst_minutes)}</strong>
                            </li>
                            <li>
                              <span>Coverage</span>
                              <strong>
                                {formatPct((outcomeTradeDetail.coverage_pct ?? 0) / 100)}
                                {" "}
                                ({outcomeTradeDetail.bars_available ?? 0}/{outcomeTradeDetail.bars_expected ?? 0})
                              </strong>
                            </li>
                            <li>
                              <span>Verfügbar bis</span>
                              <strong>{formatIso(outcomeTradeDetail.available_until)}</strong>
                            </li>
                          </ul>

                          {!outcomePricePathChart ? (
                            <p>Für diesen Trade sind noch keine Preisverlaufsdaten im Cache vorhanden.</p>
                          ) : (
                            <ResponsiveContainer width="100%" height={360}>
                              <LineChart data={outcomePricePathChart.data}>
                                <CartesianGrid strokeDasharray="3 3" />
                                <XAxis
                                  dataKey="axis_minutes"
                                  tickFormatter={(value) => formatRelativeMinutes(Number(value))}
                                />
                                <YAxis
                                  yAxisId="left"
                                  stroke="#2563eb"
                                  domain={outcomePricePathChart.priceDomain}
                                  tickFormatter={(value) => formatNumber(Number(value), 2)}
                                  width={90}
                                />
                                <YAxis
                                  yAxisId="right"
                                  orientation="right"
                                  stroke="#16a34a"
                                  domain={outcomePricePathChart.pnlDomain}
                                  tickFormatter={(value) => formatNumber(Number(value), 2)}
                                  width={70}
                                />
                                <ReferenceLine
                                  x={outcomePricePathChart.entryAxis}
                                  yAxisId="left"
                                  stroke="#0f766e"
                                  strokeDasharray="4 4"
                                  label="Entry"
                                />
                                <ReferenceLine
                                  x={outcomePricePathChart.exitAxis}
                                  yAxisId="left"
                                  stroke="#b45309"
                                  strokeDasharray="4 4"
                                  label="Exit"
                                />
                                <Tooltip
                                  formatter={(value: number, name: string) => [
                                    Number.isFinite(value) ? value.toFixed(2) : value,
                                    name === "price"
                                      ? "Preis"
                                      : name === "pnl_from_entry"
                                        ? "Hypothetischer PnL"
                                        : "Delta vs Exit"
                                  ]}
                                  labelFormatter={(_, payload) => {
                                    const point = payload?.[0]?.payload;
                                    if (!point) return "";
                                    const phase =
                                      point.marker === "entry"
                                        ? "Entry"
                                        : point.marker === "exit"
                                          ? "Exit"
                                          : point.phase === "pre_entry"
                                            ? "Vor Entry"
                                            : point.phase === "in_trade"
                                              ? "Im Trade"
                                              : "Nach Exit";
                                    return `${formatIso(point.timestamp)} · ${phase} · ${formatRelativeMinutes(point.axis_minutes)}`;
                                  }}
                                />
                                <Legend />
                                <Line
                                  yAxisId="left"
                                  type="monotone"
                                  dataKey="price_line"
                                  stroke="#2563eb"
                                  dot={false}
                                  connectNulls
                                  name="price"
                                />
                                <Line
                                  yAxisId="left"
                                  type="monotone"
                                  dataKey="marker_price"
                                  stroke="#dc2626"
                                  strokeWidth={0}
                                  dot={{ r: 4 }}
                                  connectNulls={false}
                                  name="entry_exit_markers"
                                />
                                <Line
                                  yAxisId="right"
                                  type="monotone"
                                  dataKey="pnl_from_entry"
                                  stroke="#16a34a"
                                  dot={false}
                                  name="pnl_from_entry"
                                />
                                <Line
                                  yAxisId="right"
                                  type="monotone"
                                  dataKey="delta_vs_exit"
                                  stroke="#f59e0b"
                                  dot={false}
                                  name="delta_vs_exit"
                                />
                              </LineChart>
                            </ResponsiveContainer>
                          )}

                          <div className="table-wrapper">
                            <table className="table">
                              <thead>
                                <tr>
                                  <th>Fenster</th>
                                  <th>Delta vs Exit</th>
                                </tr>
                              </thead>
                              <tbody>
                                <tr>
                                  <td>1h</td>
                                  <td className={getPnLClass(outcomeTradeDetail.delta_pnl_1h ?? 0)}>
                                    {outcomeTradeDetail.delta_pnl_1h !== null ? outcomeTradeDetail.delta_pnl_1h.toFixed(2) : "—"}
                                  </td>
                                </tr>
                                <tr>
                                  <td>3h</td>
                                  <td className={getPnLClass(outcomeTradeDetail.delta_pnl_3h ?? 0)}>
                                    {outcomeTradeDetail.delta_pnl_3h !== null ? outcomeTradeDetail.delta_pnl_3h.toFixed(2) : "—"}
                                  </td>
                                </tr>
                                <tr>
                                  <td>6h</td>
                                  <td className={getPnLClass(outcomeTradeDetail.delta_pnl_6h ?? 0)}>
                                    {outcomeTradeDetail.delta_pnl_6h !== null ? outcomeTradeDetail.delta_pnl_6h.toFixed(2) : "—"}
                                  </td>
                                </tr>
                                <tr>
                                  <td>12h</td>
                                  <td className={getPnLClass(outcomeTradeDetail.delta_pnl_12h ?? 0)}>
                                    {outcomeTradeDetail.delta_pnl_12h !== null ? outcomeTradeDetail.delta_pnl_12h.toFixed(2) : "—"}
                                  </td>
                                </tr>
                              </tbody>
                            </table>
                          </div>
                        </>
                      )}
                    </>
                  )}
                </>
              )}
            </div>
            <div className="card">
              <h4>GPT Summary</h4>
              {summary.summary ? <p>{summary.summary["summary"]}</p> : <p>Noch keine Summary vorhanden.</p>}
            </div>
            <div className="card">
              <h4>Backtest (letzte 90 Tage)</h4>
              {(() => {
                const backtestSnapshot = summary.backtest;
                if (!backtestSnapshot) {
                  return <p>Noch keine Backtest-Auswertung vorhanden.</p>;
                }
                const details = backtestSnapshot.summary;
                const aggregate = (details as any)?.summary_normal ?? details?.summary;
                const symbolStrategySide =
                  (details as any)?.by_symbol_strategy_side_all ??
                  details?.by_symbol_strategy_side ??
                  [];
                const symbolStrategySideLive = details?.by_symbol_strategy_side ?? [];
                const liveMap = new Map(
                  symbolStrategySideLive.map((row) => [
                    `${row.symbol ?? ""}||${row.strategy ?? ""}||${row.side ?? ""}`,
                    row
                  ])
                );
                const aggregatePnlPerTrade = aggregate && aggregate.trades ? aggregate.pnl / aggregate.trades : 0;

                return (
                  <>
                    <ul className="kv-list">
                      <li>
                        <span>Status</span>
                        <strong className={`badge ${mapBacktestBadge(backtestSnapshot.status)}`}>
                          {backtestSnapshot.status}
                        </strong>
                      </li>
                      <li>
                        <span>Zeitraum</span>
                        <strong>
                          {backtestSnapshot.window_start?.slice(0, 10) ?? "—"} &rarr;{" "}
                          {backtestSnapshot.window_end?.slice(0, 10) ?? "—"}
                        </strong>
                      </li>
                      <li>
                        <span>Gestartet</span>
                        <strong>{formatIso(backtestSnapshot.started_at)}</strong>
                      </li>
                      <li>
                        <span>Fertig</span>
                        <strong>{formatIso(backtestSnapshot.completed_at)}</strong>
                      </li>
                    </ul>

                    {backtestSnapshot.status === "completed" && aggregate ? (
                      <>
                        <ul className="kv-list">
                          <li>
                            <span>PnL</span>
                            <strong className={getPnLClass(aggregate.pnl)}>{aggregate.pnl.toFixed(2)}</strong>
                          </li>
                          <li>
                            <span>Trades</span>
                            <strong>{aggregate.trades}</strong>
                          </li>
                          <li>
                            <span>Win-Rate</span>
                            <strong>{formatPct(aggregate.win_rate)}</strong>
                          </li>
                          <li>
                            <span>PnL/Trade</span>
                            <strong className={getPnLClass(aggregatePnlPerTrade)}>{aggregatePnlPerTrade.toFixed(2)}</strong>
                          </li>
                        </ul>

                        <h5>Pro Symbol · Strategie · Richtung</h5>
                        {symbolStrategySide.length === 0 ? (
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
                                  <th>PnL</th>
                                  <th>Fees</th>
                                  <th>PnL/Trade</th>
                                  <th>Live Trades</th>
                                  <th>Live PnL</th>
                                  <th>Live PnL/Trade</th>
                                </tr>
                              </thead>
                              <tbody>
                                {symbolStrategySide.map((row, index) => {
                                  const pnlPerTrade = row.trades ? row.pnl / row.trades : 0;
                                  const liveRow = liveMap.get(
                                    `${row.symbol ?? ""}||${row.strategy ?? ""}||${row.side ?? ""}`
                                  );
                                  const livePnl = liveRow?.pnl ?? 0;
                                  const livePnlPerTrade = liveRow?.trades ? livePnl / liveRow.trades : 0;
                                  return (
                                    <tr key={`${row.symbol}-${row.strategy}-${row.side}-${index}`}>
                                      <td>{row.symbol}</td>
                                      <td>{row.strategy}</td>
                                      <td>{row.side}</td>
                                      <td>{row.trades}</td>
                                      <td>{row.wins}</td>
                                      <td>{formatPct(row.win_rate)}</td>
                                      <td className={getPnLClass(row.pnl)}>{row.pnl.toFixed(2)}</td>
                                      <td>{row.fees.toFixed(2)}</td>
                                      <td className={getPnLClass(pnlPerTrade)}>{pnlPerTrade.toFixed(2)}</td>
                                      <td>{liveRow?.trades ?? 0}</td>
                                      <td className={getPnLClass(livePnl)}>{livePnl.toFixed(2)}</td>
                                      <td className={getPnLClass(livePnlPerTrade)}>{livePnlPerTrade.toFixed(2)}</td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </>
                    ) : backtestSnapshot.status === "failed" ? (
                      <p>Backtest fehlgeschlagen: {backtestSnapshot.error ?? "Unbekannter Fehler"}</p>
                    ) : (
                      <p>Backtest läuft – Ergebnisse folgen automatisch.</p>
                    )}
                  </>
                );
              })()}
            </div>
          </>
        ) : (
          <p>Lade Details...</p>
        )}
        </div>
      )}
    </section>
  );
}

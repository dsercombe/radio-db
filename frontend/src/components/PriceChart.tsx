import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ComposedChart,
  Customized,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { fetchPriceHistory, type PriceHistoryResponse } from "../api/client";
import { useVisibilityPolling } from "../hooks/useVisibilityPolling";

interface PriceChartProps {
  symbols?: string[];
  months?: number;
}

export function PriceChart({ symbols = ["BTCUSDT", "ETHUSDT"], months = 3 }: PriceChartProps): JSX.Element {
  const [data, setData] = useState<PriceHistoryResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [intervalBySymbol, setIntervalBySymbol] = useState<Record<string, string>>({});
  const [spanBySymbol, setSpanBySymbol] = useState<Record<string, string>>({});
  const [ready, setReady] = useState<boolean>(false);
  const [inView, setInView] = useState<boolean>(true);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const loadingRef = useRef<boolean>(false);
  const MAX_CANDLES = 900;

  // Initialisiere Interval/Span Defaults pro Symbol
  useEffect(() => {
    setIntervalBySymbol((prev) => {
      const next = { ...prev };
      symbols.forEach((symbol) => {
        if (!next[symbol]) {
          next[symbol] = "5m";
        }
      });
      return next;
    });
    setSpanBySymbol((prev) => {
      const next = { ...prev };
      symbols.forEach((symbol) => {
        if (!next[symbol]) {
          next[symbol] = "12h";
        }
      });
      return next;
    });
  }, [symbols]);

  const loadData = useCallback(async (force: boolean = false) => {
    if (loadingRef.current && !force) return; // verhindert parallele Loads bei vielen Re-Renders
    try {
      loadingRef.current = true;
      setLoading(true);
      setError(null);
      const promises = symbols.map((symbol) =>
        fetchPriceHistory(
          symbol,
          months,
          intervalBySymbol[symbol] || "1h",
          spanBySymbol[symbol] || "m",
          MAX_CANDLES
        )
      );
      const results = await Promise.all(promises);
      setData(results);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Preisdaten");
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, [symbols, months, intervalBySymbol, spanBySymbol]);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: number | null = null;
    let idleId: number | null = null;

    const markReady = () => {
      if (!cancelled) {
        setReady(true);
      }
    };

    const scheduleIdle = () => {
      const idle = (window as Window & {
        requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
        cancelIdleCallback?: (id: number) => void;
      });
      if (idle.requestIdleCallback) {
        idleId = idle.requestIdleCallback(markReady, { timeout: 1500 });
      } else {
        timeoutId = window.setTimeout(markReady, 1200);
      }
    };

    if (document.visibilityState === "visible") {
      scheduleIdle();
    } else {
      const onVisible = () => {
        if (document.visibilityState === "visible") {
          scheduleIdle();
          document.removeEventListener("visibilitychange", onVisible);
        }
      };
      document.addEventListener("visibilitychange", onVisible);
    }

    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      if (idleId !== null) {
        const idle = (window as Window & { cancelIdleCallback?: (id: number) => void });
        if (idle.cancelIdleCallback) {
          idle.cancelIdleCallback(idleId);
        }
      }
    };
  }, []);

  useEffect(() => {
    const node = containerRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.length > 0) {
          setInView(entries[0].isIntersecting);
        }
      },
      { rootMargin: "200px" }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (ready && inView) {
      void loadData();
    }
  }, [ready, inView, loadData]);

  useVisibilityPolling(loadData, {
    intervalMs: 30 * 60 * 1000,
    initialDelayMs: 0,
    backoffFactor: 2,
    maxIntervalMs: 60 * 60 * 1000,
    enabled: ready && inView,
  });

  const dataBySymbol = useMemo(() => {
    const map = new Map<string, PriceHistoryResponse>();
    data.forEach((item) => {
      map.set(item.symbol, item);
    });
    return map;
  }, [data]);

  // Formatierung für Tooltip
  const formatTimestamp = (value: string) => {
    try {
      const date = new Date(value);
      return date.toLocaleString("de-DE", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch {
      return value;
    }
  };

  const formatPrice = (value: number) => {
    return new Intl.NumberFormat("de-DE", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }).format(value);
  };

  const colors: Record<string, { up: string; down: string }> = {
    BTCUSDT: { up: "#18b26b", down: "#e34d4d" },
    ETHUSDT: { up: "#3f6edc", down: "#d03f96" },
  };

  if (!ready || !inView) {
    return (
      <div className="card" ref={containerRef} style={{ padding: "20px", textAlign: "center" }}>
        Preisdaten werden geladen, sobald der Bereich sichtbar ist...
      </div>
    );
  }

  if (loading && data.length === 0) {
    return (
      <div className="card" ref={containerRef} style={{ padding: "20px", textAlign: "center" }}>
        Lade Preisdaten...
      </div>
    );
  }

  if (error) {
    return (
      <div className="card" ref={containerRef} style={{ padding: "20px", color: "#c00" }}>
        <strong>Fehler:</strong> {error}
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="card" ref={containerRef} style={{ padding: "20px", textAlign: "center" }}>
        Keine Preisdaten verfügbar.
      </div>
    );
  }

  return (
    <div ref={containerRef} className="price-charts-grid">
      {symbols.map((symbol) => {
        const response = dataBySymbol.get(symbol);
        const chartData = response?.data ?? [];
        const color = colors[symbol] || { up: "#19b26b", down: "#e05252" };
        const intervalValue = intervalBySymbol[symbol] || "5m";
        const spanValue = spanBySymbol[symbol] || "12h";

        const CandleLayer = (props: any) => {
          const { xAxisMap, yAxisMap, data: rawData } = props;
          if (!xAxisMap || !yAxisMap) return null;
          const xAxis = Object.values(xAxisMap)[0] as any;
          const yAxis = Object.values(yAxisMap)[0] as any;
          const xScale = xAxis?.scale;
          const yScale = yAxis?.scale;
          if (!xScale || !yScale) return null;
          const bandwidth = xScale.bandwidth ? xScale.bandwidth() : 10;
          const bodyWidth = Math.max(1, Math.min(14, bandwidth > 1 ? bandwidth - 1 : bandwidth));

          return (
            <g>
              {(rawData || []).map((entry: any, index: number) => {
                const x = xScale(entry.timestamp);
                if (x === undefined || x === null) return null;
                const open = Number(entry.open);
                const close = Number(entry.close);
                const high = Number(entry.high);
                const low = Number(entry.low);
                const isUp = close >= open;
                const stroke = isUp ? color.up : color.down;
                const centerX = x + bandwidth / 2;
                const yOpen = yScale(open);
                const yClose = yScale(close);
                const yHigh = yScale(high);
                const yLow = yScale(low);
                const bodyTop = Math.min(yOpen, yClose);
                const bodyBottom = Math.max(yOpen, yClose);
                const bodyHeight = Math.max(1, bodyBottom - bodyTop);
                return (
                  <g key={`candle-${symbol}-${index}`}>
                    <line x1={centerX} x2={centerX} y1={yHigh} y2={yLow} stroke={stroke} strokeWidth={1} />
                    <rect
                      x={centerX - bodyWidth / 2}
                      y={bodyTop}
                      width={bodyWidth}
                      height={bodyHeight}
                      fill={stroke}
                      stroke={stroke}
                    />
                  </g>
                );
              })}
            </g>
          );
        };

        const tooltipContent = ({ active, payload }: any) => {
          if (!active || !payload?.length) return null;
          const row = payload[0]?.payload;
          if (!row) return null;
          return (
            <div style={{ backgroundColor: "#fff", border: "1px solid #ccc", borderRadius: "6px", padding: "8px" }}>
              <div style={{ fontWeight: 600, marginBottom: "4px" }}>{formatTimestamp(row.timestamp)}</div>
              <div>O: {formatPrice(Number(row.open))}</div>
              <div>H: {formatPrice(Number(row.high))}</div>
              <div>L: {formatPrice(Number(row.low))}</div>
              <div>C: {formatPrice(Number(row.close))}</div>
            </div>
          );
        };

        return (
          <div key={symbol} className="card" style={{ marginBottom: "1.5rem", minWidth: 0 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap", alignItems: "center" }}>
              <div>
                <h3 style={{ marginBottom: "0.25rem" }}>{symbol.replace("USDT", "")} Preis</h3>
                <div style={{ fontSize: "0.85em", opacity: 0.7 }}>
                  Intervall: {response?.interval ?? intervalValue}
                </div>
              </div>
              <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
                <label style={{ display: "flex", flexDirection: "column", fontSize: "0.85em" }}>
                  Candle Intervall
                  <select
                    value={intervalValue}
                    onChange={(event) => {
                      const value = event.target.value;
                      setIntervalBySymbol((prev) => ({ ...prev, [symbol]: value }));
                    }}
                  >
                    <option value="1d">1d</option>
                    <option value="3h">3h</option>
                    <option value="1h">1h</option>
                    <option value="5m">5m</option>
                    <option value="1m">1m</option>
                  </select>
                </label>
                <label style={{ display: "flex", flexDirection: "column", fontSize: "0.85em" }}>
                  Zeitraum
                  <select
                    value={spanValue}
                    onChange={(event) => {
                      const value = event.target.value;
                      setSpanBySymbol((prev) => ({ ...prev, [symbol]: value }));
                    }}
                  >
                    <option value="all">all</option>
                    <option value="y">y</option>
                    <option value="m">m</option>
                    <option value="w">w</option>
                    <option value="d">d</option>
                    <option value="12h">12h</option>
                    <option value="1h">1h</option>
                  </select>
                </label>
              </div>
            </div>
            {chartData.length === 0 ? (
              <p style={{ marginTop: "1rem" }}>Keine Preisdaten verfügbar.</p>
            ) : (
              <ResponsiveContainer width="100%" height={360}>
                <ComposedChart data={chartData} margin={{ top: 10, right: 30, left: 20, bottom: 40 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                  <XAxis
                    dataKey="timestamp"
                    tickFormatter={formatTimestamp}
                    angle={-35}
                    textAnchor="end"
                    height={60}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tickFormatter={formatPrice}
                    domain={["auto", "auto"]}
                    label={{ value: "Preis (USDT)", angle: -90, position: "insideLeft" }}
                  />
                  <Tooltip content={tooltipContent} />
                  <Line dataKey="close" stroke="rgba(0,0,0,0)" dot={false} activeDot={{ r: 4 }} />
                  <Customized component={CandleLayer} />
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </div>
        );
      })}
    </div>
  );
}

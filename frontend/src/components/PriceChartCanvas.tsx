import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchPriceHistory, type PriceHistoryResponse } from "../api/client";
import { useVisibilityPolling } from "../hooks/useVisibilityPolling";

interface PriceChartCanvasProps {
  symbols?: string[];
  months?: number;
}

type CandlePoint = {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
};

const DEFAULT_INTERVAL = "5m";
const DEFAULT_SPAN = "12h";
const MAX_CANDLES = 600;

const colors: Record<string, { up: string; down: string; label: string }> = {
  BTCUSDT: { up: "#18b26b", down: "#e34d4d", label: "BTC" },
  ETHUSDT: { up: "#3f6edc", down: "#d03f96", label: "ETH" },
};

export function PriceChartCanvas({ symbols = ["BTCUSDT", "ETHUSDT"], months = 3 }: PriceChartCanvasProps): JSX.Element {
  const [data, setData] = useState<PriceHistoryResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [intervalBySymbol, setIntervalBySymbol] = useState<Record<string, string>>({});
  const [spanBySymbol, setSpanBySymbol] = useState<Record<string, string>>({});
  const [ready, setReady] = useState<boolean>(false);
  const [inView, setInView] = useState<boolean>(true);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pendingRefreshRef = useRef<boolean>(false);
  const loadingRef = useRef<boolean>(false);

  useEffect(() => {
    setIntervalBySymbol((prev) => {
      let changed = false;
      const next = { ...prev };
      symbols.forEach((symbol) => {
        if (!next[symbol]) {
          next[symbol] = DEFAULT_INTERVAL;
          changed = true;
        }
      });
      return changed ? next : prev;
    });
    setSpanBySymbol((prev) => {
      let changed = false;
      const next = { ...prev };
      symbols.forEach((symbol) => {
        if (!next[symbol]) {
          next[symbol] = DEFAULT_SPAN;
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [symbols]);

  const loadData = useCallback(async () => {
    if (loadingRef.current) {
      pendingRefreshRef.current = true;
      return;
    }
    try {
      loadingRef.current = true;
      setLoading(true);
      setError(null);
      const withTimeout = async <T,>(promise: Promise<T>, ms: number): Promise<T> => {
        return await new Promise<T>((resolve, reject) => {
          const timer = window.setTimeout(() => reject(new Error("Price history timeout")), ms);
          promise
            .then((value) => {
              window.clearTimeout(timer);
              resolve(value);
            })
            .catch((err) => {
              window.clearTimeout(timer);
              reject(err);
            });
        });
      };
      const results = await Promise.allSettled(
        symbols.map((symbol) =>
          withTimeout(
            fetchPriceHistory(
              symbol,
              months,
              intervalBySymbol[symbol] || DEFAULT_INTERVAL,
              spanBySymbol[symbol] || DEFAULT_SPAN,
              MAX_CANDLES
            ),
            25000
          )
        )
      );
      const ok: PriceHistoryResponse[] = [];
      results.forEach((res, idx) => {
        if (res.status === "fulfilled") {
          ok.push(res.value);
        } else {
          console.warn("Price history failed:", symbols[idx], res.reason);
        }
      });
      if (!ok.length) {
        setError("Keine Preisdaten verfügbar oder Timeout.");
      }
      setData(ok);
      setLastUpdated(new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Preisdaten");
    } finally {
      loadingRef.current = false;
      setLoading(false);
      if (pendingRefreshRef.current) {
        pendingRefreshRef.current = false;
        window.setTimeout(() => {
          void loadData();
        }, 0);
      }
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

    const idle = (window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    });
    if (idle.requestIdleCallback) {
      idleId = idle.requestIdleCallback(markReady, { timeout: 800 });
    } else {
      timeoutId = window.setTimeout(markReady, 600);
    }

    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      if (idleId !== null && idle.cancelIdleCallback) {
        idle.cancelIdleCallback(idleId);
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
  }, [ready, inView, intervalBySymbol, spanBySymbol, loadData]);

  useVisibilityPolling(loadData, {
    intervalMs: 15 * 60 * 1000,
    initialDelayMs: 0,
    backoffFactor: 2,
    maxIntervalMs: 30 * 60 * 1000,
    enabled: ready && inView,
  });

  const dataBySymbol = useMemo(() => {
    const map = new Map<string, CandlePoint[]>();
    data.forEach((response) => {
      const normalized: CandlePoint[] = response.data.map((point) => ({
        timestamp: new Date(point.timestamp).getTime(),
        open: Number(point.open),
        high: Number(point.high),
        low: Number(point.low),
        close: Number(point.close),
      }));
      map.set(response.symbol, normalized);
    });
    return map;
  }, [data]);

  if (!ready || !inView) {
    return (
      <div className="card" ref={containerRef} style={{ padding: "20px", textAlign: "center" }}>
        Canvas-Charts laden, sobald der Bereich sichtbar ist...
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

  return (
    <div ref={containerRef}>
      <div className="card" style={{ marginBottom: "1.5rem" }}>
        <h3 style={{ marginBottom: "0.25rem" }}>Canvas-Preischarts (Beta)</h3>
        <p style={{ margin: 0, opacity: 0.7, fontSize: "0.85em" }}>
          Schnelleres Rendering für lange Zeiträume. Gleiche Logik wie oben.
        </p>
        <p style={{ margin: "0.35rem 0 0", opacity: 0.6, fontSize: "0.8em" }}>
          {loading ? "Lade..." : lastUpdated ? `Stand: ${lastUpdated}` : "—"}
        </p>
      </div>
      <div className="price-charts-grid">
        {symbols.map((symbol) => {
          const series = dataBySymbol.get(symbol) ?? [];
          const color = colors[symbol] || { up: "#18b26b", down: "#e34d4d", label: symbol };
          const intervalValue = intervalBySymbol[symbol] || DEFAULT_INTERVAL;
          const spanValue = spanBySymbol[symbol] || DEFAULT_SPAN;
          return (
            <CanvasPanel
              key={`canvas-${symbol}`}
              symbol={symbol}
              label={color.label}
              series={series}
              colorUp={color.up}
              colorDown={color.down}
              intervalValue={intervalValue}
              spanValue={spanValue}
              onIntervalChange={(value) => setIntervalBySymbol((prev) => ({ ...prev, [symbol]: value }))}
              onSpanChange={(value) => setSpanBySymbol((prev) => ({ ...prev, [symbol]: value }))}
              loading={loading}
            />
          );
        })}
      </div>
    </div>
  );
}

interface CanvasPanelProps {
  symbol: string;
  label: string;
  series: CandlePoint[];
  colorUp: string;
  colorDown: string;
  intervalValue: string;
  spanValue: string;
  onIntervalChange: (value: string) => void;
  onSpanChange: (value: string) => void;
  loading: boolean;
}

function CanvasPanel(props: CanvasPanelProps): JSX.Element {
  const {
    symbol,
    label,
    series,
    colorUp,
    colorDown,
    intervalValue,
    spanValue,
    onIntervalChange,
    onSpanChange,
    loading,
  } = props;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ width: number; height: number }>({ width: 600, height: 360 });

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const update = () => {
      const rect = node.getBoundingClientRect();
      setSize({
        width: Math.max(300, Math.floor(rect.width)),
        height: 340,
      });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const { width, height } = size;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);

    if (!series.length) {
      ctx.fillStyle = "#888";
      ctx.font = "14px sans-serif";
      ctx.fillText("Keine Preisdaten verfügbar.", 16, 24);
      return;
    }

    const padding = { top: 20, right: 20, bottom: 30, left: 50 };
    const innerW = width - padding.left - padding.right;
    const innerH = height - padding.top - padding.bottom;
    const highs = series.map((d) => d.high);
    const lows = series.map((d) => d.low);
    const maxVal = Math.max(...highs);
    const minVal = Math.min(...lows);
    const range = maxVal - minVal || 1;

    // Grid
    ctx.strokeStyle = "#f0f0f0";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = padding.top + (innerH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();
    }

    // Y axis labels
    ctx.fillStyle = "#555";
    ctx.font = "12px sans-serif";
    for (let i = 0; i <= 4; i += 1) {
      const value = maxVal - (range / 4) * i;
      const y = padding.top + (innerH / 4) * i;
      ctx.fillText(value.toFixed(2), 6, y + 4);
    }

    const step = innerW / series.length;
    const bodyWidth = Math.max(1, Math.min(10, step * 0.6));

    for (let i = 0; i < series.length; i += 1) {
      const point = series[i];
      const x = padding.left + step * i + step / 2;
      const openY = padding.top + ((maxVal - point.open) / range) * innerH;
      const closeY = padding.top + ((maxVal - point.close) / range) * innerH;
      const highY = padding.top + ((maxVal - point.high) / range) * innerH;
      const lowY = padding.top + ((maxVal - point.low) / range) * innerH;
      const isUp = point.close >= point.open;
      const color = isUp ? colorUp : colorDown;

      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, highY);
      ctx.lineTo(x, lowY);
      ctx.stroke();

      ctx.fillStyle = color;
      const bodyTop = Math.min(openY, closeY);
      const bodyHeight = Math.max(1, Math.abs(closeY - openY));
      ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
    }

    // Last price label
    const last = series[series.length - 1];
    ctx.fillStyle = "#111";
    ctx.font = "12px sans-serif";
    ctx.fillText(`Last: ${last.close.toFixed(2)}`, width - padding.right - 90, padding.top + 4);
  }, [series, size, colorUp, colorDown]);

  return (
    <div ref={containerRef} className="card" style={{ minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap", alignItems: "center" }}>
        <div>
          <h3 style={{ marginBottom: "0.25rem" }}>{label} Preis (Canvas)</h3>
          <div style={{ fontSize: "0.85em", opacity: 0.7 }}>
            {loading ? "Lade..." : "Live Cache"}
          </div>
        </div>
        <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
          <label style={{ display: "flex", flexDirection: "column", fontSize: "0.85em" }}>
            Candle Intervall
            <select value={intervalValue} onChange={(event) => onIntervalChange(event.target.value)}>
              <option value="1d">1d</option>
              <option value="3h">3h</option>
              <option value="1h">1h</option>
              <option value="5m">5m</option>
              <option value="1m">1m</option>
            </select>
          </label>
          <label style={{ display: "flex", flexDirection: "column", fontSize: "0.85em" }}>
            Zeitraum
            <select value={spanValue} onChange={(event) => onSpanChange(event.target.value)}>
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
      <canvas ref={canvasRef} />
      {series.length === 0 && <p style={{ marginTop: "0.5rem" }}>Keine Preisdaten verfügbar.</p>}
    </div>
  );
}

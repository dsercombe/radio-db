import { useEffect, useState, useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Brush,
  ReferenceLine,
} from "recharts";
import { fetchBacktestTrades, type BacktestTradesResponse } from "../api/client";

interface StrategyVisualizationProps {
  strategy: string;
  symbol: string;
}

interface ChartDataPoint {
  timestamp: string;
  price: number;
  entry?: number;
  exit?: number;
  pnl?: number;
}

export function StrategyVisualization({
  strategy,
  symbol,
}: StrategyVisualizationProps): JSX.Element {
  const [tradesData, setTradesData] = useState<BacktestTradesResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadTrades = async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await fetchBacktestTrades(strategy, symbol);
        setTradesData(data);
      } catch (err) {
        console.error(err);
        setError("Trade-Daten konnten nicht geladen werden.");
      } finally {
        setLoading(false);
      }
    };
    loadTrades();
  }, [strategy, symbol]);

  const chartData = useMemo(() => {
    if (!tradesData || tradesData.trades.length === 0) {
      return [];
    }

    // Gruppiere Trades nach Timestamp und erstelle Chart-Datenpunkte
    const dataMap = new Map<string, ChartDataPoint>();

    for (const trade of tradesData.trades) {
      const timestamp = trade.timestamp;
      if (!dataMap.has(timestamp)) {
        dataMap.set(timestamp, {
          timestamp,
          price: trade.price,
        });
      }
      const point = dataMap.get(timestamp)!;
      if (trade.entry_price) {
        point.entry = trade.entry_price;
      }
      if (trade.exit_price) {
        point.exit = trade.exit_price;
      }
      if (trade.pnl !== undefined) {
        point.pnl = trade.pnl;
      }
    }

    return Array.from(dataMap.values()).sort((a, b) =>
      a.timestamp.localeCompare(b.timestamp)
    );
  }, [tradesData]);

  if (loading) {
    return (
      <div style={{ marginTop: "2rem" }}>
        <h4>Trade-Visualisierung</h4>
        <p>Lade Trade-Daten...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ marginTop: "2rem" }}>
        <h4>Trade-Visualisierung</h4>
        <div className="card card--error">{error}</div>
      </div>
    );
  }

  if (!tradesData || tradesData.trades.length === 0) {
    return (
      <div style={{ marginTop: "2rem" }}>
        <h4>Trade-Visualisierung</h4>
        <div className="card card--info">
          <p>
            Keine Trade-Daten verfügbar für {strategy} / {symbol}.
          </p>
          {tradesData?.backtest_id && (
            <p>
              Backtest ID: {tradesData.backtest_id}
              {tradesData.backtest_start && tradesData.backtest_end && (
                <>
                  <br />
                  Zeitraum: {tradesData.backtest_start} bis {tradesData.backtest_end}
                </>
              )}
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ marginTop: "2rem" }}>
      <h4>Trade-Visualisierung</h4>
      {tradesData.backtest_id && (
        <p style={{ fontSize: "0.9rem", color: "#666", marginBottom: "1rem" }}>
          Backtest: {tradesData.backtest_id}
          {tradesData.backtest_start && tradesData.backtest_end && (
            <> ({tradesData.backtest_start} - {tradesData.backtest_end})</>
          )}
        </p>
      )}
      <div style={{ width: "100%", height: "400px" }}>
        <ResponsiveContainer>
          <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fontSize: 12 }}
              angle={-45}
              textAnchor="end"
              height={80}
            />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip
              formatter={(value: unknown, name: string) => {
                if (name === "price") return [`$${Number(value).toFixed(2)}`, "Preis"];
                if (name === "entry") return [`$${Number(value).toFixed(2)}`, "Entry"];
                if (name === "exit") return [`$${Number(value).toFixed(2)}`, "Exit"];
                if (name === "pnl") return [`$${Number(value).toFixed(2)}`, "PnL"];
                return [value, name];
              }}
            />
            <Legend />
            <Line
              type="monotone"
              dataKey="price"
              stroke="#8884d8"
              strokeWidth={2}
              dot={false}
              name="Preis"
            />
            {chartData.some((d) => d.entry !== undefined) && (
              <Line
                type="monotone"
                dataKey="entry"
                stroke="#82ca9d"
                strokeWidth={2}
                dot={{ r: 6 }}
                name="Entry"
              />
            )}
            {chartData.some((d) => d.exit !== undefined) && (
              <Line
                type="monotone"
                dataKey="exit"
                stroke="#ff7300"
                strokeWidth={2}
                dot={{ r: 6 }}
                name="Exit"
              />
            )}
            <Brush dataKey="timestamp" height={30} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}


import { useCallback, useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from "recharts";
import { fetchBaselineComparison, type BaselineComparisonResponse } from "../api/client";

interface BaselineComparisonChartProps {
  strategyName?: string;
}

export function BaselineComparisonChart({ strategyName }: BaselineComparisonChartProps): JSX.Element {
  const [comparison, setComparison] = useState<BaselineComparisonResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchBaselineComparison(strategyName);
      setComparison(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden des Baseline-Vergleichs");
    } finally {
      setLoading(false);
    }
  }, [strategyName]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30000); // Refresh alle 30 Sekunden
    return () => clearInterval(interval);
  }, [loadData]);

  if (loading && !comparison) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Lade Baseline-Vergleich...</div>;
  }

  if (error) {
    const is404 = error.includes("404") || error.includes("Not Found");
    return (
      <div style={{ padding: "20px", color: "#c00" }}>
        <div style={{ marginBottom: "10px" }}><strong>Fehler:</strong> {error}</div>
        {is404 && (
          <div style={{ marginTop: "10px", padding: "10px", backgroundColor: "#fff3cd", borderRadius: "4px", color: "#856404" }}>
            <strong>Hinweis:</strong> Dieser Endpunkt ist möglicherweise noch nicht verfügbar. Bitte starten Sie den Backend-Server neu, um die neuen Endpunkte zu laden.
          </div>
        )}
      </div>
    );
  }

  if (!comparison || !comparison.baseline || !comparison.best_optimized) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Keine Vergleichsdaten verfügbar</div>;
  }

  // Baue Chart-Daten
  const chartData = [
    {
      name: "PnL/Trade",
      baseline: comparison.baseline.pnl_per_trade,
      optimized: comparison.best_optimized.pnl_per_trade,
      improvement: comparison.improvement?.pnl_per_trade_improvement || 0,
    },
    {
      name: "Score",
      baseline: comparison.baseline.score || comparison.baseline.pnl_per_trade,
      optimized: comparison.best_optimized.score || comparison.best_optimized.pnl_per_trade,
      improvement: comparison.improvement?.score_improvement_pct || 0,
    },
    {
      name: "Trades",
      baseline: comparison.baseline.trades,
      optimized: comparison.best_optimized.trades,
      improvement: comparison.improvement?.trades_improvement || 0,
    },
    {
      name: "Winrate (%)",
      baseline: comparison.baseline.winrate * 100,
      optimized: comparison.best_optimized.winrate * 100,
      improvement: (comparison.improvement?.winrate_improvement || 0) * 100,
    },
  ];

  return (
    <div style={{ padding: "20px" }}>
      <h3>Baseline vs. Optimiert</h3>
      <div style={{ marginBottom: "20px" }}>
        <div style={{ display: "flex", gap: "20px", marginBottom: "10px" }}>
          <div style={{ padding: "10px", backgroundColor: "#f5f5f5", borderRadius: "4px", flex: 1 }}>
            <strong>Baseline:</strong>
            <div style={{ fontSize: "12px", marginTop: "5px" }}>
              Score: {comparison.baseline.score?.toFixed(4) || comparison.baseline.pnl_per_trade.toFixed(4)}
            </div>
            <div style={{ fontSize: "12px" }}>
              PnL/Trade: {comparison.baseline.pnl_per_trade.toFixed(4)}
            </div>
            <div style={{ fontSize: "12px" }}>
              Trades: {comparison.baseline.trades}
            </div>
            <div style={{ fontSize: "12px" }}>
              Winrate: {(comparison.baseline.winrate * 100).toFixed(2)}%
            </div>
          </div>
          <div style={{ padding: "10px", backgroundColor: "#e8f5e9", borderRadius: "4px", flex: 1 }}>
            <strong>Optimiert ({comparison.best_optimized.optimization_mode || "unknown"}):</strong>
            <div style={{ fontSize: "12px", marginTop: "5px" }}>
              Score: {comparison.best_optimized.score?.toFixed(4) || comparison.best_optimized.pnl_per_trade.toFixed(4)}
            </div>
            <div style={{ fontSize: "12px" }}>
              PnL/Trade: {comparison.best_optimized.pnl_per_trade.toFixed(4)}
            </div>
            <div style={{ fontSize: "12px" }}>
              Trades: {comparison.best_optimized.trades}
            </div>
            <div style={{ fontSize: "12px" }}>
              Winrate: {(comparison.best_optimized.winrate * 100).toFixed(2)}%
            </div>
          </div>
        </div>
        {comparison.improvement && (
          <div style={{ padding: "10px", backgroundColor: "#fff3cd", borderRadius: "4px" }}>
            <strong>Verbesserung:</strong>
            <div style={{ fontSize: "12px", marginTop: "5px" }}>
              Score: {comparison.improvement.score_improvement_pct.toFixed(2)}%
            </div>
            <div style={{ fontSize: "12px" }}>
              PnL/Trade: {comparison.improvement.pnl_per_trade_improvement.toFixed(4)}
            </div>
            <div style={{ fontSize: "12px" }}>
              Trades: {comparison.improvement.trades_improvement > 0 ? "+" : ""}{comparison.improvement.trades_improvement}
            </div>
            <div style={{ fontSize: "12px" }}>
              Winrate: {comparison.improvement.winrate_improvement > 0 ? "+" : ""}{(comparison.improvement.winrate_improvement * 100).toFixed(2)}%
            </div>
          </div>
        )}
      </div>
      <ResponsiveContainer width="100%" height={400}>
        <BarChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="name" />
          <YAxis />
          <Tooltip
            contentStyle={{ backgroundColor: "#fff", border: "1px solid #ccc", borderRadius: "4px" }}
            formatter={(value: number, name: string) => {
              if (name === "baseline") return [value.toFixed(4), "Baseline"];
              if (name === "optimized") return [value.toFixed(4), "Optimiert"];
              if (name === "improvement") return [value > 0 ? `+${value.toFixed(2)}` : value.toFixed(2), "Verbesserung"];
              return [value, name];
            }}
          />
          <Legend />
          <Bar dataKey="baseline" fill="#8884d8" name="Baseline" />
          <Bar dataKey="optimized" fill="#82ca9d" name="Optimiert">
            {chartData.map((entry, index) => (
              <Cell key={`cell-${index}`} fill={entry.improvement > 0 ? "#28a745" : "#dc3545"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}


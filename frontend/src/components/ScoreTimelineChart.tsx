import { useCallback, useEffect, useState } from "react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Dot } from "recharts";
import { fetchScoreTimeline, type ScoreTimelinePoint } from "../api/client";

interface ScoreTimelineChartProps {
  strategyName?: string;
  limit?: number;
}

export function ScoreTimelineChart({ strategyName, limit = 100 }: ScoreTimelineChartProps): JSX.Element {
  const [timeline, setTimeline] = useState<ScoreTimelinePoint[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchScoreTimeline(strategyName, limit);
      setTimeline(data.timeline);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Score-Timeline");
    } finally {
      setLoading(false);
    }
  }, [strategyName, limit]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000); // Refresh alle 10 Sekunden
    return () => clearInterval(interval);
  }, [loadData]);

  if (loading && timeline.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Lade Score-Timeline...</div>;
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

  if (timeline.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Keine Daten verfügbar</div>;
  }

  // Formatiere Daten für Chart
  const chartData = timeline.map((point) => ({
    timestamp: new Date(point.timestamp).toLocaleString(),
    score: point.score ?? 0,
    pnl_per_trade: point.pnl_per_trade,
    trades: point.trades,
    winrate: point.winrate * 100,
    accepted: point.accepted,
    optimization_mode: point.optimization_mode || "unknown",
  }));

  return (
    <div style={{ padding: "20px" }}>
      <h3>Score-Verlauf über Zeit</h3>
      <ResponsiveContainer width="100%" height={400}>
        <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" angle={-45} textAnchor="end" height={100} />
          <YAxis yAxisId="left" label={{ value: "Score", angle: -90, position: "insideLeft" }} />
          <YAxis yAxisId="right" orientation="right" label={{ value: "PnL/Trade", angle: 90, position: "insideRight" }} />
          <Tooltip
            contentStyle={{ backgroundColor: "#fff", border: "1px solid #ccc", borderRadius: "4px" }}
            formatter={(value: number, name: string) => {
              if (name === "score") return [value.toFixed(4), "Score"];
              if (name === "pnl_per_trade") return [value.toFixed(4), "PnL/Trade"];
              if (name === "winrate") return [`${value.toFixed(2)}%`, "Winrate"];
              if (name === "trades") return [value, "Trades"];
              return [value, name];
            }}
          />
          <Legend />
          <Line
            yAxisId="left"
            type="monotone"
            dataKey="score"
            stroke="#8884d8"
            strokeWidth={2}
            dot={<Dot r={3} />}
            name="Score"
          />
          <Line
            yAxisId="right"
            type="monotone"
            dataKey="pnl_per_trade"
            stroke="#82ca9d"
            strokeWidth={2}
            dot={<Dot r={3} />}
            name="PnL/Trade"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}


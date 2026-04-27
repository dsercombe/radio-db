import { useCallback, useEffect, useState } from "react";
import { fetchParameterVariations, type ParameterVariation } from "../api/client";

interface ParameterVariationsChartProps {
  strategyName?: string;
}

export function ParameterVariationsChart({ strategyName }: ParameterVariationsChartProps): JSX.Element {
  const [variations, setVariations] = useState<Record<string, ParameterVariation>>({});
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchParameterVariations(strategyName);
      setVariations(data.variations);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Parameter-Variations");
    } finally {
      setLoading(false);
    }
  }, [strategyName]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30000); // Refresh alle 30 Sekunden
    return () => clearInterval(interval);
  }, [loadData]);

  if (loading && Object.keys(variations).length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Lade Parameter-Variations...</div>;
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

  if (Object.keys(variations).length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Keine Parameter-Variations verfügbar</div>;
  }

  // Sortiere Parameter nach Anzahl der Variationen
  const sortedParams = Object.entries(variations).sort((a, b) => b[1].count - a[1].count);

  return (
    <div style={{ padding: "20px" }}>
      <h3>Parameter-Variations-Statistiken</h3>
      <div style={{ maxHeight: "600px", overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ backgroundColor: "#f0f0f0" }}>
              <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Parameter</th>
              <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Anzahl</th>
              <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Min</th>
              <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Max</th>
              <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Avg</th>
              <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Bereich</th>
            </tr>
          </thead>
          <tbody>
            {sortedParams.map(([paramName, stats]) => (
              <tr key={paramName}>
                <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                  <strong>{paramName}</strong>
                </td>
                <td style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>{stats.count}</td>
                <td style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>
                  {stats.min !== null ? stats.min.toFixed(4) : "—"}
                </td>
                <td style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>
                  {stats.max !== null ? stats.max.toFixed(4) : "—"}
                </td>
                <td style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>
                  {stats.avg !== null ? stats.avg.toFixed(4) : "—"}
                </td>
                <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                  {stats.min !== null && stats.max !== null ? (
                    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                      <div style={{ flex: 1, height: "20px", backgroundColor: "#e0e0e0", borderRadius: "4px", position: "relative" }}>
                        <div
                          style={{
                            position: "absolute",
                            left: "0",
                            top: "0",
                            height: "100%",
                            width: `${((stats.avg! - stats.min) / (stats.max - stats.min)) * 100}%`,
                            backgroundColor: stats.min === stats.max ? "#6c757d" : "#007bff",
                            borderRadius: "4px",
                          }}
                        />
                      </div>
                      <span style={{ fontSize: "12px", color: "#666" }}>
                        {stats.min.toFixed(2)} - {stats.max.toFixed(2)}
                      </span>
                    </div>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}


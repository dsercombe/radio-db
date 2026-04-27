import { useCallback, useEffect, useState } from "react";
import { fetchLLMDecisions, type LLMDecision } from "../api/client";

interface LLMDecisionsTimelineProps {
  strategyName?: string;
  limit?: number;
}

export function LLMDecisionsTimeline({ strategyName, limit = 50 }: LLMDecisionsTimelineProps): JSX.Element {
  const [decisions, setDecisions] = useState<LLMDecision[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedReasoning, setExpandedReasoning] = useState<Set<string>>(new Set());

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchLLMDecisions(strategyName, limit);
      setDecisions(data.decisions);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der LLM-Entscheidungen");
    } finally {
      setLoading(false);
    }
  }, [strategyName, limit]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000); // Refresh alle 10 Sekunden
    return () => clearInterval(interval);
  }, [loadData]);

  const toggleReasoning = useCallback((testId: string) => {
    setExpandedReasoning((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(testId)) {
        newSet.delete(testId);
      } else {
        newSet.add(testId);
      }
      return newSet;
    });
  }, []);

  if (loading && decisions.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Lade LLM-Entscheidungen...</div>;
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

  if (decisions.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Keine LLM-Entscheidungen verfügbar</div>;
  }

  return (
    <div style={{ padding: "20px" }}>
      <h3>LLM-Entscheidungen Timeline</h3>
      <div style={{ maxHeight: "600px", overflowY: "auto" }}>
        {decisions.map((decision) => {
          const isExpanded = expandedReasoning.has(decision.test_id);
          const isFeedbackRound = decision.feedback_round !== null && decision.feedback_round > 0;
          
          return (
            <div
              key={decision.test_id}
              style={{
                marginBottom: "15px",
                padding: "15px",
                backgroundColor: isFeedbackRound ? "#fff3cd" : "#f5f5f5",
                borderRadius: "8px",
                border: `2px solid ${isFeedbackRound ? "#ffc107" : decision.accepted ? "#28a745" : "#6c757d"}`,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "10px" }}>
                <div>
                  <strong>{decision.strategy_name}</strong>
                  <div style={{ fontSize: "12px", color: "#666", marginTop: "5px" }}>
                    {new Date(decision.timestamp).toLocaleString()}
                  </div>
                </div>
                <div style={{ textAlign: "right" }}>
                  {decision.llm_model && (
                    <div style={{ fontSize: "12px", color: "#666", marginBottom: "5px" }}>
                      Modell: <strong>{decision.llm_model}</strong>
                    </div>
                  )}
                  {isFeedbackRound && (
                    <div style={{ fontSize: "12px", color: "#856404", marginBottom: "5px" }}>
                      Feedback-Runde: <strong>{decision.feedback_round}</strong>
                    </div>
                  )}
                  {decision.score !== null && (
                    <div style={{ fontSize: "12px", color: "#666", marginBottom: "5px" }}>
                      Score: <strong>{decision.score.toFixed(4)}</strong>
                    </div>
                  )}
                  {decision.accepted && (
                    <div style={{ fontSize: "12px", color: "#28a745", fontWeight: "bold" }}>
                      ✓ Akzeptiert
                    </div>
                  )}
                </div>
              </div>
              
              <div style={{ marginBottom: "10px", fontSize: "14px" }}>
                <strong>Parameter:</strong>
                <pre style={{ backgroundColor: "#fff", padding: "8px", borderRadius: "4px", fontSize: "12px", overflow: "auto" }}>
                  {JSON.stringify(decision.param_values, null, 2)}
                </pre>
              </div>
              
              <div style={{ marginBottom: "10px", fontSize: "14px" }}>
                <strong>Performance:</strong>
                <div style={{ display: "flex", gap: "15px", marginTop: "5px" }}>
                  <span>PnL/Trade: {decision.pnl_per_trade.toFixed(4)}</span>
                  <span>Trades: {decision.trades}</span>
                  <span>Winrate: {(decision.winrate * 100).toFixed(2)}%</span>
                </div>
              </div>
              
              {decision.llm_reasoning && (
                <div style={{ marginTop: "10px" }}>
                  <button
                    onClick={() => toggleReasoning(decision.test_id)}
                    style={{
                      padding: "5px 10px",
                      backgroundColor: "#007bff",
                      color: "#fff",
                      border: "none",
                      borderRadius: "4px",
                      cursor: "pointer",
                      fontSize: "12px",
                    }}
                  >
                    {isExpanded ? "▼" : "▶"} Reasoning {isExpanded ? "ausblenden" : "anzeigen"}
                  </button>
                  {isExpanded && (
                    <div
                      style={{
                        marginTop: "10px",
                        padding: "10px",
                        backgroundColor: "#fff",
                        borderRadius: "4px",
                        fontSize: "13px",
                        whiteSpace: "pre-wrap",
                        maxHeight: "300px",
                        overflowY: "auto",
                      }}
                    >
                      {decision.llm_reasoning}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


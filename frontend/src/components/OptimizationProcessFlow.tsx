import { useCallback, useEffect, useState } from "react";
import { fetchLLMDecisions, type LLMDecision } from "../api/client";

interface OptimizationProcessFlowProps {
  strategyName?: string;
  limit?: number;
}

export function OptimizationProcessFlow({ strategyName, limit = 50 }: OptimizationProcessFlowProps): JSX.Element {
  const [decisions, setDecisions] = useState<LLMDecision[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchLLMDecisions(strategyName, limit);
      setDecisions(data.decisions);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden des Optimierungsprozesses");
    } finally {
      setLoading(false);
    }
  }, [strategyName, limit]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000); // Refresh alle 10 Sekunden
    return () => clearInterval(interval);
  }, [loadData]);

  if (loading && decisions.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Lade Optimierungsprozess...</div>;
  }

  if (error) {
    return <div style={{ padding: "20px", color: "#c00" }}>Fehler: {error}</div>;
  }

  if (decisions.length === 0) {
    return <div style={{ padding: "20px", textAlign: "center" }}>Keine Optimierungsprozess-Daten verfügbar</div>;
  }

  // Gruppiere Entscheidungen nach Feedback-Runde und Strategie
  const groupedDecisions: Record<string, LLMDecision[]> = {};
  decisions.forEach((decision) => {
    const key = `${decision.strategy_name}_${decision.feedback_round || 0}`;
    if (!groupedDecisions[key]) {
      groupedDecisions[key] = [];
    }
    groupedDecisions[key].push(decision);
  });

  // Finde Baseline (erste Entscheidung ohne feedback_round oder mit feedback_round === 0)
  const baselineDecisions = decisions.filter((d) => d.feedback_round === null || d.feedback_round === 0);
  const feedbackDecisions = decisions.filter((d) => d.feedback_round !== null && d.feedback_round > 0);
  const acceptedDecisions = decisions.filter((d) => d.accepted);

  return (
    <div style={{ padding: "20px" }}>
      <h3>Optimierungsprozess-Flow</h3>
      <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
        {/* Baseline Phase */}
        <div style={{ padding: "15px", backgroundColor: "#f5f5f5", borderRadius: "8px", border: "2px solid #6c757d" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
            <div style={{ width: "30px", height: "30px", backgroundColor: "#6c757d", color: "#fff", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold" }}>
              1
            </div>
            <strong>Baseline-Test</strong>
          </div>
          <div style={{ fontSize: "14px", color: "#666" }}>
            {baselineDecisions.length > 0 ? (
              <div>
                <div>Anzahl Tests: {baselineDecisions.length}</div>
                <div>Bester Score: {Math.max(...baselineDecisions.map((d) => d.score || 0)).toFixed(4)}</div>
              </div>
            ) : (
              "Noch keine Baseline-Tests durchgeführt"
            )}
          </div>
        </div>

        {/* LLM Round 1 Phase */}
        <div style={{ padding: "15px", backgroundColor: "#e3f2fd", borderRadius: "8px", border: "2px solid #2196f3" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
            <div style={{ width: "30px", height: "30px", backgroundColor: "#2196f3", color: "#fff", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold" }}>
              2
            </div>
            <strong>LLM-Round 1 (Initiale Empfehlungen)</strong>
          </div>
          <div style={{ fontSize: "14px", color: "#666" }}>
            {baselineDecisions.filter((d) => d.llm_model && (d.feedback_round === null || d.feedback_round === 0)).length > 0 ? (
              <div>
                <div>Anzahl Tests: {baselineDecisions.filter((d) => d.llm_model && (d.feedback_round === null || d.feedback_round === 0)).length}</div>
                <div>Verwendetes Modell: {baselineDecisions.find((d) => d.llm_model)?.llm_model || "Unknown"}</div>
                <div>Bester Score: {Math.max(...baselineDecisions.filter((d) => d.llm_model && (d.feedback_round === null || d.feedback_round === 0)).map((d) => d.score || 0)).toFixed(4)}</div>
              </div>
            ) : (
              "Noch keine LLM-Empfehlungen erhalten"
            )}
          </div>
        </div>

        {/* Feedback Phase */}
        {feedbackDecisions.length > 0 && (
          <div style={{ padding: "15px", backgroundColor: "#fff3cd", borderRadius: "8px", border: "2px solid #ffc107" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
              <div style={{ width: "30px", height: "30px", backgroundColor: "#ffc107", color: "#000", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold" }}>
                3
              </div>
              <strong>Feedback-Runde</strong>
            </div>
            <div style={{ fontSize: "14px", color: "#666" }}>
              <div>Anzahl Tests: {feedbackDecisions.length}</div>
              <div>Verwendetes Modell: {feedbackDecisions[0]?.llm_model || "Unknown"}</div>
              <div>Bester Score: {Math.max(...feedbackDecisions.map((d) => d.score || 0)).toFixed(4)}</div>
            </div>
          </div>
        )}

        {/* Acceptance Phase */}
        {acceptedDecisions.length > 0 && (
          <div style={{ padding: "15px", backgroundColor: "#e8f5e9", borderRadius: "8px", border: "2px solid #28a745" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
              <div style={{ width: "30px", height: "30px", backgroundColor: "#28a745", color: "#fff", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold" }}>
                ✓
              </div>
              <strong>Akzeptierte Parameter</strong>
            </div>
            <div style={{ fontSize: "14px", color: "#666" }}>
              <div>Anzahl akzeptiert: {acceptedDecisions.length}</div>
              <div>Bester Score: {Math.max(...acceptedDecisions.map((d) => d.score || 0)).toFixed(4)}</div>
              <div>PnL/Trade: {Math.max(...acceptedDecisions.map((d) => d.pnl_per_trade)).toFixed(4)}</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


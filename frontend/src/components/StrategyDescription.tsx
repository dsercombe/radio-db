import type { StrategyMetadataResponse } from "../api/client";

interface StrategyDescriptionProps {
  strategyName: string;
  metadata: StrategyMetadataResponse;
}

export function StrategyDescription({
  strategyName,
  metadata,
}: StrategyDescriptionProps): JSX.Element {
  const strategy = metadata.strategies[strategyName];

  if (!strategy) {
    return (
      <div className="card card--info">
        <p>Keine Beschreibung für Strategie "{strategyName}" verfügbar.</p>
      </div>
    );
  }

  return (
    <div className="card card--info" style={{ marginBottom: "1rem" }}>
      <h4>Strategie-Beschreibung</h4>
      <p style={{ whiteSpace: "pre-wrap", lineHeight: "1.6" }}>{strategy.description.trim()}</p>
    </div>
  );
}


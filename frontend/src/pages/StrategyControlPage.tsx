import { useEffect, useState } from "react";
import {
  fetchStrategyBranches,
  fetchStrategyMetadata,
  type StrategyBranch,
  type StrategyMetadataResponse,
} from "../api/client";
import { StrategyBranchTree } from "../components/StrategyBranchTree";
import { StrategyParameterEditor } from "../components/StrategyParameterEditor";
import { StrategyDescription } from "../components/StrategyDescription";
import { StrategyVisualization } from "../components/StrategyVisualization";

export function StrategyControlPage(): JSX.Element {
  const [branches, setBranches] = useState<StrategyBranch[]>([]);
  const [metadata, setMetadata] = useState<StrategyMetadataResponse | null>(null);
  const [selectedBranch, setSelectedBranch] = useState<StrategyBranch | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadData = async () => {
      try {
        setLoading(true);
        setError(null);
        const [branchesData, metadataData] = await Promise.all([
          fetchStrategyBranches(),
          fetchStrategyMetadata(),
        ]);
        setBranches(branchesData.branches);
        setMetadata(metadataData);
      } catch (err) {
        console.error(err);
        setError("Daten konnten nicht geladen werden.");
      } finally {
        setLoading(false);
      }
    };
    loadData();
  }, []);

  const handleBranchSelect = (branch: StrategyBranch) => {
    setSelectedBranch(branch);
  };

  const handleRefresh = async () => {
    try {
      setLoading(true);
      setError(null);
      const branchesData = await fetchStrategyBranches();
      setBranches(branchesData.branches);
      // Aktualisiere selectedBranch falls vorhanden
      if (selectedBranch) {
        const updated = branchesData.branches.find(
          (b) =>
            b.strategy === selectedBranch.strategy &&
            b.variation === selectedBranch.variation &&
            b.direction === selectedBranch.direction &&
            b.symbol === selectedBranch.symbol
        );
        if (updated) {
          setSelectedBranch(updated);
        }
      }
    } catch (err) {
      console.error(err);
      setError("Daten konnten nicht aktualisiert werden.");
    } finally {
      setLoading(false);
    }
  };

  if (loading && branches.length === 0) {
    return (
      <section className="page">
        <header className="page__header">
          <div>
            <h2>Strategie Steuerung</h2>
            <p>Lade Strategie-Zweige...</p>
          </div>
        </header>
      </section>
    );
  }

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Strategie Steuerung</h2>
          <p>Verwaltung von Strategie-Parametern pro Strategie-Zweig.</p>
        </div>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button type="button" className="button button--ghost" onClick={handleRefresh}>
            Aktualisieren
          </button>
        </div>
      </header>

      {error && <div className="card card--error">{error}</div>}

      <div className="grid grid--cols-2 grid--gap-lg" style={{ gridTemplateColumns: "300px 1fr" }}>
        <div className="card">
          <h3>Strategie-Zweige</h3>
          {loading ? (
            <p>Lade...</p>
          ) : branches.length === 0 ? (
            <p>Keine Strategie-Zweige gefunden.</p>
          ) : (
            <StrategyBranchTree
              branches={branches}
              selectedBranch={selectedBranch}
              onSelect={handleBranchSelect}
            />
          )}
        </div>

        <div className="card">
          {selectedBranch ? (
            <>
              <h3>
                {selectedBranch.strategy} / {selectedBranch.variation} / {selectedBranch.direction} /{" "}
                {selectedBranch.symbol}
              </h3>
              {metadata && (
                <StrategyDescription
                  strategyName={selectedBranch.strategy}
                  metadata={metadata}
                />
              )}
              {metadata && (
                <StrategyParameterEditor
                  branch={selectedBranch}
                  metadata={metadata}
                  onUpdate={handleRefresh}
                />
              )}
              <StrategyVisualization
                strategy={selectedBranch.strategy}
                symbol={selectedBranch.symbol}
              />
            </>
          ) : (
            <div>
              <p>Wähle einen Strategie-Zweig aus, um Details anzuzeigen.</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}


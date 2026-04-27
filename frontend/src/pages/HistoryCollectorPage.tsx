import { useEffect, useState } from "react";
import {
  fetchHistoryCollectorStatus,
  fetchHistoryCollectorLogs,
  fetchHistoryCollectorConfig,
  setHistoryCollectorConfig,
  startHistoryCollector,
  stopHistoryCollector,
  type HistoryCollectorStatus,
  type HistoryCollectorConfig as HistoryCollectorConfigType,
} from "../api/client";

export function HistoryCollectorPage(): JSX.Element {
  const [status, setStatus] = useState<HistoryCollectorStatus | null>(null);
  const [config, setConfig] = useState<HistoryCollectorConfigType | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = async () => {
    try {
      setError(null);
      const [statusData, configData, logsData] = await Promise.all([
        fetchHistoryCollectorStatus().catch((err) => {
          console.error("Fehler beim Laden des Status:", err);
          throw new Error(`Status: ${err instanceof Error ? err.message : String(err)}`);
        }),
        fetchHistoryCollectorConfig().catch((err) => {
          console.error("Fehler beim Laden der Konfiguration:", err);
          // Konfiguration ist optional, verwende null bei Fehler
          return { config: null };
        }),
        fetchHistoryCollectorLogs(100).catch((err) => {
          console.error("Fehler beim Laden der Logs:", err);
          // Logs sind optional, verwende leeres Array bei Fehler
          return { logs: [], count: 0 };
        }),
      ]);
      setStatus(statusData);
      if (configData && configData.config) {
        setConfig(configData.config);
      }
      if (logsData && logsData.logs) {
        setLogs(logsData.logs);
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Fehler beim Laden der Daten";
      console.error("Fehler in loadData:", err);
      setError(errorMessage);
    }
  };

  useEffect(() => {
    loadData();
    // Reduziere Refresh-Intervall auf 15 Sekunden für bessere Performance
    const interval = setInterval(loadData, 15000); // Refresh alle 15 Sekunden
    return () => clearInterval(interval);
  }, []);

  const handleStart = async () => {
    setLoading(true);
    try {
      await startHistoryCollector();
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Starten");
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await stopHistoryCollector();
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Stoppen");
    } finally {
      setLoading(false);
    }
  };

  const handleConfigUpdate = async (updates: Partial<HistoryCollectorConfigType>) => {
    setLoading(true);
    try {
      await setHistoryCollectorConfig(updates);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Aktualisieren der Konfiguration");
    } finally {
      setLoading(false);
    }
  };

  const handleStartDateChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = e.target.value;
    // Aktualisiere lokalen State sofort für bessere UX
    if (config) {
      setConfig({ ...config, start_date: newValue });
    }
    // Dann aktualisiere auf dem Server
    await handleConfigUpdate({ start_date: newValue });
  };

  const handleIntervalChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (config) {
      const value = parseInt(e.target.value, 10);
      if (!isNaN(value) && value > 0) {
        handleConfigUpdate({ fetch_interval_seconds: value });
      }
    }
  };

  const handleDataSourceChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    if (config) {
      handleConfigUpdate({ data_source: e.target.value });
    }
  };

  const handleIntervalsChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (config) {
      const interval = e.target.value;
      const checked = e.target.checked;
      const currentIntervals = config.intervals || [];
      if (checked && !currentIntervals.includes(interval)) {
        handleConfigUpdate({ intervals: [...currentIntervals, interval] });
      } else if (!checked && currentIntervals.includes(interval)) {
        handleConfigUpdate({ intervals: currentIntervals.filter((i) => i !== interval) });
      }
    }
  };

  const availableIntervals = ["1s", "5s", "1m", "5m", "15m", "1h"];

  return (
    <div className="page-container">
      <h1>History Data Collector</h1>

      {error && (
        <div className="error-message" style={{ color: "red", marginBottom: "1rem", padding: "10px", backgroundColor: "#fee", borderRadius: "4px" }}>
          <strong>Network Error:</strong> {error}
        </div>
      )}

      {/* Status Section */}
      <section className="section" style={{ marginBottom: "2rem" }}>
        <h2>Status</h2>
        <div className="status-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "1rem" }}>
          <div className="status-item">
            <strong>Status:</strong>{" "}
            <span style={{ color: status?.running ? "green" : "red" }}>
              {status ? (status.running ? "Läuft" : "Gestoppt") : "Lädt..."}
            </span>
            {status && (
              <span style={{ marginLeft: "10px", fontSize: "12px", color: "#666" }}>
                (running: {String(status.running)})
              </span>
            )}
          </div>
          <div className="status-item">
            <strong>Letzte Aktivität:</strong>{" "}
            {status?.last_activity
              ? new Date(status.last_activity).toLocaleString("de-DE")
              : "Keine"}
          </div>
          {status?.last_error && (
            <div className="status-item" style={{ color: "red" }}>
              <strong>Letzter Fehler:</strong> {status.last_error}
            </div>
          )}
          {status?.stats && (
            <>
              <div className="status-item">
                <strong>Gaps gefüllt:</strong> {status.stats.gaps_filled || 0}
              </div>
              <div className="status-item">
                <strong>Neue Daten:</strong> {status.stats.new_data_fetched || 0}
              </div>
              <div className="status-item">
                <strong>Fehler:</strong> {status.stats.errors || 0}
              </div>
            </>
          )}
        </div>
        <div style={{ marginTop: "1rem" }}>
          <button
            onClick={handleStart}
            disabled={loading || status?.running}
            style={{
              padding: "0.5rem 1rem",
              marginRight: "0.5rem",
              backgroundColor: "#4CAF50",
              color: "white",
              border: "none",
              borderRadius: "4px",
              cursor: status?.running || loading ? "not-allowed" : "pointer",
            }}
          >
            Start
          </button>
          <button
            onClick={handleStop}
            disabled={loading || !status?.running}
            style={{
              padding: "0.5rem 1rem",
              backgroundColor: "#f44336",
              color: "white",
              border: "none",
              borderRadius: "4px",
              cursor: !status?.running || loading ? "not-allowed" : "pointer",
            }}
          >
            Stop
          </button>
        </div>
      </section>

      {/* Configuration Section */}
      <section className="section" style={{ marginBottom: "2rem" }}>
        <h2>Konfiguration</h2>
        {config && (
          <div className="config-form" style={{ display: "grid", gap: "1rem" }}>
            <div className="form-group">
              <label>
                <strong>Startdatum:</strong>
                <input
                  type="date"
                  value={config?.start_date || "2025-01-01"}
                  onChange={handleStartDateChange}
                  style={{ marginLeft: "0.5rem", padding: "0.25rem" }}
                  disabled={loading}
                />
              </label>
            </div>

            <div className="form-group">
              <label>
                <strong>Abruf-Intervall (Sekunden):</strong>
                <input
                  type="number"
                  value={config.fetch_interval_seconds || 300}
                  onChange={handleIntervalChange}
                  min="60"
                  style={{ marginLeft: "0.5rem", padding: "0.25rem", width: "100px" }}
                />
              </label>
            </div>

            <div className="form-group">
              <label>
                <strong>Datenquelle:</strong>
                <select
                  value={config.data_source || "BINANCE"}
                  onChange={handleDataSourceChange}
                  style={{ marginLeft: "0.5rem", padding: "0.25rem" }}
                >
                  <option value="BINANCE">BINANCE</option>
                  <option value="KRAKEN">KRAKEN</option>
                  <option value="BOTH">BOTH</option>
                </select>
              </label>
            </div>

            <div className="form-group">
              <strong>Intervalle:</strong>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.5rem" }}>
                {availableIntervals.map((interval) => (
                  <label key={interval} style={{ display: "flex", alignItems: "center", gap: "0.25rem" }}>
                    <input
                      type="checkbox"
                      value={interval}
                      checked={(config.intervals || []).includes(interval)}
                      onChange={handleIntervalsChange}
                    />
                    {interval}
                  </label>
                ))}
              </div>
            </div>

            <div className="form-group">
              <label>
                <strong>Symbole:</strong>
                <div style={{ marginTop: "0.5rem" }}>
                  {(config.symbols || []).map((symbol) => (
                    <span key={symbol} style={{ marginRight: "0.5rem", padding: "0.25rem 0.5rem", backgroundColor: "#f0f0f0", borderRadius: "4px" }}>
                      {symbol}
                    </span>
                  ))}
                </div>
              </label>
            </div>

            <div className="form-group">
              <label>
                <strong>Max Gap Stunden:</strong>
                <input
                  type="number"
                  value={config.max_gap_hours || 24}
                  onChange={(e) => {
                    const value = parseInt(e.target.value, 10);
                    if (!isNaN(value) && value > 0) {
                      handleConfigUpdate({ max_gap_hours: value });
                    }
                  }}
                  min="1"
                  style={{ marginLeft: "0.5rem", padding: "0.25rem", width: "100px" }}
                />
              </label>
            </div>
          </div>
        )}
      </section>

      {/* Logs Section */}
      <section className="section">
        <h2>Logs</h2>
        <div
          className="logs-container"
          style={{
            backgroundColor: "#1e1e1e",
            color: "#d4d4d4",
            padding: "1rem",
            borderRadius: "4px",
            maxHeight: "400px",
            overflowY: "auto",
            fontFamily: "monospace",
            fontSize: "0.875rem",
          }}
        >
          {logs.length === 0 ? (
            <div style={{ color: "#888" }}>Keine Logs verfügbar</div>
          ) : (
            logs.map((log, index) => (
              <div key={index} style={{ marginBottom: "0.25rem", whiteSpace: "pre-wrap" }}>
                {log}
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}

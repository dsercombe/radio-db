import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchNewsStatus,
  fetchNewsFeed,
  fetchNewsSignals,
  fetchNewsPositions,
  triggerNewsScan,
  type NewsFeedItem,
  type NewsPositionItem,
  type NewsSignalItem,
  type NewsStatus,
} from "../api/client";

function formatDate(value?: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatNumber(value?: number | null, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function sentimentColor(value: number): string {
  if (value > 0.2) return "var(--success-color)";
  if (value < -0.2) return "var(--danger-color)";
  return "var(--text-color)";
}

export function NewsTradingPage(): JSX.Element {
  const [status, setStatus] = useState<NewsStatus | null>(null);
  const [feed, setFeed] = useState<NewsFeedItem[]>([]);
  const [signals, setSignals] = useState<NewsSignalItem[]>([]);
  const [positions, setPositions] = useState<NewsPositionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [manualScanRunning, setManualScanRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scanResult, setScanResult] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setError(null);
      setLoading(true);
      const [statusResp, feedResp, signalsResp, positionsResp] = await Promise.all([
        fetchNewsStatus(),
        fetchNewsFeed(30),
        fetchNewsSignals(30),
        fetchNewsPositions(20),
      ]);
      setStatus(statusResp);
      setFeed(feedResp.items || []);
      setSignals(signalsResp.items || []);
      setPositions(positionsResp.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Newsdaten");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 45000);
    return () => clearInterval(interval);
  }, [loadData]);

  const handleManualScan = useCallback(async () => {
    try {
      setManualScanRunning(true);
      const response = await triggerNewsScan();
      setScanResult(`Pulse: ${JSON.stringify(response.result)}`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scan fehlgeschlagen");
    } finally {
      setManualScanRunning(false);
    }
  }, [loadData]);

  const isEnabled = status?.enabled;
  const heartbeat = status?.state?.heartbeat || status?.state?.updated_at;

  const groupedSignals = useMemo(() => {
    return signals.reduce<Record<string, NewsSignalItem[]>>((acc, sig) => {
      const key = sig.symbol;
      acc[key] = acc[key] || [];
      acc[key].push(sig);
      return acc;
    }, {});
  }, [signals]);

  return (
    <div className="page news-trading-page">
      <section className="card">
        <header className="card__header">
          <div>
            <h2>News Trading Status</h2>
            <p>
              {isEnabled ? "Aktiver Pulse" : "Subsystem deaktiviert"}
              {heartbeat && ` • Heartbeat ${formatDate(heartbeat)}`}
            </p>
          </div>
          <div className="card__actions">
            <button onClick={loadData} disabled={loading}>
              Refresh
            </button>
            <button onClick={handleManualScan} disabled={!isEnabled || manualScanRunning}>
              {manualScanRunning ? "Scanning…" : "Manueller Scan"}
            </button>
          </div>
        </header>
        {error && <div className="alert alert--danger">{error}</div>}
        {scanResult && <div className="alert alert--info">{scanResult}</div>}
        <div className="grid grid--cols-3">
          <div>
            <div className="metric-label">Queue Depth</div>
            <div className="metric-value">{status?.state?.queue_depth ?? 0}</div>
          </div>
          <div>
            <div className="metric-label">Processed Events</div>
            <div className="metric-value">{status?.state?.processed_events ?? 0}</div>
          </div>
          <div>
            <div className="metric-label">Symbols</div>
            <div className="metric-value">{status?.symbols?.join(", ") ?? "—"}</div>
          </div>
        </div>
      </section>

      <section className="card">
        <header className="card__header">
          <h2>LLM Signals</h2>
          <p>Letzte verarbeitete News inklusive Reaktionsvorschläge</p>
        </header>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Zeit</th>
                <th>Headline</th>
                <th>Impact</th>
                <th>Sentiment</th>
                <th>Tags</th>
                <th>Signals</th>
              </tr>
            </thead>
            <tbody>
              {feed.map((item) => (
                <tr key={item.event_id}>
                  <td>{formatDate(item.published_at || item.created_at)}</td>
                  <td>
                    {item.url ? (
                      <a href={item.url} target="_blank" rel="noreferrer">
                        {item.headline}
                      </a>
                    ) : (
                      item.headline
                    )}
                    <div className="table-subline">{item.summary}</div>
                  </td>
                  <td>{formatNumber(item.impact, 2)}</td>
                  <td style={{ color: sentimentColor(item.sentiment) }}>{formatNumber(item.sentiment, 2)}</td>
                  <td>{item.tags?.join(", ")}</td>
                  <td>
                    {(item.signals || []).map((sig) => (
                      <div key={sig.signal_id} className="badge">
                        {sig.rule_name} · {sig.direction.toUpperCase()} · {sig.symbol}
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
              {feed.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center" }}>
                    Keine News verarbeitet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <header className="card__header">
          <h2>Aktive Signale</h2>
          <p>Pending und letzte Signale gruppiert nach Symbol</p>
        </header>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Richtung</th>
                <th>Confidence</th>
                <th>Horizon</th>
                <th>Size (USD)</th>
                <th>Status</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((sig) => (
                <tr key={sig.signal_id}>
                  <td>{sig.symbol}</td>
                  <td>{sig.direction.toUpperCase()}</td>
                  <td>{formatNumber(sig.confidence, 2)}</td>
                  <td>{sig.horizon_minutes}m</td>
                  <td>{formatNumber(sig.size_usd, 0)}</td>
                  <td>{sig.status}</td>
                  <td>{formatDate(sig.created_at)}</td>
                </tr>
              ))}
              {signals.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: "center" }}>
                    Keine Signale verfügbar
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <header className="card__header">
          <h2>News Trades / Positions</h2>
          <p>Aktive Swing Trades und Historie</p>
        </header>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Richtung</th>
                <th>Status</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Size</th>
                <th>PNL</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((pos) => (
                <tr key={pos.position_id}>
                  <td>{pos.symbol}</td>
                  <td>{pos.direction.toUpperCase()}</td>
                  <td>{pos.status}</td>
                  <td>{pos.entry_price ? formatNumber(pos.entry_price, 4) : "—"}</td>
                  <td>{pos.exit_price ? formatNumber(pos.exit_price, 4) : "—"}</td>
                  <td>{formatNumber(pos.size, 4)}</td>
                  <td>{pos.pnl != null ? formatNumber(pos.pnl, 2) : "—"}</td>
                </tr>
              ))}
              {positions.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: "center" }}>
                    Keine Trades ausgeführt
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

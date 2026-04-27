import { useEffect, useRef, useState } from "react";
import { fetchLogs, LogFile, tailLog } from "../api/client";

export function LogsPage(): JSX.Element {
  const [logs, setLogs] = useState<LogFile[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tailLines, setTailLines] = useState<string[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true);
        const data = await fetchLogs();
        setLogs(data.items);
      } catch (err) {
        console.error(err);
        setError("Logs konnten nicht geladen werden.");
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const handleSelectLog = async (path: string) => {
    try {
      setSelected(path);
      const data = await tailLog(path, 200);
      setTailLines(data.lines);
    } catch (err) {
      console.error(err);
      setError("Konnte Log nicht laden.");
    }
  };

  useEffect(() => {
    if (!selected) {
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
      return;
    }
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const host = window.location.host;
    const ws = new WebSocket(`${protocol}://${host}/api/logs/live?path=${encodeURIComponent(selected)}`);
    socketRef.current = ws;
    ws.onmessage = (event) => {
      const text = event.data as string;
      setTailLines((prev) => {
        const merged = (prev.join("\n") + text).split(/\r?\n/);
        return merged.slice(-400);
      });
    };
    ws.onerror = () => setError("Log-Stream unterbrochen.");
    ws.onclose = () => {
      if (socketRef.current === ws) {
        socketRef.current = null;
      }
    };
    return () => {
      if (socketRef.current === ws) {
        ws.close();
        socketRef.current = null;
      }
    };
  }, [selected]);

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Logs &amp; Diagnose</h2>
          <p>Greife auf Service-Logs zu und lade sie herunter.</p>
        </div>
      </header>

      {error && <div className="card card--error">{error}</div>}

      <div className="grid grid--cols-2 grid--gap-lg">
        <div className="card">
          <h3>Verfügbare Logs</h3>
          {loading ? (
            <p>Lade Logliste...</p>
          ) : logs.length === 0 ? (
            <p>Keine Logs gefunden.</p>
          ) : (
            <ul className="template-list">
              {logs.map((log) => (
                <li key={log.path}>
                  <div className="template-list__info">
                    <strong>{log.name}</strong>
                    <span className="template-list__tags">{log.path}</span>
                    <span>{(log.size / 1024).toFixed(1)} KB</span>
                  </div>
                  <button type="button" className="button button--ghost" onClick={() => handleSelectLog(log.path)}>
                    Anzeigen
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="card">
          <h3>Log-Auszug</h3>
          {selected ? <p className="form__label">{selected}</p> : <p>Wähle ein Log aus der Liste.</p>}
          <pre className="log-viewer">{tailLines.join("\n") || "—"}</pre>
        </div>
      </div>
    </section>
  );
}

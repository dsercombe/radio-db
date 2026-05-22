import { useEffect, useState } from "react";
import { getLinkTrackingMetrics, type LinkTrackingMetricRow } from "../radioDbApi";

export default function LinkTrackingPanel() {
  const [rows, setRows] = useState<LinkTrackingMetricRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    void getLinkTrackingMetrics()
      .then((res) => setRows(res.items || []))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <h3>Link Tracking</h3>
      {loading ? <div className="radio-db-empty">Loading link metrics…</div> : null}
      <div className="radio-db-list">
        {rows.length === 0 && !loading ? <div className="radio-db-empty">No tracked links yet.</div> : null}
        {rows.map((r) => (
          <div key={r.code} className="radio-db-list-item">
            <div className="radio-db-run__head">
              <strong>{r.recipient ?? "-"}</strong>
              <span>{r.clicks} clicks</span>
            </div>
            <div className="radio-db-subtle">{r.link_type} · {r.original_url}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

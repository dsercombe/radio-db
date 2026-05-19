import { useEffect, useMemo, useState } from "react";
import {
  AgentRunDetailResponse,
  AgentRunListItem,
  ApiPermissionMode,
  BrowserSessionState,
  OutreachCampaignDTO,
  OutreachCampaignMonitorResponse,
  StationDetailResponse,
  StationListItem,
  closeBrowserSession,
  createBrowserSession,
  getAgentRunDetail,
  getArtifactUrl,
  getBrowserSession,
  getDataControlOverview,
  getOutreachCampaignMonitor,
  getStationDetail,
  listAgentRuns,
  listBrowserSessions,
  listOutreachCampaigns,
  listStations,
  performBrowserAction,
  setApiPermissionMode,
  snapshotBrowserSession,
  startManualScan,
} from "./radioDbApi";

type ViewId = "command" | "campaigns" | "stations" | "forms" | "system" | "legacy";

interface ScanJobOverview {
  supported_job_types: string[];
  by_status: Record<string, number>;
  latest: Array<{
    id: number;
    job_type: string;
    status: string;
    priority: number;
    requested_by: string;
    claimed_by: string | null;
    created_at: string | null;
    finished_at: string | null;
    error: string | null;
  }>;
}

const API_ROOT = (() => {
  const configured = import.meta.env.VITE_API_ROOT;
  if (configured) return configured.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    const host = window.location.hostname;
    if (host !== "localhost" && host !== "127.0.0.1" && host !== "radio.public-air.net") {
      return "https://radio.public-air.net/api/v1";
    }
  }
  return "/api/v1";
})();

const navItems: Array<{ id: ViewId; label: string; note: string }> = [
  { id: "command", label: "Command", note: "What needs attention" },
  { id: "campaigns", label: "Campaigns", note: "Release outreach" },
  { id: "stations", label: "Stations", note: "Targets and routes" },
  { id: "forms", label: "Forms", note: "Supervised form work" },
  { id: "system", label: "System", note: "Workers and queues" },
  { id: "legacy", label: "Legacy", note: "Old interface backup" },
];

function numberFmt(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("de-DE") : "0";
}

function dateFmt(value?: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}

function getRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function statusTone(status: string): string {
  const normalized = status.toLowerCase();
  if (["succeeded", "completed", "verified", "active", "sent", "simulated"].includes(normalized)) return "good";
  if (["running", "queued", "candidate", "draft"].includes(normalized)) return "work";
  if (["failed", "blocked", "rejected", "error"].includes(normalized)) return "bad";
  return "muted";
}

async function fetchScanJobs(): Promise<ScanJobOverview> {
  const response = await fetch(`${API_ROOT}/scan-jobs`);
  if (!response.ok) throw new Error(`scan_jobs_${response.status}`);
  return response.json();
}

async function createScanJob(job_type: string, payload: Record<string, unknown>, requested_by = "new-ui"): Promise<void> {
  const response = await fetch(`${API_ROOT}/scan-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Radio-DB-Mode": "dry-run" },
    body: JSON.stringify({ job_type, payload, priority: 50, requested_by }),
  });
  if (!response.ok) throw new Error(await response.text());
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rdb-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function Pill({ children, tone = "muted" }: { children: string; tone?: string }) {
  return <span className={`rdb-pill rdb-pill--${tone}`}>{children}</span>;
}

function Panel({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rdb-panel">
      <header className="rdb-panel__head">
        <h2>{title}</h2>
        {action}
      </header>
      {children}
    </section>
  );
}

function App() {
  const [view, setView] = useState<ViewId>("command");
  const [permissionMode, setPermissionModeState] = useState<ApiPermissionMode>("dry-run");
  const [message, setMessage] = useState("");
  const [stations, setStations] = useState<StationListItem[]>([]);
  const [selectedStationId, setSelectedStationId] = useState<number | null>(55205);
  const [stationDetail, setStationDetail] = useState<StationDetailResponse | null>(null);
  const [runs, setRuns] = useState<AgentRunListItem[]>([]);
  const [selectedRun, setSelectedRun] = useState<AgentRunDetailResponse | null>(null);
  const [browserSessions, setBrowserSessions] = useState<BrowserSessionState[]>([]);
  const [browserSession, setBrowserSession] = useState<BrowserSessionState | null>(null);
  const [campaigns, setCampaigns] = useState<OutreachCampaignDTO[]>([]);
  const [selectedCampaignId, setSelectedCampaignId] = useState<number | null>(null);
  const [campaignMonitor, setCampaignMonitor] = useState<OutreachCampaignMonitorResponse | null>(null);
  const [scanJobs, setScanJobs] = useState<ScanJobOverview | null>(null);
  const [systemOverview, setSystemOverview] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState("");
  const [stationStatusFilter, setStationStatusFilter] = useState("");
  const [stationSubmissionFilter, setStationSubmissionFilter] = useState<"any" | "yes" | "no">("any");
  const [stationCountryFilter, setStationCountryFilter] = useState("");
  const [stationOutreachFilter, setStationOutreachFilter] = useState<"" | "verified_submission" | "contact_only">("");
  const [stationMinConfidence, setStationMinConfidence] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setApiPermissionMode(permissionMode);
  }, [permissionMode]);

  async function refreshAll() {
    setBusy(true);
    try {
      const [stationResponse, runResponse, campaignResponse, jobResponse, overviewResponse, sessionResponse] = await Promise.all([
        listStations({
          q: query || undefined,
          country: stationCountryFilter || undefined,
          status: stationStatusFilter || undefined,
          has_submission: stationSubmissionFilter,
          outreach: stationOutreachFilter,
          min_confidence: stationMinConfidence,
          page: 1,
          page_size: 50,
          sort_by: "updated_at",
          sort_order: "desc",
        }),
        listAgentRuns({ limit: 25 }),
        listOutreachCampaigns(false),
        fetchScanJobs(),
        getDataControlOverview({ history_limit: 10, history_hours: 72 }),
        listBrowserSessions(),
      ]);
      setStations(stationResponse.items);
      setRuns(runResponse.items);
      setCampaigns(campaignResponse.items);
      setScanJobs(jobResponse);
      setSystemOverview(getRecord(overviewResponse.monitor));
      setBrowserSessions(sessionResponse.items);
      if (selectedStationId) {
        setStationDetail(await getStationDetail(selectedStationId));
      }
      const campaignId = selectedCampaignId ?? campaignResponse.items[0]?.id ?? null;
      setSelectedCampaignId(campaignId);
      if (campaignId) setCampaignMonitor(await getOutreachCampaignMonitor(campaignId));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Refresh failed");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refreshAll();
  }, []);

  async function selectStation(id: number) {
    setSelectedStationId(id);
    setStationDetail(await getStationDetail(id));
    const stationRuns = await listAgentRuns({ stationId: id, limit: 20 });
    setRuns(stationRuns.items);
  }

  async function openRun(id: number) {
    const detail = await getAgentRunDetail(id);
    setSelectedRun(detail);
    setView("forms");
  }

  async function startSupervisedScan() {
    if (!selectedStationId) return;
    setBusy(true);
    try {
      const run = await startManualScan({ station_id: selectedStationId, mode: "review", max_pages: 2, force_rescan: true });
      setSelectedRun(run);
      setView("forms");
      setMessage(`Review run #${run.id} created`);
      await refreshAll();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scan failed");
    } finally {
      setBusy(false);
    }
  }

  async function openSupervisedBrowser() {
    const startUrl = stationDetail?.forms[0]?.url ?? stationDetail?.website_url ?? selectedRun?.target_url ?? null;
    const session = await createBrowserSession({
      start_url: startUrl,
      station_id: selectedStationId,
      run_id: selectedRun?.id ?? null,
      label: stationDetail?.canonical_name ?? "Form review",
    });
    setBrowserSession(session);
    setView("forms");
  }

  async function snapshotBrowser() {
    if (!browserSession) return;
    setBrowserSession(await snapshotBrowserSession(browserSession.id));
  }

  async function closeBrowser() {
    if (!browserSession) return;
    await closeBrowserSession(browserSession.id);
    setBrowserSession(null);
  }

  async function queueJob(jobType: string, payload: Record<string, unknown>) {
    await createScanJob(jobType, payload);
    setScanJobs(await fetchScanJobs());
    setMessage(`${jobType} queued`);
  }

  const selectedCampaign = useMemo(
    () => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null,
    [campaigns, selectedCampaignId],
  );
  const latestStep = selectedRun?.steps.at(-1) ?? null;
  const screenshotUrl = browserSession?.screenshot_path
    ? getArtifactUrl(browserSession.screenshot_path)
    : latestStep?.screenshot_path
      ? getArtifactUrl(latestStep.screenshot_path)
      : "";
  const monitor = systemOverview;

  return (
    <div className="rdb-app">
      <aside className="rdb-sidebar">
        <div className="rdb-brand">
          <span>Radio DB</span>
          <strong>Control Center</strong>
        </div>
        <nav>
          {navItems.map((item) => (
            <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}>
              <strong>{item.label}</strong>
              <small>{item.note}</small>
            </button>
          ))}
        </nav>
        <div className="rdb-mode">
          <span>Mode</span>
          <select value={permissionMode} onChange={(event) => setPermissionModeState(event.target.value as ApiPermissionMode)}>
            <option value="read-only">read-only</option>
            <option value="dry-run">dry-run</option>
            <option value="execute">execute</option>
          </select>
        </div>
      </aside>

      <main className="rdb-main">
        <header className="rdb-topbar">
          <div>
            <h1>{navItems.find((item) => item.id === view)?.label}</h1>
            <p>{message || "Clear workflow: choose a target, review evidence, run dry, then approve execution."}</p>
          </div>
          <div className="rdb-actions">
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search stations" />
            <button onClick={() => void refreshAll()} disabled={busy}>Refresh</button>
          </div>
        </header>

        {view === "command" && (
          <div className="rdb-stack">
            <Panel title="Operations Overview">
              <div className="rdb-command-hero">
                <div>
                  <span className="rdb-eyebrow">Radio outreach database</span>
                  <h2>{numberFmt(monitor.pitch_ready_stations)} pitch-ready stations</h2>
                  <p>{numberFmt(monitor.stations_total)} total stations, {numberFmt(monitor.submission_channels)} submission routes, worker queue online.</p>
                </div>
                <div className="rdb-command-status">
                  <Pill tone={scanJobs ? "good" : "work"}>{scanJobs ? "worker online" : "worker unknown"}</Pill>
                  <span>{browserSessions.length} browser sessions</span>
                </div>
              </div>
              <div className="rdb-metrics rdb-metrics--wide">
                <Metric label="Verified stations" value={numberFmt(monitor.stations_verified)} />
                <Metric label="Decision makers" value={numberFmt(monitor.stations_with_decision_maker)} />
                <Metric label="Queue succeeded" value={numberFmt(scanJobs?.by_status.succeeded)} />
                <Metric label="Queue failed" value={numberFmt(scanJobs?.by_status.failed)} />
              </div>
              <div className="rdb-process">
                {["Pick station", "Scan forms", "Map fields", "Dry run", "Approve submit"].map((step, index) => (
                  <div key={step}><span>{index + 1}</span><strong>{step}</strong></div>
                ))}
              </div>
            </Panel>
            <Panel title="Needs Attention">
              <div className="rdb-run-table">
                <div className="rdb-run-table__head"><span>Run</span><span>State</span><span>Issues</span><span>Status</span></div>
                {runs.filter((run) => run.status !== "completed").slice(0, 8).map((run) => (
                  <button key={run.id} className="rdb-run-row" onClick={() => void openRun(run.id)}>
                    <span><strong>#{run.id}</strong><small>{run.station_name}</small></span>
                    <span>{run.current_state}</span>
                    <span>{run.blocked_reason || `${run.issue_count} issues`}</span>
                    <Pill tone={statusTone(run.status)}>{run.status}</Pill>
                  </button>
                ))}
                {!runs.filter((run) => run.status !== "completed").length ? <div className="rdb-empty rdb-empty--small">No open runs need attention.</div> : null}
              </div>
            </Panel>
          </div>
        )}

        {view === "campaigns" && (
          <div className="rdb-grid rdb-grid--campaigns">
            <Panel title="Campaigns">
              <div className="rdb-campaign-table">
                <div className="rdb-campaign-table__head"><span>Campaign</span><span>Release</span><span>Clicks</span><span>Status</span></div>
                {campaigns.map((campaign) => (
                  <button
                    key={campaign.id}
                    className={`rdb-campaign-row ${campaign.id === selectedCampaignId ? "selected" : ""}`}
                    onClick={async () => {
                      setSelectedCampaignId(campaign.id);
                      setCampaignMonitor(await getOutreachCampaignMonitor(campaign.id));
                    }}
                  >
                    <span><strong>{campaign.name}</strong><small>{campaign.artist_name}</small></span>
                    <span>{campaign.song_title}</span>
                    <span>{campaign.press_release_click_count}</span>
                    <Pill tone={campaign.is_active ? "good" : "muted"}>{campaign.is_active ? "active" : "archived"}</Pill>
                  </button>
                ))}
              </div>
            </Panel>
            <Panel title={selectedCampaign ? selectedCampaign.name : "Campaign Detail"}>
              {selectedCampaign ? (
                <div className="rdb-detail">
                  <div className="rdb-metrics">
                    <Metric label="Drafts" value={numberFmt(campaignMonitor?.summary.drafted)} />
                    <Metric label="Sent" value={numberFmt(campaignMonitor?.summary.sent)} />
                    <Metric label="Clicked" value={numberFmt(campaignMonitor?.summary.clicked)} />
                    <Metric label="Blocked" value={numberFmt(campaignMonitor?.summary.blocked)} />
                  </div>
                  <dl>
                    <dt>Press release</dt><dd>{selectedCampaign.press_release_url || "-"}</dd>
                    <dt>Contact</dt><dd>{selectedCampaign.submission_defaults?.contact_name || "Jonas Sercombe"} · {selectedCampaign.submission_defaults?.contact_email || "radio@public-air.net"}</dd>
                    <dt>Assets</dt><dd>{Object.values(selectedCampaign.release_assets || {}).filter(Boolean).length} saved asset fields</dd>
                  </dl>
                  <button onClick={() => selectedCampaignId && void queueJob("campaign_monitor", { campaign_id: selectedCampaignId })}>Queue monitor refresh</button>
                </div>
              ) : <div className="rdb-empty">Select a campaign.</div>}
            </Panel>
          </div>
        )}

        {view === "stations" && (
          <div className="rdb-grid rdb-grid--stations">
            <Panel title="Station Directory">
              <div className="rdb-filterbar">
                <label>
                  Status
                  <select value={stationStatusFilter} onChange={(event) => setStationStatusFilter(event.target.value)}>
                    <option value="">All</option>
                    <option value="verified">Verified</option>
                    <option value="candidate">Candidate</option>
                    <option value="rejected">Rejected</option>
                  </select>
                </label>
                <label>
                  Submission
                  <select value={stationSubmissionFilter} onChange={(event) => setStationSubmissionFilter(event.target.value as "any" | "yes" | "no")}>
                    <option value="any">Any</option>
                    <option value="yes">Has route</option>
                    <option value="no">No route</option>
                  </select>
                </label>
                <label>
                  Country
                  <input value={stationCountryFilter} onChange={(event) => setStationCountryFilter(event.target.value.toUpperCase())} placeholder="DE" />
                </label>
                <label>
                  Outreach
                  <select value={stationOutreachFilter} onChange={(event) => setStationOutreachFilter(event.target.value as "" | "verified_submission" | "contact_only")}>
                    <option value="">All</option>
                    <option value="verified_submission">Verified submission</option>
                    <option value="contact_only">Contact only</option>
                  </select>
                </label>
                <label>
                  Min confidence
                  <input type="number" min={0} max={1} step={0.1} value={stationMinConfidence} onChange={(event) => setStationMinConfidence(Number(event.target.value) || 0)} />
                </label>
                <button onClick={() => void refreshAll()}>Apply</button>
              </div>
              <div className="rdb-station-table">
                <div className="rdb-station-table__head">
                  <span>Station</span>
                  <span>Market</span>
                  <span>Routes</span>
                  <span>People</span>
                  <span>Quality</span>
                  <span>Status</span>
                </div>
                {stations.map((station) => (
                  <button key={station.id} className={`rdb-station-row ${station.id === selectedStationId ? "selected" : ""}`} onClick={() => void selectStation(station.id)}>
                    <span className="rdb-station-row__name">
                      <strong>{station.canonical_name}</strong>
                      <small>{station.website_url || "No website"}</small>
                    </span>
                    <span>{station.country_code || "--"}{station.city ? ` · ${station.city}` : ""}</span>
                    <span>{station.submission_count}</span>
                    <span>{station.people_count}</span>
                    <span>{Math.round((station.quality_score ?? station.confidence_score) * 100)}%</span>
                    <Pill tone={statusTone(station.status)}>{station.status}</Pill>
                  </button>
                ))}
              </div>
            </Panel>
            <Panel title="Station Workspace" action={<button onClick={() => void startSupervisedScan()}>Start form review</button>}>
              {stationDetail ? (
                <div className="rdb-detail rdb-station-detail">
                  <div className="rdb-station-hero">
                    <div>
                      <span className="rdb-eyebrow">{stationDetail.country_code || "--"} {stationDetail.city ? `· ${stationDetail.city}` : ""}</span>
                      <h3>{stationDetail.canonical_name}</h3>
                      <p>{stationDetail.website_url || "No website saved"}</p>
                    </div>
                    <Pill tone={statusTone(stationDetail.status)}>{stationDetail.status}</Pill>
                  </div>
                  <div className="rdb-metrics">
                    <Metric label="Forms" value={String(stationDetail.forms.length)} />
                    <Metric label="Routes" value={String(stationDetail.submissions.length)} />
                    <Metric label="People" value={String(stationDetail.people.length)} />
                    <Metric label="Confidence" value={`${Math.round(stationDetail.confidence_score * 100)}%`} />
                  </div>
                  <div className="rdb-detail-sections">
                    <section>
                      <h3>Submission routes</h3>
                      {stationDetail.submissions.length ? stationDetail.submissions.slice(0, 4).map((route) => (
                        <div key={route.id} className="rdb-compact-row">
                          <strong>{route.method}</strong>
                          <span>{route.email || route.url || "-"}</span>
                        </div>
                      )) : <div className="rdb-empty rdb-empty--small">No routes saved.</div>}
                    </section>
                    <section>
                      <h3>Known forms</h3>
                      {stationDetail.forms.length ? stationDetail.forms.slice(0, 4).map((form) => (
                        <div key={form.id} className="rdb-compact-row">
                          <strong>{form.form_type}</strong>
                          <span>{form.status} · {Math.round(form.confidence * 100)}%</span>
                        </div>
                      )) : <div className="rdb-empty rdb-empty--small">No forms saved.</div>}
                    </section>
                    <section>
                      <h3>People</h3>
                      {stationDetail.people.length ? stationDetail.people.slice(0, 4).map((person) => (
                        <div key={person.id} className="rdb-compact-row">
                          <strong>{person.name || person.role}</strong>
                          <span>{person.email || person.show_name || person.role}</span>
                        </div>
                      )) : <div className="rdb-empty rdb-empty--small">No people saved.</div>}
                    </section>
                  </div>
                </div>
              ) : <div className="rdb-empty">Select a station.</div>}
            </Panel>
          </div>
        )}

        {view === "forms" && (
          <div className="rdb-stack">
            <Panel title="Form Review Workbench" action={<button onClick={() => void openSupervisedBrowser()}>Open supervised browser</button>}>
              <div className="rdb-form-board">
                <section><span>01</span><h3>Find</h3><p>Scan the station site and detect candidate forms or submission emails.</p></section>
                <section><span>02</span><h3>Understand</h3><p>Map fields to campaign data, note required files, captcha or login.</p></section>
                <section><span>03</span><h3>Preview</h3><p>Fill a dry-run in the supervised browser and capture screenshots.</p></section>
                <section><span>04</span><h3>Approve</h3><p>Only after review, submit manually or queue an execute job.</p></section>
              </div>
            </Panel>
            <div className="rdb-grid rdb-grid--forms">
              <Panel title="Current Run">
              {selectedRun ? (
                <div className="rdb-run-card">
                  <h3>Run #{selectedRun.id} · {selectedRun.station_name}</h3>
                  <Pill tone={statusTone(selectedRun.status)}>{selectedRun.status}</Pill>
                  <p>{selectedRun.current_state}{selectedRun.blocked_reason ? ` · ${selectedRun.blocked_reason}` : ""}</p>
                  <div className="rdb-timeline">
                    {selectedRun.steps.map((step) => (
                      <button key={step.id} className="rdb-step" onClick={() => setMessage(`Step ${step.step_index}: ${step.state_before} -> ${step.state_after}`)}>
                        <strong>{`${step.step_index}. ${step.state_before} -> ${step.state_after}`}</strong>
                        <span>{Math.round(step.confidence * 100)}% · {step.latency_ms}ms</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : <div className="rdb-empty">Start or select a form run to review steps.</div>}
            </Panel>
              <Panel title="Visual Inspector" action={<><button onClick={() => void snapshotBrowser()} disabled={!browserSession}>Snapshot</button><button onClick={() => void closeBrowser()} disabled={!browserSession}>Close</button></>}>
              {screenshotUrl ? (
                <button className="rdb-screenshot" onClick={() => window.open(screenshotUrl, "_blank")}>
                  <img src={screenshotUrl} alt="Current form screenshot" />
                </button>
              ) : <div className="rdb-empty">No screenshot yet.</div>}
              <div className="rdb-actions-grid">
                <button disabled={!browserSession} onClick={() => browserSession && void performBrowserAction(browserSession.id, { action: "wait", value: "1000" }).then(setBrowserSession)}>Wait</button>
                <button disabled={!browserSession} onClick={() => browserSession && void snapshotBrowser()}>Capture</button>
              </div>
            </Panel>
            </div>
          </div>
        )}

        {view === "system" && (
          <div className="rdb-grid rdb-grid--split">
            <Panel title="Scan Queue">
              <div className="rdb-list">
                {scanJobs?.latest.map((job) => (
                  <div key={job.id} className="rdb-row rdb-row--static">
                    <strong>#{job.id} {job.job_type}</strong>
                    <span>{job.claimed_by || "unclaimed"} · {dateFmt(job.finished_at || job.created_at)}</span>
                    <Pill tone={statusTone(job.status)}>{job.status}</Pill>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title="Quick Jobs">
              <div className="rdb-job-grid">
                <button onClick={() => void queueJob("stats_snapshot", {})}><strong>Stats snapshot</strong><span>Refresh operational counters.</span></button>
                <button onClick={() => void queueJob("submission_country_form_cycle", { station_limit: 3, max_forms_per_station: 2 })}><strong>Form scan cycle</strong><span>Scan a small country batch for forms.</span></button>
                <button onClick={() => selectedCampaignId && void queueJob("campaign_monitor", { campaign_id: selectedCampaignId })}><strong>Campaign monitor</strong><span>Rebuild campaign progress view.</span></button>
              </div>
            </Panel>
          </div>
        )}

        {view === "legacy" && (
          <Panel title="Legacy Interface Backup">
            <p className="rdb-muted">The complete previous UI is preserved in <code>frontend/src/LegacyApp.tsx</code>. It is not mounted by default so the new workflow can stay focused.</p>
          </Panel>
        )}
      </main>
    </div>
  );
}

export default App;

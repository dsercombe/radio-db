import { useEffect, useMemo, useState } from "react";
import {
  AgentRunDetailResponse,
  AgentRunListItem,
  BrowserSessionState,
  CountryDiscoverySnapshotResponse,
  closeBrowserSession,
  createBrowserSession,
  getBrowserSession,
  StationControlDetailResponse,
  StationDetailResponse,
  StationListItem,
  StationListResponse,
  deleteStation,
  getAgentRunDetail,
  getArtifactUrl,
  getCountryDiscoverySnapshot,
  getStationControl,
  getStationDetail,
  listAgentRuns,
  listBrowserSessions,
  listStations,
  setStationManualConfirm,
  startCountryDiscovery,
  startManualScan,
  navigateBrowserSession,
  performBrowserAction,
  snapshotBrowserSession,
  updateStation,
} from "./radioDbApi";

type TabId = "stations" | "contact" | "agent" | "data";

const TABS: Array<{ id: TabId; label: string; note: string }> = [
  { id: "stations", label: "Stations Workspace", note: "liste zuerst, modal editieren" },
  { id: "contact", label: "Contact Center", note: "drafts und lokale Sprache" },
  { id: "agent", label: "Agent Control", note: "runs, steps, artefacts" },
  { id: "data", label: "Data Control", note: "performance und focus" },
];

interface StationDraft {
  canonical_name: string;
  country_code: string;
  city: string;
  language: string;
  website_url: string;
  stream_url: string;
  status: "candidate" | "verified" | "rejected";
  confidence_score: string;
}

function formatDate(value?: string | null): string {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function statusTone(status: string): string {
  switch (status.toLowerCase()) {
    case "verified":
    case "completed":
    case "running":
    case "active":
      return "positive";
    case "rejected":
    case "failed":
    case "blocked":
      return "negative";
    case "paused":
    case "candidate":
      return "neutral";
    default:
      return "muted";
  }
}

function buildDraft(station: StationDetailResponse | null): { subject: string; body: string; localeHint: string } {
  if (!station) {
    return {
      subject: "",
      body: "",
      localeHint: "Wähle einen Sender, um einen Draft zu generieren.",
    };
  }

  const language = station.language?.trim().toLowerCase() || "en";
  const city = station.city?.trim() || station.country_code || "your market";
  const greetingByLanguage: Record<string, string> = {
    de: "Hallo",
    fr: "Bonjour",
    es: "Hola",
    it: "Ciao",
    nl: "Hallo",
    pt: "Olá",
    sv: "Hej",
  };
  const greeting = greetingByLanguage[language.slice(0, 2)] || "Hello";
  const subject = `Music submission for ${station.canonical_name}`;
  const body = [
    `${greeting} ${station.canonical_name} team,`,
    "",
    `I am reaching out with a release that feels relevant for ${city}.`,
    `The station profile suggests a fit for ${station.genres.length > 0 ? station.genres.slice(0, 3).join(", ") : "your editorial lane"}.`,
    "",
    "If there is a preferred submission route, I am happy to follow it.",
    "",
    `Best regards,`,
    `Radio DB automation`,
  ].join("\n");

  return {
    subject,
    body,
    localeHint: `Draft context: ${station.country_code || "--"} / ${station.language || "--"} / confidence ${formatPercent(station.confidence_score)}`,
  };
}

function App(): JSX.Element {
  const [activeTab, setActiveTab] = useState<TabId>("stations");
  const [filters, setFilters] = useState({
    q: "",
    country: "",
    status: "",
    page: 1,
    pageSize: 40,
  });
  const [searchDraft, setSearchDraft] = useState("");
  const [stations, setStations] = useState<StationListResponse | null>(null);
  const [stationsLoading, setStationsLoading] = useState(false);
  const [stationsError, setStationsError] = useState<string | null>(null);
  const [selectedStationId, setSelectedStationId] = useState<number | null>(null);
  const [selectedStation, setSelectedStation] = useState<StationDetailResponse | null>(null);
  const [stationControl, setStationControl] = useState<StationControlDetailResponse | null>(null);
  const [stationLoading, setStationLoading] = useState(false);
  const [stationError, setStationError] = useState<string | null>(null);
  const [stationDraft, setStationDraft] = useState<StationDraft | null>(null);
  const [stationSaving, setStationSaving] = useState(false);
  const [manualConfirmBusy, setManualConfirmBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [manualScanMode, setManualScanMode] = useState("scan");
  const [manualScanPages, setManualScanPages] = useState(1);
  const [manualScanBusy, setManualScanBusy] = useState(false);
  const [agentRuns, setAgentRuns] = useState<AgentRunListItem[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [selectedRun, setSelectedRun] = useState<AgentRunDetailResponse | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const [countryDiscovery, setCountryDiscovery] = useState<CountryDiscoverySnapshotResponse | null>(null);
  const [countryDiscoveryLoading, setCountryDiscoveryLoading] = useState(false);
  const [countryDiscoveryError, setCountryDiscoveryError] = useState<string | null>(null);
  const [browserSessions, setBrowserSessions] = useState<BrowserSessionState[]>([]);
  const [browserSessionsLoading, setBrowserSessionsLoading] = useState(false);
  const [browserSessionError, setBrowserSessionError] = useState<string | null>(null);
  const [browserSessionId, setBrowserSessionId] = useState<string | null>(null);
  const [browserSessionDetail, setBrowserSessionDetail] = useState<BrowserSessionState | null>(null);
  const [browserUrlDraft, setBrowserUrlDraft] = useState("");
  const [browserSelectorDraft, setBrowserSelectorDraft] = useState("");
  const [browserValueDraft, setBrowserValueDraft] = useState("");
  const [browserAction, setBrowserAction] = useState<"click" | "fill" | "press" | "select" | "goto" | "wait">("goto");
  const [systemMessage, setSystemMessage] = useState<string | null>(null);

  const currentStation = useMemo<StationListItem | null>(() => {
    if (!stations?.items.length) {
      return null;
    }
    if (selectedStationId === null) {
      return stations.items[0] ?? null;
    }
    return stations.items.find((item) => item.id === selectedStationId) ?? stations.items[0] ?? null;
  }, [stations, selectedStationId]);

  const summary = useMemo(() => {
    const items = stations?.items ?? [];
    const total = stations?.total ?? 0;
    const verified = items.filter((item) => item.status === "verified").length;
    const rejected = items.filter((item) => item.status === "rejected").length;
    const avgConfidence = items.length > 0 ? items.reduce((sum, item) => sum + item.confidence_score, 0) / items.length : 0;
    const countries = new Set(items.map((item) => item.country_code).filter(Boolean)).size;
    return { total, verified, rejected, avgConfidence, countries };
  }, [stations]);

  const contactDraft = useMemo(() => buildDraft(selectedStation), [selectedStation]);

  useEffect(() => {
    void refreshStations();
    void refreshAgentOverview();
  }, []);

  useEffect(() => {
    void refreshStations();
  }, [filters]);

  useEffect(() => {
    if (!stations?.items.length) {
      return;
    }
    if (selectedStationId === null) {
      setSelectedStationId(stations.items[0].id);
    }
  }, [stations, selectedStationId]);

  useEffect(() => {
    if (selectedStationId === null) {
      setSelectedStation(null);
      setStationControl(null);
      setStationDraft(null);
      setSelectedRunId(null);
      setSelectedRun(null);
      return;
    }
    void refreshStationContext(selectedStationId);
  }, [selectedStationId]);

  useEffect(() => {
    if (selectedRunId === null) {
      setSelectedRun(null);
      return;
    }
    void refreshRun(selectedRunId);
  }, [selectedRunId]);

  useEffect(() => {
    if (!browserSessionId) {
      setBrowserSessionDetail(null);
      return;
    }

    let disposed = false;
    void refreshBrowserSessionDetail(browserSessionId);

    const source = new EventSource(`/api/v1/browser/sessions/${browserSessionId}/stream`);
    const handleBrowserEvent = () => {
      if (!disposed) {
        void refreshBrowserSessions();
        void refreshBrowserSessionDetail(browserSessionId);
      }
    };

    source.addEventListener("browser-session", handleBrowserEvent);
    source.addEventListener("closed", handleBrowserEvent);
    source.onerror = () => {
      if (!disposed) {
        setBrowserSessionError("Browser stream disconnected.");
      }
    };

    return () => {
      disposed = true;
      source.close();
    };
  }, [browserSessionId]);

  useEffect(() => {
    if (activeTab === "agent" || activeTab === "data") {
      void refreshAgentOverview();
      void refreshCountryDiscovery();
      void refreshBrowserSessions();
    }
  }, [activeTab]);

  async function refreshStations(): Promise<void> {
    setStationsLoading(true);
    setStationsError(null);
    try {
      const response = await listStations({
        q: filters.q,
        country: filters.country,
        status: filters.status,
        page: filters.page,
        page_size: filters.pageSize,
        sort_by: "updated_at",
        sort_order: "desc",
      });
      setStations(response);
    } catch (error) {
      setStationsError(error instanceof Error ? error.message : "Stations konnten nicht geladen werden.");
    } finally {
      setStationsLoading(false);
    }
  }

  async function refreshStationContext(stationId: number): Promise<void> {
    setStationLoading(true);
    setStationError(null);
    try {
      const [detail, control, runs] = await Promise.all([
        getStationDetail(stationId),
        getStationControl(stationId),
        listAgentRuns({ stationId, limit: 8, offset: 0 }),
      ]);
      setSelectedStation(detail);
      setStationControl(control);
      setStationDraft({
        canonical_name: detail.canonical_name,
        country_code: detail.country_code,
        city: detail.city || "",
        language: detail.language,
        website_url: detail.website_url || "",
        stream_url: detail.stream_url || "",
        status: detail.status === "verified" ? "verified" : detail.status === "rejected" ? "rejected" : "candidate",
        confidence_score: String(detail.confidence_score),
      });
      setAgentRuns(runs.items);
      setRunsTotal(runs.total);
      setSelectedRunId((current) => current ?? runs.items[0]?.id ?? null);
    } catch (error) {
      setStationError(error instanceof Error ? error.message : "Senderdetails konnten nicht geladen werden.");
    } finally {
      setStationLoading(false);
    }
  }

  async function refreshAgentOverview(stationId: number | null = selectedStationId): Promise<void> {
    setRunsLoading(true);
    setRunsError(null);
    try {
      const response = await listAgentRuns({ stationId: stationId ?? undefined, limit: 20, offset: 0 });
      setAgentRuns(response.items);
      setRunsTotal(response.total);
      if (response.items.length > 0 && !response.items.some((item) => item.id === selectedRunId)) {
        setSelectedRunId(response.items[0].id);
      }
    } catch (error) {
      setRunsError(error instanceof Error ? error.message : "Agent-Runs konnten nicht geladen werden.");
    } finally {
      setRunsLoading(false);
    }
  }

  async function refreshRun(runId: number): Promise<void> {
    setRunLoading(true);
    try {
      const response = await getAgentRunDetail(runId);
      setSelectedRun(response);
    } catch (error) {
      setRunsError(error instanceof Error ? error.message : "Run-Detail konnte nicht geladen werden.");
    } finally {
      setRunLoading(false);
    }
  }

  async function refreshCountryDiscovery(): Promise<void> {
    setCountryDiscoveryLoading(true);
    setCountryDiscoveryError(null);
    try {
      const response = await getCountryDiscoverySnapshot();
      setCountryDiscovery(response);
    } catch (error) {
      setCountryDiscoveryError(error instanceof Error ? error.message : "Country-discovery snapshot unavailable.");
    } finally {
      setCountryDiscoveryLoading(false);
    }
  }

  async function refreshBrowserSessions(): Promise<void> {
    setBrowserSessionsLoading(true);
    setBrowserSessionError(null);
    try {
      const response = await listBrowserSessions();
      setBrowserSessions(response.items);
      if (!browserSessionId && response.items.length > 0) {
        setBrowserSessionId(response.items[0].id);
      }
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser sessions konnten nicht geladen werden.");
    } finally {
      setBrowserSessionsLoading(false);
    }
  }

  async function refreshBrowserSessionDetail(sessionId: string): Promise<void> {
    try {
      const detail = await getBrowserSession(sessionId);
      setBrowserSessionDetail(detail);
      setBrowserUrlDraft(detail.url);
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser session detail konnte nicht geladen werden.");
    }
  }

  async function handleStationSave(): Promise<void> {
    if (!selectedStationId || !stationDraft) {
      return;
    }
    setStationSaving(true);
    setSystemMessage(null);
    try {
      const response = await updateStation(selectedStationId, {
        canonical_name: stationDraft.canonical_name,
        country_code: stationDraft.country_code,
        city: stationDraft.city.trim() || null,
        language: stationDraft.language,
        website_url: stationDraft.website_url.trim() || null,
        stream_url: stationDraft.stream_url.trim() || null,
        status: stationDraft.status,
        confidence_score: Number(stationDraft.confidence_score),
      });
      setSelectedStation(response);
      setStationDraft({
        canonical_name: response.canonical_name,
        country_code: response.country_code,
        city: response.city || "",
        language: response.language,
        website_url: response.website_url || "",
        stream_url: response.stream_url || "",
        status: response.status === "verified" ? "verified" : response.status === "rejected" ? "rejected" : "candidate",
        confidence_score: String(response.confidence_score),
      });
      setSystemMessage("Sender gespeichert.");
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Sender konnte nicht gespeichert werden.");
    } finally {
      setStationSaving(false);
    }
  }

  async function handleManualConfirm(): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setManualConfirmBusy(true);
    setSystemMessage(null);
    try {
      await setStationManualConfirm(selectedStationId, true);
      setSystemMessage("Manual confirm gesetzt.");
      await refreshStationContext(selectedStationId);
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Manual confirm fehlgeschlagen.");
    } finally {
      setManualConfirmBusy(false);
    }
  }

  async function handleDeleteStation(): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    const accepted = window.confirm(`Sender ${selectedStation?.canonical_name || selectedStationId} wirklich löschen?`);
    if (!accepted) {
      return;
    }
    setDeleteBusy(true);
    setSystemMessage(null);
    try {
      await deleteStation(selectedStationId);
      setSystemMessage("Sender gelöscht.");
      setSelectedStationId(null);
      setSelectedStation(null);
      setStationControl(null);
      setStationDraft(null);
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Sender konnte nicht gelöscht werden.");
    } finally {
      setDeleteBusy(false);
    }
  }

  async function handleStartManualScan(): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setManualScanBusy(true);
    setSystemMessage(null);
    try {
      const response = await startManualScan({
        station_id: selectedStationId,
        mode: manualScanMode,
        target_url: stationDraft?.website_url || selectedStation?.website_url || null,
        max_pages: manualScanPages,
        force_rescan: true,
      });
      setSystemMessage(`Manual scan gestartet: ${response.status}`);
      setActiveTab("agent");
      await refreshAgentOverview(selectedStationId);
      setSelectedRunId(response.id);
      await refreshRun(response.id);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Manual scan konnte nicht gestartet werden.");
    } finally {
      setManualScanBusy(false);
    }
  }

  async function handleStartCountryDiscovery(): Promise<void> {
    setCountryDiscoveryLoading(true);
    setSystemMessage(null);
    try {
      const response = await startCountryDiscovery();
      setSystemMessage(response.message === "started" ? "Country discovery gestartet." : "Country discovery läuft bereits.");
      await refreshCountryDiscovery();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Country discovery konnte nicht gestartet werden.");
    } finally {
      setCountryDiscoveryLoading(false);
    }
  }

  async function handleOpenBrowserSession(): Promise<void> {
    const stationUrl = stationDraft?.website_url || selectedStation?.website_url || currentStation?.website_url || "";
    const startUrl = browserUrlDraft.trim() || stationUrl || undefined;
    setBrowserSessionError(null);
    try {
      const session = await createBrowserSession({
        start_url: startUrl || null,
        station_id: selectedStationId ?? currentStation?.id ?? null,
        run_id: selectedRunId,
        label: selectedStation?.canonical_name || currentStation?.canonical_name || "Station browser",
      });
      setBrowserSessionId(session.id);
      setSystemMessage(`Browser session ${session.id} geöffnet.`);
      await refreshBrowserSessions();
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser session konnte nicht geöffnet werden.");
    }
  }

  async function handleBrowserNavigate(): Promise<void> {
    if (!browserSessionId || !browserUrlDraft.trim()) {
      return;
    }
    try {
      await navigateBrowserSession(browserSessionId, { url: browserUrlDraft.trim() });
      await refreshBrowserSessions();
      setSystemMessage("Browser navigiert.");
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Navigation fehlgeschlagen.");
    }
  }

  async function handleBrowserAction(): Promise<void> {
    if (!browserSessionId) {
      return;
    }
    try {
      await performBrowserAction(browserSessionId, {
        action: browserAction,
        selector: browserSelectorDraft.trim() || undefined,
        value: browserValueDraft.trim() || undefined,
      });
      await refreshBrowserSessions();
      setSystemMessage(`Browser-Aktion ${browserAction} ausgeführt.`);
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser-Aktion fehlgeschlagen.");
    }
  }

  async function handleBrowserSnapshot(): Promise<void> {
    if (!browserSessionId) {
      return;
    }
    try {
      await snapshotBrowserSession(browserSessionId);
      await refreshBrowserSessions();
      setSystemMessage("Browser snapshot aktualisiert.");
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Snapshot fehlgeschlagen.");
    }
  }

  async function handleBrowserClose(): Promise<void> {
    if (!browserSessionId) {
      return;
    }
    try {
      await closeBrowserSession(browserSessionId);
      setBrowserSessionId(null);
      await refreshBrowserSessions();
      setSystemMessage("Browser session geschlossen.");
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser session konnte nicht geschlossen werden.");
    }
  }

  async function handleCopyDraft(): Promise<void> {
    if (!selectedStation) {
      return;
    }
    const draft = buildDraft(selectedStation);
    await navigator.clipboard.writeText([draft.subject, "", draft.body].join("\n"));
    setSystemMessage("Draft kopiert.");
  }

  function openArtifact(path: string | null | undefined): void {
    if (!path) {
      return;
    }
    window.open(getArtifactUrl(path), "_blank", "noopener,noreferrer");
  }

  const modalOpen = Boolean(selectedStationId && selectedStation);
  const selectedRunArtifactCount = selectedRun ? selectedRun.steps.filter((step) => Boolean(step.screenshot_path || step.dom_snapshot_path)).length : 0;

  return (
    <div className="radio-db-app">
      <header className="radio-db-hero">
        <div className="radio-db-hero__copy">
          <p className="radio-db-kicker">Radio DB Radical Overhaul</p>
          <h1>Stations zuerst. Modal editing. Control loops daneben.</h1>
          <p className="radio-db-hero__lede">
            Die neue Oberfläche hängt an den `/api/v1`-Routen und ersetzt Fokus-Seitenwechsel durch eine schnelle Senderliste mit direkter Bearbeitung.
          </p>
        </div>
        <div className="radio-db-hero__stats">
          <MetricCard label="Sender" value={summary.total.toString()} sublabel={`${summary.countries} Länder`} />
          <MetricCard label="Verified" value={summary.verified.toString()} sublabel={`${summary.rejected} rejected`} />
          <MetricCard label="Ø Confidence" value={formatPercent(summary.avgConfidence)} sublabel={currentStation ? currentStation.canonical_name : "kein Sender selektiert"} />
        </div>
      </header>

      <nav className="radio-db-tabs" aria-label="Dashboard Bereiche">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`radio-db-tab ${activeTab === tab.id ? "radio-db-tab--active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            <span>{tab.label}</span>
            <small>{tab.note}</small>
          </button>
        ))}
      </nav>

      <main className="radio-db-main">
        {systemMessage ? <div className="radio-db-banner">{systemMessage}</div> : null}
        {stationsError ? <div className="radio-db-banner radio-db-banner--error">{stationsError}</div> : null}
        {stationError ? <div className="radio-db-banner radio-db-banner--error">{stationError}</div> : null}
        {runsError ? <div className="radio-db-banner radio-db-banner--error">{runsError}</div> : null}
        {countryDiscoveryError ? <div className="radio-db-banner radio-db-banner--error">{countryDiscoveryError}</div> : null}
        {browserSessionError ? <div className="radio-db-banner radio-db-banner--error">{browserSessionError}</div> : null}

        {activeTab === "stations" && (
          <section className="radio-db-grid radio-db-grid--stations">
            <article className="radio-db-panel radio-db-panel--wide">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Senderliste</h2>
                  <p>Filter, sortieren, dann im Modal editieren.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshStations()} disabled={stationsLoading}>
                    Refresh
                  </button>
                  <button type="button" className="radio-db-button" onClick={() => setActiveTab("contact")}>
                    Contact Center
                  </button>
                </div>
              </div>

              <form
                className="radio-db-filters"
                onSubmit={(event) => {
                  event.preventDefault();
                  setFilters((current) => ({ ...current, q: searchDraft.trim(), page: 1 }));
                }}
              >
                <label>
                  Search
                  <input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="Station, City, Website" />
                </label>
                <label>
                  Country
                  <input
                    value={filters.country}
                    onChange={(event) => setFilters((current) => ({ ...current, country: event.target.value.toUpperCase().slice(0, 2), page: 1 }))}
                    placeholder="DE"
                    maxLength={2}
                  />
                </label>
                <label>
                  Status
                  <select value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value, page: 1 }))}>
                    <option value="">all</option>
                    <option value="candidate">candidate</option>
                    <option value="verified">verified</option>
                    <option value="rejected">rejected</option>
                  </select>
                </label>
                <label>
                  Page size
                  <select value={filters.pageSize} onChange={(event) => setFilters((current) => ({ ...current, pageSize: Number(event.target.value), page: 1 }))}>
                    <option value={25}>25</option>
                    <option value={40}>40</option>
                    <option value={80}>80</option>
                    <option value={120}>120</option>
                  </select>
                </label>
                <button type="submit" className="radio-db-button radio-db-button--ghost">
                  Apply
                </button>
              </form>

              <div className="radio-db-table-shell">
                {stationsLoading ? <div className="radio-db-empty">Loading stations…</div> : null}
                {!stationsLoading && stations?.items.length === 0 ? <div className="radio-db-empty">No stations match the current filter.</div> : null}
                <table className="radio-db-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Country</th>
                      <th>City</th>
                      <th>Status</th>
                      <th>Confidence</th>
                      <th>Signals</th>
                      <th>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stations?.items.map((item) => (
                      <tr
                        key={item.id}
                        className={selectedStationId === item.id ? "radio-db-row radio-db-row--active" : "radio-db-row"}
                        onClick={() => setSelectedStationId(item.id)}
                      >
                        <td>
                          <strong>{item.canonical_name}</strong>
                          <div className="radio-db-subtle">{item.website_url || "no website"}</div>
                        </td>
                        <td>{item.country_code || "--"}</td>
                        <td>{item.city || "--"}</td>
                        <td><span className={`radio-db-pill radio-db-pill--${statusTone(item.status)}`}>{item.status}</span></td>
                        <td>{formatPercent(item.confidence_score)}</td>
                        <td>
                          <div className="radio-db-mini-metrics">
                            <span>{item.submission_count} submissions</span>
                            <span>{item.people_count} people</span>
                            <span>{item.genre_count} genres</span>
                          </div>
                        </td>
                        <td>{formatDate(item.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </article>

            <aside className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Quick context</h2>
                  <p>Focus on the selected station and jump directly into the modal.</p>
                </div>
              </div>
              {currentStation ? (
                <div className="radio-db-stack">
                  <div className="radio-db-context-card">
                    <h3>{currentStation.canonical_name}</h3>
                    <p>{currentStation.country_code || "--"} · {currentStation.city || "no city"}</p>
                    <span className={`radio-db-pill radio-db-pill--${statusTone(currentStation.status)}`}>{currentStation.status}</span>
                  </div>
                  <div className="radio-db-summary-list">
                    <div><span>Website</span><strong>{selectedStation?.website_url || currentStation.website_url || "-"}</strong></div>
                    <div><span>Language</span><strong>{selectedStation?.language || currentStation.language || "-"}</strong></div>
                    <div><span>Submissions</span><strong>{selectedStation?.submissions.length ?? currentStation.submission_count}</strong></div>
                    <div><span>People</span><strong>{selectedStation?.people.length ?? currentStation.people_count}</strong></div>
                  </div>
                  <button type="button" className="radio-db-button" onClick={() => setSelectedStationId(currentStation.id)}>
                    Open modal editor
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("contact")}>
                    Compose contact draft
                  </button>
                </div>
              ) : (
                <div className="radio-db-empty">Select a station to open the detail modal.</div>
              )}
            </aside>
          </section>
        )}

        {activeTab === "contact" && (
          <section className="radio-db-grid radio-db-grid--two">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Contact Center</h2>
                  <p>Station-scoped draft with language hint and evidence context.</p>
                </div>
              </div>
              {selectedStation ? (
                <div className="radio-db-stack">
                  <div className="radio-db-summary-list">
                    <div><span>Station</span><strong>{selectedStation.canonical_name}</strong></div>
                    <div><span>Locale</span><strong>{selectedStation.country_code || "--"} / {selectedStation.language || "--"}</strong></div>
                    <div><span>Forms</span><strong>{selectedStation.forms.length}</strong></div>
                    <div><span>Contacts</span><strong>{selectedStation.contacts.length}</strong></div>
                  </div>
                  <div className="radio-db-draft">
                    <label>
                      Subject
                      <input value={contactDraft.subject} readOnly />
                    </label>
                    <label>
                      Draft body
                      <textarea rows={14} value={contactDraft.body} readOnly />
                    </label>
                    <div className="radio-db-panel__actions">
                      <button type="button" className="radio-db-button" onClick={() => void handleCopyDraft()}>
                        Copy draft
                      </button>
                      <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("stations")}>
                        Back to stations
                      </button>
                    </div>
                    <div className="radio-db-subtle">{contactDraft.localeHint}</div>
                  </div>
                </div>
              ) : (
                <div className="radio-db-empty">Pick a station in the list to build a localized outreach draft.</div>
              )}
            </article>

            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>People and routes</h2>
                  <p>Direct targets from the station record.</p>
                </div>
              </div>
              {selectedStation ? (
                <div className="radio-db-stack">
                  <div>
                    <h3 className="radio-db-section-title">Contacts</h3>
                    <div className="radio-db-list">
                      {selectedStation.contacts.length > 0 ? selectedStation.contacts.map((contact) => (
                        <div key={contact.id} className="radio-db-list-item">
                          <strong>{contact.name || contact.role}</strong>
                          <span>{contact.role}</span>
                          <small>{contact.email || contact.contact_url || contact.notes || "No direct route"}</small>
                        </div>
                      )) : <div className="radio-db-empty">No direct contacts recorded yet.</div>}
                    </div>
                  </div>
                  <div>
                    <h3 className="radio-db-section-title">People</h3>
                    <div className="radio-db-list">
                      {selectedStation.people.length > 0 ? selectedStation.people.map((person) => (
                        <div key={person.id} className="radio-db-list-item">
                          <strong>{person.name || person.show_name || person.role}</strong>
                          <span>{person.role}</span>
                          <small>{person.email || person.contact_url || person.linkedin_url || person.musical_preferences || "No public route"}</small>
                        </div>
                      )) : <div className="radio-db-empty">No people records yet.</div>}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="radio-db-empty">Station detail required.</div>
              )}
            </article>
          </section>
        )}

        {activeTab === "agent" && (
          <section className="radio-db-grid radio-db-grid--two">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Agent Control</h2>
                  <p>Recent runs plus manual scan controls for the selected station.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshAgentOverview()} disabled={runsLoading}>
                    Refresh runs
                  </button>
                  <button type="button" className="radio-db-button" onClick={() => void handleStartCountryDiscovery()} disabled={countryDiscoveryLoading}>
                    Start country discovery
                  </button>
                </div>
              </div>

              <div className="radio-db-run-launcher">
                <div className="radio-db-mini-metrics">
                  <span>{runsTotal} total runs</span>
                  <span>{selectedStation ? `selected: ${selectedStation.canonical_name}` : "no station selected"}</span>
                </div>
                <label>
                  mode
                  <input value={manualScanMode} onChange={(event) => setManualScanMode(event.target.value)} />
                </label>
                <label>
                  max pages
                  <input type="number" min={1} max={10} value={manualScanPages} onChange={(event) => setManualScanPages(Number(event.target.value) || 1)} />
                </label>
                <button type="button" className="radio-db-button" onClick={() => void handleStartManualScan()} disabled={!selectedStationId || manualScanBusy}>
                  Start manual scan
                </button>
              </div>

              <div className="radio-db-list radio-db-list--runs">
                {runsLoading ? <div className="radio-db-empty">Loading agent runs…</div> : null}
                {agentRuns.length === 0 && !runsLoading ? <div className="radio-db-empty">No runs available.</div> : null}
                {agentRuns.map((run) => (
                  <button
                    type="button"
                    key={run.id}
                    className={`radio-db-run ${selectedRunId === run.id ? "radio-db-run--active" : ""}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <div className="radio-db-run__head">
                      <strong>#{run.id} {run.station_name}</strong>
                      <span className={`radio-db-pill radio-db-pill--${statusTone(run.status)}`}>{run.status}</span>
                    </div>
                    <div className="radio-db-subtle">{run.mode} · {run.goal}</div>
                    <div className="radio-db-mini-metrics">
                      <span>{run.step_count} steps</span>
                      <span>{run.issue_count} issues</span>
                      <span>{formatPercent(run.confidence)}</span>
                    </div>
                  </button>
                ))}
              </div>
            </article>

            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Run detail</h2>
                  <p>Artefacts and live step evidence for the selected run.</p>
                </div>
              </div>
              {runLoading ? <div className="radio-db-empty">Loading run detail…</div> : null}
              {selectedRun ? (
                <div className="radio-db-stack">
                  <div className="radio-db-context-card">
                    <h3>{selectedRun.station_name}</h3>
                    <p>{selectedRun.mode} · {selectedRun.goal}</p>
                    <div className="radio-db-mini-metrics">
                      <span>{selectedRun.current_state}</span>
                      <span>{formatPercent(selectedRun.confidence)}</span>
                      <span>{selectedRunArtifactCount} artefacts</span>
                    </div>
                  </div>
                  <div className="radio-db-list">
                    {selectedRun.steps.map((step) => (
                      <div key={step.id} className="radio-db-list-item">
                        <div className="radio-db-run__head">
                          <strong>Step {step.step_index}</strong>
                          <span>{step.state_before} → {step.state_after}</span>
                        </div>
                        <small>confidence {formatPercent(step.confidence)} · {step.latency_ms}ms</small>
                        <div className="radio-db-panel__actions">
                          <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => openArtifact(step.screenshot_path)} disabled={!step.screenshot_path}>
                            Screenshot
                          </button>
                          <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => openArtifact(step.dom_snapshot_path)} disabled={!step.dom_snapshot_path}>
                            DOM snapshot
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                  {selectedRun.latest_assessment ? (
                    <div className="radio-db-summary-list">
                      <div><span>Automation readiness</span><strong>{formatPercent(selectedRun.latest_assessment.automation_readiness)}</strong></div>
                      <div><span>Risk</span><strong>{formatPercent(selectedRun.latest_assessment.risk_score)}</strong></div>
                      <div><span>Assessment</span><strong>{selectedRun.latest_assessment.status}</strong></div>
                    </div>
                  ) : null}
                  {selectedRun.issues.length > 0 ? (
                    <div className="radio-db-list">
                      {selectedRun.issues.map((issue) => (
                        <div key={issue.id} className="radio-db-list-item">
                          <strong>{issue.title}</strong>
                          <span>{issue.severity} · {issue.status}</span>
                          <small>{issue.details || issue.issue_type}</small>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="radio-db-empty">Select a run to inspect steps and artifacts.</div>
              )}
            </article>
          </section>
        )}

        {activeTab === "data" && (
          <section className="radio-db-grid radio-db-grid--two">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Data Control</h2>
                  <p>Operational context, focus data, and discovery snapshot.</p>
                </div>
              </div>
              <div className="radio-db-summary-list">
                <div><span>Total stations</span><strong>{summary.total}</strong></div>
                <div><span>Verified</span><strong>{summary.verified}</strong></div>
                <div><span>Rejected</span><strong>{summary.rejected}</strong></div>
                <div><span>Average confidence</span><strong>{formatPercent(summary.avgConfidence)}</strong></div>
              </div>
              <div className="radio-db-box-grid">
                <div className="radio-db-box">
                  <h3>Country discovery</h3>
                  <p>{countryDiscoveryLoading ? "Loading…" : "Snapshot and memory view from the discovery pipeline."}</p>
                  <pre>{JSON.stringify(countryDiscovery?.state ?? {}, null, 2)}</pre>
                </div>
                <div className="radio-db-box">
                  <h3>Memory</h3>
                  <pre>{JSON.stringify(countryDiscovery?.memory ?? {}, null, 2)}</pre>
                </div>
              </div>
            </article>

            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Browser Session</h2>
                  <p>Remote session, navigation and action queue for the selected station.</p>
                </div>
              </div>
              <div className="radio-db-run-launcher">
                <label>
                  Start URL
                  <input
                    value={browserUrlDraft}
                    onChange={(event) => setBrowserUrlDraft(event.target.value)}
                    placeholder={selectedStation?.website_url || currentStation?.website_url || "https://example.com"}
                  />
                </label>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button" onClick={() => void handleOpenBrowserSession()}>
                    Open browser session
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleBrowserNavigate()} disabled={!browserSessionId}>
                    Navigate
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleBrowserSnapshot()} disabled={!browserSessionId}>
                    Snapshot
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleBrowserClose()} disabled={!browserSessionId}>
                    Close
                  </button>
                </div>
                <div className="radio-db-grid radio-db-grid--two-column">
                  <label>
                    Action
                    <select value={browserAction} onChange={(event) => setBrowserAction(event.target.value as typeof browserAction)}>
                      <option value="goto">goto</option>
                      <option value="click">click</option>
                      <option value="fill">fill</option>
                      <option value="press">press</option>
                      <option value="select">select</option>
                      <option value="wait">wait</option>
                    </select>
                  </label>
                  <label>
                    Selector
                    <input value={browserSelectorDraft} onChange={(event) => setBrowserSelectorDraft(event.target.value)} placeholder="input[name=email]" />
                  </label>
                </div>
                <label>
                  Value
                  <input value={browserValueDraft} onChange={(event) => setBrowserValueDraft(event.target.value)} placeholder="text, key or url" />
                </label>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button" onClick={() => void handleBrowserAction()} disabled={!browserSessionId}>
                    Run action
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshBrowserSessions()} disabled={browserSessionsLoading}>
                    Refresh sessions
                  </button>
                </div>
              </div>
              <div className="radio-db-box">
                <h3>Sessions</h3>
                {browserSessionsLoading ? <div className="radio-db-empty">Loading sessions…</div> : null}
                <div className="radio-db-list">
                  {browserSessions.map((session) => (
                    <button
                      type="button"
                      key={session.id}
                      className={`radio-db-run ${browserSessionId === session.id ? "radio-db-run--active" : ""}`}
                      onClick={async () => {
                        setBrowserSessionId(session.id);
                        try {
                          const response = await getBrowserSession(session.id);
                          setBrowserUrlDraft(response.url);
                        } catch {
                          // ignore selection refresh errors
                        }
                      }}
                    >
                      <div className="radio-db-run__head">
                        <strong>{session.title || session.url || session.id}</strong>
                        <span className={`radio-db-pill radio-db-pill--${statusTone(session.status)}`}>{session.status}</span>
                      </div>
                      <div className="radio-db-subtle">{session.url}</div>
                      <div className="radio-db-mini-metrics">
                        <span>{session.action_count} actions</span>
                        <span>{session.event_count} events</span>
                        <span>{formatDate(session.updated_at)}</span>
                      </div>
                      {session.last_error ? <small>{session.last_error}</small> : null}
                    </button>
                  ))}
                </div>
              </div>
              <div className="radio-db-box">
                <h3>Live timeline</h3>
                {browserSessionDetail ? (
                  <div className="radio-db-stack">
                    <div className="radio-db-summary-list">
                      <div><span>Session</span><strong>{browserSessionDetail.title || browserSessionDetail.id}</strong></div>
                      <div><span>URL</span><strong>{browserSessionDetail.url}</strong></div>
                      <div><span>Status</span><strong>{browserSessionDetail.status}</strong></div>
                      <div><span>Events</span><strong>{browserSessionDetail.event_count}</strong></div>
                    </div>
                    <div className="radio-db-list radio-db-list--runs radio-db-list--timeline">
                      {browserSessionDetail.timeline.slice().reverse().map((event) => (
                        <div key={`${String(event.id)}-${String(event.at)}`} className="radio-db-list-item">
                          <strong>{String(event.type)}</strong>
                          <span>{String(event.url || "")}</span>
                          <small>{String(event.at || "")}</small>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="radio-db-empty">Select or open a session to follow its live event stream.</div>
                )}
              </div>
            </article>
          </section>
        )}
      </main>

      {modalOpen && selectedStation && stationDraft ? (
        <div className="radio-db-modal" role="dialog" aria-modal="true" aria-label="Station editor">
          <div className="radio-db-modal__backdrop" onClick={() => setSelectedStationId(null)} />
          <div className="radio-db-modal__panel">
            <div className="radio-db-modal__head">
              <div>
                <p className="radio-db-kicker">Station detail</p>
                <h2>{selectedStation.canonical_name}</h2>
                <p>{selectedStation.country_code || "--"} · {selectedStation.city || "no city"} · {selectedStation.language || "no language"}</p>
              </div>
              <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setSelectedStationId(null)}>
                Close
              </button>
            </div>

            <div className="radio-db-modal__content">
              <div className="radio-db-modal__left">
                <div className="radio-db-summary-list">
                  <div><span>Status</span><strong>{selectedStation.status}</strong></div>
                  <div><span>Confidence</span><strong>{formatPercent(selectedStation.confidence_score)}</strong></div>
                  <div><span>Manual confirm</span><strong>{stationControl?.station_id ? "available" : "loading"}</strong></div>
                  <div><span>Updated</span><strong>{formatDate(selectedStation.updated_at)}</strong></div>
                </div>

                <div className="radio-db-edit-form">
                  <label>
                    Canonical name
                    <input value={stationDraft.canonical_name} onChange={(event) => setStationDraft((current) => current ? { ...current, canonical_name: event.target.value } : current)} />
                  </label>
                  <div className="radio-db-grid radio-db-grid--two-column">
                    <label>
                      Country code
                      <input value={stationDraft.country_code} onChange={(event) => setStationDraft((current) => current ? { ...current, country_code: event.target.value.toUpperCase().slice(0, 2) } : current)} />
                    </label>
                    <label>
                      Language
                      <input value={stationDraft.language} onChange={(event) => setStationDraft((current) => current ? { ...current, language: event.target.value } : current)} />
                    </label>
                  </div>
                  <label>
                    City
                    <input value={stationDraft.city} onChange={(event) => setStationDraft((current) => current ? { ...current, city: event.target.value } : current)} />
                  </label>
                  <label>
                    Website
                    <input value={stationDraft.website_url} onChange={(event) => setStationDraft((current) => current ? { ...current, website_url: event.target.value } : current)} />
                  </label>
                  <label>
                    Stream URL
                    <input value={stationDraft.stream_url} onChange={(event) => setStationDraft((current) => current ? { ...current, stream_url: event.target.value } : current)} />
                  </label>
                  <div className="radio-db-grid radio-db-grid--two-column">
                    <label>
                      Status
                      <select value={stationDraft.status} onChange={(event) => setStationDraft((current) => current ? { ...current, status: event.target.value as StationDraft["status"] } : current)}>
                        <option value="candidate">candidate</option>
                        <option value="verified">verified</option>
                        <option value="rejected">rejected</option>
                      </select>
                    </label>
                    <label>
                      Confidence
                      <input type="number" min={0} max={1} step={0.01} value={stationDraft.confidence_score} onChange={(event) => setStationDraft((current) => current ? { ...current, confidence_score: event.target.value } : current)} />
                    </label>
                  </div>
                </div>

                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button" onClick={() => void handleStationSave()} disabled={stationSaving}>
                    Save station
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleManualConfirm()} disabled={manualConfirmBusy}>
                    Manual confirm
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleDeleteStation()} disabled={deleteBusy}>
                    Delete station
                  </button>
                </div>

                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("contact")}>Open contact center</button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("agent")}>Open agent control</button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleStartManualScan()} disabled={manualScanBusy}>
                    Scan this station
                  </button>
                </div>
              </div>

              <div className="radio-db-modal__right">
                <div className="radio-db-box">
                  <h3>Submissions</h3>
                  <div className="radio-db-list">
                    {selectedStation.submissions.map((submission) => (
                      <div key={submission.id} className="radio-db-list-item">
                        <strong>{submission.method}</strong>
                        <span>{submission.email || submission.url || "no route"}</span>
                        <small>{submission.requirements || "No requirements recorded."}</small>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="radio-db-box">
                  <h3>Programs</h3>
                  <div className="radio-db-list">
                    {selectedStation.programs.map((program) => (
                      <div key={program.id} className="radio-db-list-item">
                        <strong>{program.name}</strong>
                        <span>{program.schedule || "unscheduled"}</span>
                        <small>{program.description || "No program description."}</small>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="radio-db-box">
                  <h3>Forms</h3>
                  <div className="radio-db-list">
                    {selectedStation.forms.map((form) => (
                      <div key={form.id} className="radio-db-list-item">
                        <strong>{form.page_title || form.url}</strong>
                        <span>{form.form_type} · {form.status}</span>
                        <small>{form.language || "lang?"} · {form.requires_login ? "login" : "public"} · {form.has_captcha ? "captcha" : "no captcha"}</small>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MetricCard({ label, value, sublabel }: { label: string; value: string; sublabel: string }): JSX.Element {
  return (
    <div className="radio-db-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{sublabel}</small>
    </div>
  );
}

export default App;
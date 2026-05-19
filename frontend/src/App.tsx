import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ApiPermissionMode,
  AgentRunDetailResponse,
  AgentRunListItem,
  BrowserSessionState,
  ContactDraftResponse,
  ContactOutcomeResponse,
  ContactSendResponse,
  ContactTemplateDTO,
  CountryDiscoverySnapshotResponse,
  OutreachCampaignDTO,
  OutreachCampaignMonitorResponse,
  DataControlOverviewResponse,
  StationGroupDetailResponse,
  StationGroupListItem,
  StationGroupStationDTO,
  addStationToGroup,
  addStationsToGroup,
  archiveOutreachCampaign,
  closeBrowserSession,
  createOutreachCampaign,
  createStationGroup,
  createBrowserSession,
  createContactSend,
  createStationContactDraft,
  getAgentRunDetail,
  getArtifactUrl,
  getBrowserSession,
  getCountryDiscoverySnapshot,
  getDataControlOverview,
  getOutreachCampaignMonitor,
  getStationGroup,
  getStationContactDraft,
  getStationControl,
  getStationDetail,
  listAgentRuns,
  listBrowserSessions,
  listContactTemplates,
  listDraftSends,
  listSendOutcomes,
  listStationGroups,
  listOutreachCampaigns,
  listStationMemberships,
  listStationContactDrafts,
  listStations,
  navigateBrowserSession,
  patchOutreachCampaign,
  performBrowserAction,
  previewStationContactDraft,
  removeStationFromGroup,
  setApiPermissionMode,
  setStationManualConfirm,
  snapshotBrowserSession,
  startCountryDiscovery,
  startManualScan,
  updateScanFocus,
  updateStation,
  deleteStation,
  generateOutreachEmail,
  StationControlDetailResponse,
  StationDetailResponse,
  StationListItem,
  StationListResponse,
} from "./radioDbApi";

type TabId = "stations" | "runs" | "campaigns" | "system";
type StationPanelTab = "overview" | "outreach" | "forms" | "activity" | "edit";
type BrowserActionType = "click" | "fill" | "press" | "select" | "goto" | "wait";
type ScanFocus = "off" | "international" | "dach" | "anglo" | "eu_core" | "top_major";
type CampaignMonitorFilter = "all" | "drafted" | "sent" | "clicked" | "failed" | "blocked" | "simulated";

const STATION_FILTERS_STORAGE_KEY = "radio-db-station-filters";
const OUTREACH_CAMPAIGN_ID_KEY = "radio-db-outreach-campaign-id";
const EDITORIAL_ASSESSMENT_KIND = "editorial_fit_v1";

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

interface CampaignDraft {
  name: string;
  artist: string;
  songTitle: string;
  pitch: string;
  primaryLanguage: string;
  releaseDate: string;
  pressReleaseUrl: string;
  trackingCode: string;
  contactName: string;
  contactEmail: string;
  contactPhone: string;
  labelName: string;
  artistCity: string;
  artistCountry: string;
  artistWebsite: string;
  genre: string;
  spotifyUrl: string;
  soundcloudUrl: string;
  youtubeUrl: string;
  bandcampUrl: string;
  audioFileUrl: string;
  downloadUrl: string;
  pressPhotoUrl: string;
  coverArtUrl: string;
  pressKitUrl: string;
  lyricsUrl: string;
  labelCode: string;
  artistBioShort: string;
  artistBioLong: string;
  notes: string;
  referenceTemplate: string;
}

function emptyCampaignDraft(): CampaignDraft {
  return {
    name: "",
    artist: "",
    songTitle: "",
    pitch: "",
    primaryLanguage: "",
    releaseDate: "",
    pressReleaseUrl: "",
    trackingCode: "",
    contactName: "Jonas Sercombe",
    contactEmail: "radio@public-air.net",
    contactPhone: "+4915777870496",
    labelName: "Public Air Records",
    artistCity: "",
    artistCountry: "",
    artistWebsite: "",
    genre: "",
    spotifyUrl: "",
    soundcloudUrl: "",
    youtubeUrl: "",
    bandcampUrl: "",
    audioFileUrl: "",
    downloadUrl: "",
    pressPhotoUrl: "",
    coverArtUrl: "",
    pressKitUrl: "",
    lyricsUrl: "",
    labelCode: "",
    artistBioShort: "",
    artistBioLong: "",
    notes: "",
    referenceTemplate: "",
  };
}

function campaignDtoToForm(row: OutreachCampaignDTO): CampaignDraft {
  const defaults = row.submission_defaults ?? {};
  const profile = row.artist_profile ?? {};
  const assets = row.release_assets ?? {};
  return {
    name: row.name,
    artist: row.artist_name,
    songTitle: row.song_title,
    pitch: row.pitch_text,
    primaryLanguage: row.song_language ?? "",
    releaseDate: row.release_date ?? "",
    pressReleaseUrl: row.press_release_url ?? "",
    trackingCode: row.tracking_code ?? "",
    contactName: defaults.contact_name ?? "Jonas Sercombe",
    contactEmail: defaults.contact_email ?? "radio@public-air.net",
    contactPhone: defaults.contact_phone ?? "+4915777870496",
    labelName: defaults.label_name ?? "Public Air Records",
    artistCity: profile.artist_city ?? "",
    artistCountry: profile.artist_country ?? "",
    artistWebsite: profile.artist_website ?? "",
    genre: profile.genre ?? "",
    spotifyUrl: profile.spotify_url ?? "",
    soundcloudUrl: profile.soundcloud_url ?? "",
    youtubeUrl: profile.youtube_url ?? "",
    bandcampUrl: profile.bandcamp_url ?? "",
    audioFileUrl: assets.audio_file_url ?? "",
    downloadUrl: assets.download_url ?? "",
    pressPhotoUrl: assets.press_photo_url ?? "",
    coverArtUrl: assets.cover_art_url ?? "",
    pressKitUrl: assets.press_kit_url ?? "",
    lyricsUrl: assets.lyrics_url ?? "",
    labelCode: assets.label_code ?? "",
    artistBioShort: profile.artist_bio_short ?? "",
    artistBioLong: profile.artist_bio_long ?? "",
    notes: row.operator_notes ?? "",
    referenceTemplate: row.reference_template ?? "",
  };
}

interface StationGroupDraft {
  name: string;
  description: string;
  artistKey: string;
}

interface MonitorCandidate {
  id: string;
  label: string;
  route: string;
  type: string;
  status: string;
  confidence: number | null;
  note: string;
}

const TABS: Array<{ id: TabId; label: string; note: string }> = [
  { id: "stations", label: "Stations", note: "Master DB, Auswahl und manuelle Operator-Arbeit" },
  { id: "runs", label: "Runs", note: "Scanner, Browser und Review-Konsole" },
  { id: "campaigns", label: "Campaigns", note: "Song-zentrierte Batch-Vorbereitung" },
  { id: "system", label: "System", note: "Monitoring, Quoten und Worker-Zustand" },
];

const STATION_PANEL_TABS: Array<{ id: StationPanelTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "outreach", label: "Outreach" },
  { id: "forms", label: "Forms" },
  { id: "activity", label: "Activity" },
  { id: "edit", label: "Edit" },
];

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
    case "ready":
    case "sent":
    case "clicked":
      return "positive";
    case "rejected":
    case "failed":
    case "blocked":
      return "negative";
    case "paused":
    case "candidate":
    case "draft":
    case "drafted":
    case "simulated":
      return "neutral";
    default:
      return "muted";
  }
}

function outreachLabel(value: string): string {
  switch (value) {
    case "verified_submission":
    case "submission_confirmed":
      return "verified submission";
    case "contact_only":
    case "contact_only_quality":
      return "contact only";
    case "needs_review":
      return "needs review";
    default:
      return value.replaceAll("_", " ");
  }
}

function outreachTone(value: string): string {
  return value === "verified_submission" || value === "submission_confirmed" ? "positive" : "neutral";
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object") {
    return value as Record<string, unknown>;
  }
  return {};
}

function pluralize(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function nextBestAction(
  station: StationListItem | null,
  detail: StationDetailResponse | null,
  draft: ContactDraftResponse | null,
): string {
  if (!station) {
    return "Sender auswählen";
  }
  if (detail && detail.forms.length > 0) {
    return "Submission-Form prüfen oder Run starten";
  }
  if (draft?.available_routes.length) {
    return "Kontakt-Draft prüfen und Route wählen";
  }
  if (station.website_url) {
    return "Website scannen und Submission-Weg finden";
  }
  return "Daten ergänzen und Station verifizieren";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return String(value);
    }
    if (typeof value === "boolean") {
      return value ? "yes" : "no";
    }
  }
  return "";
}

function browserEventLabel(event: Record<string, unknown>): string {
  const payload = asRecord(event.payload);
  const type = String(event.type || "event");
  switch (type) {
    case "opened":
      return `Browser geoeffnet${payload.start_url ? `: ${String(payload.start_url)}` : ""}`;
    case "snapshot":
      return `Screenshot gespeichert${payload.label ? ` (${String(payload.label)})` : ""}`;
    case "snapshot_requested":
      return "Snapshot angefordert";
    case "navigate_started":
      return `Navigation gestartet: ${String(payload.url || event.url || "")}`;
    case "navigate_completed":
      return `Navigation abgeschlossen: ${String(payload.url || event.url || "")}`;
    case "navigate_failed":
      return `Navigation fehlgeschlagen: ${String(payload.error || "")}`;
    case "action_started":
      return `Aktion gestartet: ${String(payload.action || "action")}`;
    case "action_completed":
      return `Aktion abgeschlossen: ${String(payload.action || "action")}`;
    case "action_failed":
      return `Aktion fehlgeschlagen: ${String(payload.error || "")}`;
    case "closing":
      return "Session wird geschlossen";
    default:
      return type.replaceAll("_", " ");
  }
}

function browserEventPhase(event: Record<string, unknown>): string {
  const type = String(event.type || "");
  if (type.includes("snapshot")) {
    return "Screenshot";
  }
  if (type.includes("navigate") || type === "opened") {
    return "Navigation";
  }
  if (type.includes("action")) {
    return "Action";
  }
  if (type.includes("fail") || type.includes("error")) {
    return "Blocker";
  }
  return "Session";
}

function browserEventTone(event: Record<string, unknown>): string {
  const type = String(event.type || "");
  const status = String(event.status || "");
  if (type.includes("fail") || status === "error") {
    return "negative";
  }
  if (type.includes("completed") || type === "opened" || type === "snapshot") {
    return "positive";
  }
  return "neutral";
}

function loadStoredStationFilters(): {
  filters: {
    q: string;
    country: string;
    status: string;
    hasSubmission: "any" | "yes" | "no";
    hasPeople: "any" | "yes" | "no";
    outreach: "" | "verified_submission" | "contact_only";
    minConfidence: number;
    page: number;
    pageSize: number;
    sortBy: "updated_at" | "confidence" | "name";
    sortOrder: "asc" | "desc";
  };
  searchDraft: string;
} {
  const defaults = {
    filters: {
      q: "",
      country: "",
      status: "",
      hasSubmission: "any" as const,
      hasPeople: "any" as const,
      outreach: "" as const,
      minConfidence: 0,
      page: 1,
      pageSize: 40,
      sortBy: "updated_at" as const,
      sortOrder: "desc" as const,
    },
    searchDraft: "",
  };

  if (typeof window === "undefined") {
    return defaults;
  }

  try {
    const raw = window.localStorage.getItem(STATION_FILTERS_STORAGE_KEY);
    if (!raw) {
      return defaults;
    }
    const parsed = JSON.parse(raw) as Partial<typeof defaults>;
    const filters = parsed.filters ?? {};
    return {
      filters: {
        q: typeof filters.q === "string" ? filters.q : defaults.filters.q,
        country: typeof filters.country === "string" ? filters.country : defaults.filters.country,
        status: typeof filters.status === "string" ? filters.status : defaults.filters.status,
        hasSubmission: filters.hasSubmission === "yes" || filters.hasSubmission === "no" ? filters.hasSubmission : defaults.filters.hasSubmission,
        hasPeople: filters.hasPeople === "yes" || filters.hasPeople === "no" ? filters.hasPeople : defaults.filters.hasPeople,
        outreach:
          filters.outreach === "verified_submission" || filters.outreach === "submission_confirmed"
            ? "verified_submission"
            : filters.outreach === "contact_only" || filters.outreach === "contact_only_quality"
              ? "contact_only"
              : defaults.filters.outreach,
        minConfidence: typeof filters.minConfidence === "number" ? filters.minConfidence : defaults.filters.minConfidence,
        page: typeof filters.page === "number" && filters.page >= 1 ? filters.page : defaults.filters.page,
        pageSize: typeof filters.pageSize === "number" ? filters.pageSize : defaults.filters.pageSize,
        sortBy: filters.sortBy === "confidence" || filters.sortBy === "name" ? filters.sortBy : defaults.filters.sortBy,
        sortOrder: filters.sortOrder === "asc" ? "asc" : defaults.filters.sortOrder,
      },
      searchDraft: typeof parsed.searchDraft === "string" ? parsed.searchDraft : defaults.searchDraft,
    };
  } catch {
    return defaults;
  }
}

function App(): JSX.Element {
  const storedStationFilters = loadStoredStationFilters();
  const [activeTab, setActiveTab] = useState<TabId>("stations");
  const [stationPanelTab, setStationPanelTab] = useState<StationPanelTab>("overview");
  const [filters, setFilters] = useState(storedStationFilters.filters);
  const [searchDraft, setSearchDraft] = useState(storedStationFilters.searchDraft);
  const [stations, setStations] = useState<StationListResponse | null>(null);
  const [stationsLoading, setStationsLoading] = useState(false);
  const [stationsError, setStationsError] = useState<string | null>(null);
  const [selectedStationId, setSelectedStationId] = useState<number | null>(null);
  const [selectedStationIds, setSelectedStationIds] = useState<number[]>([]);
  const [campaignStationIds, setCampaignStationIds] = useState<number[]>([]);
  const [stationGroups, setStationGroups] = useState<StationGroupListItem[]>([]);
  const [stationGroupsLoading, setStationGroupsLoading] = useState(false);
  const [selectedGroupId, setSelectedGroupId] = useState<number | null>(null);
  const [selectedGroupDetail, setSelectedGroupDetail] = useState<StationGroupDetailResponse | null>(null);
  const [stationMemberships, setStationMemberships] = useState<StationGroupListItem[]>([]);
  const [groupTargetId, setGroupTargetId] = useState<number | "">("");
  const [groupDraft, setGroupDraft] = useState<StationGroupDraft>({
    name: "",
    description: "",
    artistKey: "",
  });
  const [groupBusy, setGroupBusy] = useState(false);
  const [selectedStation, setSelectedStation] = useState<StationDetailResponse | null>(null);
  const [stationControl, setStationControl] = useState<StationControlDetailResponse | null>(null);
  const [stationLoading, setStationLoading] = useState(false);
  const [stationError, setStationError] = useState<string | null>(null);
  const [stationDraft, setStationDraft] = useState<StationDraft | null>(null);
  const [stationSaving, setStationSaving] = useState(false);
  const [manualConfirmBusy, setManualConfirmBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [bulkDeleteBusy, setBulkDeleteBusy] = useState(false);
  const [manualScanMode, setManualScanMode] = useState("scan");
  const [manualScanPages, setManualScanPages] = useState(1);
  const [manualScanBusy, setManualScanBusy] = useState(false);
  const [agentRuns, setAgentRuns] = useState<AgentRunListItem[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runStatusFilter, setRunStatusFilter] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [selectedRun, setSelectedRun] = useState<AgentRunDetailResponse | null>(null);
  const [runLoading, setRunLoading] = useState(false);
  const [countryDiscovery, setCountryDiscovery] = useState<CountryDiscoverySnapshotResponse | null>(null);
  const [countryDiscoveryLoading, setCountryDiscoveryLoading] = useState(false);
  const [countryDiscoveryError, setCountryDiscoveryError] = useState<string | null>(null);
  const [dataOverview, setDataOverview] = useState<DataControlOverviewResponse | null>(null);
  const [dataOverviewLoading, setDataOverviewLoading] = useState(false);
  const [dataOverviewError, setDataOverviewError] = useState<string | null>(null);
  const [scanFocusDraft, setScanFocusDraft] = useState<ScanFocus>("off");
  const [browserSessions, setBrowserSessions] = useState<BrowserSessionState[]>([]);
  const [browserSessionsLoading, setBrowserSessionsLoading] = useState(false);
  const [browserSessionError, setBrowserSessionError] = useState<string | null>(null);
  const [browserSessionId, setBrowserSessionId] = useState<string | null>(null);
  const [browserSessionDetail, setBrowserSessionDetail] = useState<BrowserSessionState | null>(null);
  const [browserUrlDraft, setBrowserUrlDraft] = useState("");
  const [browserSelectorDraft, setBrowserSelectorDraft] = useState("");
  const [browserValueDraft, setBrowserValueDraft] = useState("");
  const [browserAction, setBrowserAction] = useState<BrowserActionType>("goto");
  const [permissionMode, setPermissionMode] = useState<ApiPermissionMode>(() => {
    if (typeof window === "undefined") {
      return "dry-run";
    }
    const stored = window.localStorage.getItem("radio-db-permission-mode");
    return stored === "read-only" || stored === "dry-run" || stored === "execute" ? stored : "dry-run";
  });
  const [contactDraft, setContactDraft] = useState<ContactDraftResponse | null>(null);
  const [contactDraftLoading, setContactDraftLoading] = useState(false);
  const [contactTemplates, setContactTemplates] = useState<ContactTemplateDTO[]>([]);
  const [stationDrafts, setStationDrafts] = useState<ContactDraftResponse[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<number | "default">("default");
  const [contactSends, setContactSends] = useState<ContactSendResponse[]>([]);
  const [selectedSendId, setSelectedSendId] = useState<number | null>(null);
  const [contactOutcomes, setContactOutcomes] = useState<ContactOutcomeResponse[]>([]);
  const [contactSendLoading, setContactSendLoading] = useState(false);
  const [systemMessage, setSystemMessage] = useState<string | null>(null);
  const [campaignDraft, setCampaignDraft] = useState<CampaignDraft>(() => emptyCampaignDraft());
  const [selectedOutreachCampaignId, setSelectedOutreachCampaignId] = useState<number | null>(() => {
    if (typeof window === "undefined") {
      return null;
    }
    const raw = window.localStorage.getItem(OUTREACH_CAMPAIGN_ID_KEY);
    if (!raw) {
      return null;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  });
  const [outreachCampaigns, setOutreachCampaigns] = useState<OutreachCampaignDTO[]>([]);
  const [outreachCampaignsLoading, setOutreachCampaignsLoading] = useState(false);
  const [campaignWorkspaceBusy, setCampaignWorkspaceBusy] = useState(false);
  const [campaignMonitor, setCampaignMonitor] = useState<OutreachCampaignMonitorResponse | null>(null);
  const [campaignMonitorLoading, setCampaignMonitorLoading] = useState(false);
  const [campaignMonitorFilter, setCampaignMonitorFilter] = useState<CampaignMonitorFilter>("all");
  const [outreachMirrorEn, setOutreachMirrorEn] = useState<{ subject_en: string; body_en: string } | null>(null);
  const [outreachGenerateLoading, setOutreachGenerateLoading] = useState(false);
  const stationContextRequestId = useRef(0);
  const contactDraftRequestId = useRef(0);

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
    const visibleVerified = items.filter((item) => item.status === "verified").length;
    const visibleRejected = items.filter((item) => item.status === "rejected").length;
    const verifiedTotal = filters.status === "verified" ? total : visibleVerified;
    const rejectedTotal = filters.status === "rejected" ? total : visibleRejected;
    const avgConfidence = items.length > 0 ? items.reduce((sum, item) => sum + item.confidence_score, 0) / items.length : 0;
    const countries = new Set(items.map((item) => item.country_code).filter(Boolean)).size;
    return { total, visibleVerified, visibleRejected, verifiedTotal, rejectedTotal, avgConfidence, countries };
  }, [stations, filters.status]);

  const campaignStations = useMemo(() => {
    const source = stations?.items ?? [];
    return source.filter((item) => campaignStationIds.includes(item.id));
  }, [campaignStationIds, stations]);

  const campaignSummary = useMemo(() => {
    return {
      withSubmissions: campaignStations.filter((item) => item.submission_count > 0).length,
      withPeople: campaignStations.filter((item) => item.people_count > 0).length,
      verified: campaignStations.filter((item) => item.status === "verified").length,
    };
  }, [campaignStations]);
  const activeOutreachCampaigns = useMemo(
    () => outreachCampaigns.filter((campaign) => campaign.is_active),
    [outreachCampaigns],
  );
  const latestAssessmentEvidence = useMemo(() => {
    if (!stationControl?.assessments.length) {
      return {};
    }
    const selectedAssessment =
      stationControl.assessments.find((item) => item.assessment_kind === EDITORIAL_ASSESSMENT_KIND) ??
      stationControl.assessments.find((item) => item.assessment_kind.startsWith("editorial_")) ??
      stationControl.assessments.find((item) => item.assessment_kind === "llm_quality_v1") ??
      stationControl.assessments[0];
    return asRecord(selectedAssessment?.evidence);
  }, [stationControl]);
  const editorialSummary = String(latestAssessmentEvidence.editorial_summary_short || "").trim();
  const editorialFormat = String(latestAssessmentEvidence.editorial_format || "").trim();
  const styleTags = stringList(latestAssessmentEvidence.style_tags);
  const campaignFitTags = stringList(latestAssessmentEvidence.campaign_fit_tags);
  const pitchAngleHint = String(latestAssessmentEvidence.pitch_angle_hint || "").trim();

  const dataMonitor = asRecord(dataOverview?.monitor);
  const dataCatalog = asRecord(dataMonitor.catalog);
  const dataCoverage = asRecord(dataMonitor.coverage);
  const dataQuotas = asRecord(dataMonitor.quotas);
  const dataLlmQuota = asRecord(dataQuotas.llm);
  const dataScanFocus = asRecord(dataMonitor.scan_focus);
  const dataBoost = asRecord(dataMonitor.boost);
  const dataHighPriority = asRecord(dataMonitor.high_priority);
  const dataServices = asRecord(dataMonitor.services);
  const dataScanOperations = asRecord(dataMonitor.scan_operations);
  const rejectedScan = asRecord(dataScanOperations.rejected_scan);
  const rejectedWorker = asRecord(rejectedScan.worker);
  const dataCountryDiscovery = asRecord(dataOverview?.country_discovery);
  const filteredCampaignMonitorItems = useMemo(() => {
    const items = campaignMonitor?.items ?? [];
    if (campaignMonitorFilter === "all") {
      return items;
    }
    return items.filter((item) => item.monitor_status === campaignMonitorFilter);
  }, [campaignMonitor?.items, campaignMonitorFilter]);

  useEffect(() => {
    setApiPermissionMode(permissionMode);
    window.localStorage.setItem("radio-db-permission-mode", permissionMode);
  }, [permissionMode]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (selectedOutreachCampaignId != null) {
      window.localStorage.setItem(OUTREACH_CAMPAIGN_ID_KEY, String(selectedOutreachCampaignId));
    } else {
      window.localStorage.removeItem(OUTREACH_CAMPAIGN_ID_KEY);
    }
  }, [selectedOutreachCampaignId]);

  useEffect(() => {
    if (selectedOutreachCampaignId == null) {
      setCampaignMonitor(null);
      return;
    }
    void refreshCampaignMonitor(selectedOutreachCampaignId);
  }, [selectedOutreachCampaignId]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(
      STATION_FILTERS_STORAGE_KEY,
      JSON.stringify({
        filters,
        searchDraft,
      }),
    );
  }, [filters, searchDraft]);

  useEffect(() => {
    void refreshStations();
    void refreshRuns();
    void refreshContactTemplates();
    void refreshOutreachCampaigns();
    void refreshBrowserSessions();
    void refreshDataControl();
    void refreshStationGroups();
  }, []);

  useEffect(() => {
    void refreshStations();
  }, [filters]);

  useEffect(() => {
    if (!stations?.items.length) {
      setSelectedStationId(null);
      return;
    }
    if (selectedStationId === null || !stations.items.some((item) => item.id === selectedStationId)) {
      setSelectedStationId(stations.items[0].id);
    }
  }, [stations, selectedStationId]);

  useEffect(() => {
    if (selectedStationId === null) {
      setSelectedStation(null);
      setStationControl(null);
      setStationDraft(null);
      setContactDraft(null);
      setStationDrafts([]);
      setContactSends([]);
      setSelectedSendId(null);
      setContactOutcomes([]);
      setStationMemberships([]);
      setBrowserSessionId(null);
      setBrowserSessionDetail(null);
      return;
    }
    setSelectedStation(null);
    setStationControl(null);
    setStationDraft(null);
    setContactDraft(null);
    setStationDrafts([]);
    setContactSends([]);
    setSelectedSendId(null);
    setContactOutcomes([]);
    setStationMemberships([]);
    setBrowserSessionId(null);
    setBrowserSessionDetail(null);
    void refreshStationContext(selectedStationId);
    void refreshContactDraft(selectedStationId);
    void refreshStationRuns(selectedStationId);
    void refreshStationMemberships(selectedStationId);
  }, [selectedStationId]);

  useEffect(() => {
    if (!contactDraft?.draft_id) {
      setContactSends([]);
      setSelectedSendId(null);
      setContactOutcomes([]);
      return;
    }
    void refreshDraftSends(contactDraft.draft_id);
  }, [contactDraft?.draft_id]);

  useEffect(() => {
    if (selectedSendId === null) {
      setContactOutcomes([]);
      return;
    }
    void refreshSendOutcomes(selectedSendId);
  }, [selectedSendId]);

  useEffect(() => {
    if (selectedRunId === null) {
      setSelectedRun(null);
      return;
    }
    void refreshRunDetail(selectedRunId);
  }, [selectedRunId]);

  useEffect(() => {
    if (selectedGroupId === null) {
      setSelectedGroupDetail(null);
      return;
    }
    void refreshStationGroupDetail(selectedGroupId);
  }, [selectedGroupId]);

  useEffect(() => {
    if (browserSessionId === null) {
      setBrowserSessionDetail(null);
      return;
    }
    void refreshBrowserSessionDetail(browserSessionId);
  }, [browserSessionId]);

  useEffect(() => {
    if (!selectedRunId || !browserSessions.length) {
      return;
    }
    const matchingSession = browserSessions.find((session) => session.run_id === selectedRunId);
    if (matchingSession && browserSessionId !== matchingSession.id) {
      setBrowserSessionId(matchingSession.id);
      return;
    }
    const currentSession = browserSessions.find((session) => session.id === browserSessionId);
    if (currentSession && currentSession.run_id !== null && currentSession.run_id !== selectedRunId) {
      setBrowserSessionId(null);
    }
  }, [browserSessions, browserSessionId, selectedRunId]);

  useEffect(() => {
    if (!browserSessionId) {
      return;
    }
    const source = new EventSource(`/api/v1/browser/sessions/${browserSessionId}/stream`);
    const handleBrowserEvent = () => {
      void refreshBrowserSessions();
      void refreshBrowserSessionDetail(browserSessionId);
    };
    const handleBrowserClosed = () => {
      setBrowserSessionError(null);
      void refreshBrowserSessions();
      void refreshBrowserSessionDetail(browserSessionId);
      source.close();
    };

    source.addEventListener("browser-session", handleBrowserEvent);
    source.addEventListener("closed", handleBrowserClosed);
    source.onerror = () => {
      setBrowserSessionError("Browser stream disconnected.");
      source.close();
    };

    return () => {
      source.close();
    };
  }, [browserSessionId]);

  useEffect(() => {
    if (activeTab === "runs") {
      void refreshRuns(selectedStationId ?? undefined, runStatusFilter || undefined);
      void refreshBrowserSessions();
    }
    if (activeTab === "system") {
      void refreshDataControl();
      void refreshCountryDiscovery();
    }
  }, [activeTab, runStatusFilter]);

  async function refreshStations(): Promise<void> {
    setStationsLoading(true);
    setStationsError(null);
    try {
      const response = await listStations({
        q: filters.q,
        country: filters.country,
        status: filters.status,
        has_submission: filters.hasSubmission,
        has_people: filters.hasPeople,
        outreach: filters.outreach,
        min_confidence: filters.minConfidence,
        page: filters.page,
        page_size: filters.pageSize,
        sort_by: filters.sortBy,
        sort_order: filters.sortOrder,
      });
      setStations(response);
    } catch (error) {
      setStationsError(error instanceof Error ? error.message : "Stations konnten nicht geladen werden.");
    } finally {
      setStationsLoading(false);
    }
  }

  async function refreshStationContext(stationId: number): Promise<void> {
    const requestId = ++stationContextRequestId.current;
    setStationLoading(true);
    setStationError(null);
    try {
      const [detail, control] = await Promise.all([getStationDetail(stationId), getStationControl(stationId)]);
      if (requestId !== stationContextRequestId.current || stationId !== selectedStationId) {
        return;
      }
      setSelectedStation(detail);
      setStationControl(control);
      setStationDraft({
        canonical_name: detail.canonical_name,
        country_code: detail.country_code ?? "",
        city: detail.city ?? "",
        language: detail.language ?? "",
        website_url: detail.website_url ?? "",
        stream_url: detail.stream_url ?? "",
        status: detail.status as StationDraft["status"],
        confidence_score: String(detail.confidence_score ?? 0),
      });
      setBrowserUrlDraft(detail.website_url ?? "");
    } catch (error) {
      if (requestId !== stationContextRequestId.current) {
        return;
      }
      setStationError(error instanceof Error ? error.message : "Station context konnte nicht geladen werden.");
    } finally {
      if (requestId === stationContextRequestId.current) {
        setStationLoading(false);
      }
    }
  }

  async function refreshRuns(stationId?: number, status?: string): Promise<void> {
    setRunsLoading(true);
    setRunsError(null);
    try {
      const response = await listAgentRuns({ stationId, status, limit: 80, offset: 0 });
      setAgentRuns(response.items);
      setRunsTotal(response.total);
      if (!selectedRunId && response.items[0]) {
        setSelectedRunId(response.items[0].id);
      }
    } catch (error) {
      setRunsError(error instanceof Error ? error.message : "Runs konnten nicht geladen werden.");
    } finally {
      setRunsLoading(false);
    }
  }

  async function refreshStationRuns(stationId: number): Promise<void> {
    await refreshRuns(stationId, runStatusFilter || undefined);
  }

  async function refreshRunDetail(runId: number): Promise<void> {
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
      setCountryDiscoveryError(error instanceof Error ? error.message : "Country discovery konnte nicht geladen werden.");
    } finally {
      setCountryDiscoveryLoading(false);
    }
  }

  async function refreshDataControl(): Promise<void> {
    setDataOverviewLoading(true);
    setDataOverviewError(null);
    try {
      const response = await getDataControlOverview({ history_limit: 20, history_hours: 72 });
      setDataOverview(response);
      const nextFocus = asRecord(asRecord(response.monitor).scan_focus);
      if (Boolean(nextFocus.enabled)) {
        setScanFocusDraft(String(nextFocus.market_focus || "international") as ScanFocus);
      }
    } catch (error) {
      setDataOverviewError(error instanceof Error ? error.message : "Systemdaten konnten nicht geladen werden.");
    } finally {
      setDataOverviewLoading(false);
    }
  }

  async function refreshBrowserSessions(): Promise<void> {
    setBrowserSessionsLoading(true);
    setBrowserSessionError(null);
    try {
      const response = await listBrowserSessions();
      setBrowserSessions(response.items);
      if (!browserSessionId && response.items[0]) {
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

  async function refreshContactDraft(stationId: number): Promise<void> {
    const requestId = ++contactDraftRequestId.current;
    setContactDraftLoading(true);
    try {
      const [draft, drafts] = await Promise.all([getStationContactDraft(stationId), listStationContactDrafts(stationId)]);
      if (requestId !== contactDraftRequestId.current || stationId !== selectedStationId) {
        return;
      }
      setContactDraft(draft);
      setStationDrafts(drafts.items);
      setSelectedTemplateId(draft.template_id ?? "default");
      setOutreachMirrorEn(null);
    } catch (error) {
      if (requestId !== contactDraftRequestId.current) {
        return;
      }
      setStationError(error instanceof Error ? error.message : "Contact Draft konnte nicht geladen werden.");
    } finally {
      if (requestId === contactDraftRequestId.current) {
        setContactDraftLoading(false);
      }
    }
  }

  async function refreshContactTemplates(): Promise<void> {
    try {
      const response = await listContactTemplates();
      setContactTemplates(response.items);
    } catch {
      // template fetch failure should not block the core UI
    }
  }

  async function refreshOutreachCampaigns(): Promise<void> {
    setOutreachCampaignsLoading(true);
    try {
      const response = await listOutreachCampaigns(false);
      setOutreachCampaigns(response.items);
      const currentId = selectedOutreachCampaignId;
      if (currentId != null) {
        const match = response.items.find((item) => item.id === currentId);
        if (match) {
          setCampaignDraft(campaignDtoToForm(match));
        } else {
          setSelectedOutreachCampaignId(null);
          setCampaignDraft(emptyCampaignDraft());
        }
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Kampagnen konnten nicht geladen werden.");
    } finally {
      setOutreachCampaignsLoading(false);
    }
  }

  async function refreshCampaignMonitor(campaignId = selectedOutreachCampaignId): Promise<void> {
    if (campaignId == null) {
      setCampaignMonitor(null);
      return;
    }
    setCampaignMonitorLoading(true);
    try {
      const response = await getOutreachCampaignMonitor(campaignId);
      setCampaignMonitor(response);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Campaign Monitor konnte nicht geladen werden.");
    } finally {
      setCampaignMonitorLoading(false);
    }
  }

  async function refreshStationGroups(): Promise<void> {
    setStationGroupsLoading(true);
    try {
      const response = await listStationGroups();
      setStationGroups(response.items);
      if (!selectedGroupId && response.items[0]) {
        setSelectedGroupId(response.items[0].id);
        setGroupTargetId(response.items[0].id);
      }
      if (selectedGroupId && !response.items.some((item) => item.id === selectedGroupId)) {
        setSelectedGroupId(response.items[0]?.id ?? null);
      }
      if (groupTargetId !== "" && !response.items.some((item) => item.id === groupTargetId)) {
        setGroupTargetId(response.items[0]?.id ?? "");
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Station groups konnten nicht geladen werden.");
    } finally {
      setStationGroupsLoading(false);
    }
  }

  async function refreshStationGroupDetail(groupId: number): Promise<void> {
    try {
      const response = await getStationGroup(groupId);
      setSelectedGroupDetail(response);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Group detail konnte nicht geladen werden.");
    }
  }

  async function refreshStationMemberships(stationId: number): Promise<void> {
    try {
      const response = await listStationMemberships(stationId);
      setStationMemberships(response.items);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Station-Gruppen konnten nicht geladen werden.");
    }
  }

  async function refreshDraftSends(draftId: number): Promise<void> {
    try {
      const response = await listDraftSends(draftId);
      setContactSends(response.items);
      if (!selectedSendId && response.items[0]) {
        setSelectedSendId(response.items[0].id);
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Draft sends konnten nicht geladen werden.");
    }
  }

  async function refreshSendOutcomes(sendId: number): Promise<void> {
    try {
      const response = await listSendOutcomes(sendId);
      setContactOutcomes(response.items);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Outcomes konnten nicht geladen werden.");
    }
  }

  function setPrimaryStation(stationId: number): void {
    setSelectedStationId(stationId);
    setStationPanelTab("overview");
    setActiveTab("stations");
  }

  function toggleStationSelection(stationId: number): void {
    setSelectedStationIds((current) =>
      current.includes(stationId) ? current.filter((item) => item !== stationId) : [...current, stationId]
    );
  }

  function clearSelection(): void {
    setSelectedStationIds([]);
  }

  function addSelectionToCampaign(): void {
    setCampaignStationIds((current) => Array.from(new Set([...current, ...selectedStationIds])));
    setSystemMessage(`${selectedStationIds.length} Sender zur Campaign-Vorbereitung hinzugefügt.`);
    setActiveTab("campaigns");
  }

  async function handleCreateGroup(): Promise<void> {
    if (!groupDraft.name.trim()) {
      setSystemMessage("Gruppenname fehlt.");
      return;
    }
    setGroupBusy(true);
    try {
      const response = await createStationGroup({
        name: groupDraft.name.trim(),
        description: groupDraft.description.trim() || null,
        artist_key: groupDraft.artistKey.trim() || null,
      });
      setGroupDraft({ name: "", description: "", artistKey: "" });
      setSelectedGroupId(response.id);
      setGroupTargetId(response.id);
      setSystemMessage(`Gruppe ${response.name} erstellt.`);
      await refreshStationGroups();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Gruppe konnte nicht erstellt werden.");
    } finally {
      setGroupBusy(false);
    }
  }

  async function handleAddSelectionToGroup(): Promise<void> {
    if (!selectedStationIds.length || groupTargetId === "") {
      return;
    }
    setGroupBusy(true);
    try {
      await addStationsToGroup(groupTargetId, { station_ids: selectedStationIds });
      setSystemMessage(`${selectedStationIds.length} Sender zu Gruppe hinzugefügt.`);
      await refreshStationGroups();
      await refreshStationGroupDetail(groupTargetId);
      if (selectedStationId) {
        await refreshStationMemberships(selectedStationId);
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Gruppenzuordnung fehlgeschlagen.");
    } finally {
      setGroupBusy(false);
    }
  }

  async function handleAddCurrentStationToGroup(groupId: number): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setGroupBusy(true);
    try {
      await addStationToGroup(selectedStationId, groupId);
      setSystemMessage("Sender zur Gruppe hinzugefügt.");
      await refreshStationMemberships(selectedStationId);
      await refreshStationGroups();
      if (selectedGroupId === groupId) {
        await refreshStationGroupDetail(groupId);
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Sender konnte nicht zur Gruppe hinzugefügt werden.");
    } finally {
      setGroupBusy(false);
    }
  }

  async function handleRemoveCurrentStationFromGroup(groupId: number): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setGroupBusy(true);
    try {
      await removeStationFromGroup(selectedStationId, groupId);
      setSystemMessage("Sender aus Gruppe entfernt.");
      await refreshStationMemberships(selectedStationId);
      await refreshStationGroups();
      if (selectedGroupId === groupId) {
        await refreshStationGroupDetail(groupId);
      }
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Sender konnte nicht aus Gruppe entfernt werden.");
    } finally {
      setGroupBusy(false);
    }
  }

  async function handleStationSave(): Promise<void> {
    if (!selectedStationId || !stationDraft) {
      return;
    }
    setStationSaving(true);
    try {
      const response = await updateStation(selectedStationId, {
        canonical_name: stationDraft.canonical_name,
        country_code: stationDraft.country_code,
        city: stationDraft.city || null,
        language: stationDraft.language,
        website_url: stationDraft.website_url || null,
        stream_url: stationDraft.stream_url || null,
        status: stationDraft.status,
        confidence_score: Number(stationDraft.confidence_score || 0),
      });
      setSelectedStation(response);
      setSystemMessage(`Station ${response.canonical_name} gespeichert.`);
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Speichern fehlgeschlagen.");
    } finally {
      setStationSaving(false);
    }
  }

  async function handleManualConfirm(): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setManualConfirmBusy(true);
    try {
      await setStationManualConfirm(selectedStationId, true);
      setSystemMessage("Station manuell bestätigt.");
      await refreshStationContext(selectedStationId);
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
    setDeleteBusy(true);
    try {
      await deleteStation(selectedStationId);
      setSystemMessage("Station gelöscht.");
      setSelectedStationId(null);
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Delete fehlgeschlagen.");
    } finally {
      setDeleteBusy(false);
    }
  }

  async function handleDeleteSelectedStations(): Promise<void> {
    if (!selectedStationIds.length) {
      return;
    }
    const count = selectedStationIds.length;
    const confirmed = window.confirm(`${count} ausgewählte Sender wirklich löschen?`);
    if (!confirmed) {
      return;
    }

    setBulkDeleteBusy(true);
    try {
      const idsToDelete = [...selectedStationIds];
      for (const stationId of idsToDelete) {
        await deleteStation(stationId);
      }

      if (selectedStationId !== null && idsToDelete.includes(selectedStationId)) {
        setSelectedStationId(null);
      }
      setSelectedStationIds([]);
      setCampaignStationIds((current) => current.filter((stationId) => !idsToDelete.includes(stationId)));
      setSystemMessage(`${count} Sender gelöscht.`);
      await refreshStations();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Bulk-Delete fehlgeschlagen.");
    } finally {
      setBulkDeleteBusy(false);
    }
  }

  async function handleStartManualScan(): Promise<void> {
    if (!selectedStationId) {
      return;
    }
    setManualScanBusy(true);
    try {
      const response = await startManualScan({
        station_id: selectedStationId,
        mode: manualScanMode,
        target_url: selectedStation?.website_url ?? currentStation?.website_url ?? null,
        max_pages: manualScanPages,
      });
      setSelectedRunId(response.id);
      setActiveTab("runs");
      setSystemMessage(`Manual scan #${response.id} gestartet.`);
      await refreshRuns(selectedStationId, runStatusFilter || undefined);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Scan konnte nicht gestartet werden.");
    } finally {
      setManualScanBusy(false);
    }
  }

  async function handleOpenBrowserSession(): Promise<void> {
    setBrowserSessionError(null);
    try {
      const session = await createBrowserSession({
        start_url: browserUrlDraft || selectedStation?.website_url || currentStation?.website_url || null,
        station_id: selectedStationId,
        run_id: selectedRunId,
        label: selectedStation?.canonical_name || currentStation?.canonical_name || "manual browser",
      });
      setBrowserSessionId(session.id);
      setActiveTab("runs");
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
      setSystemMessage("Browser navigiert.");
      await refreshBrowserSessions();
      await refreshBrowserSessionDetail(browserSessionId);
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
        selector: browserSelectorDraft || null,
        value: browserValueDraft || null,
      });
      setSystemMessage(`Browser-Aktion ${browserAction} ausgeführt.`);
      await refreshBrowserSessions();
      await refreshBrowserSessionDetail(browserSessionId);
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
      setSystemMessage("Browser snapshot aktualisiert.");
      await refreshBrowserSessionDetail(browserSessionId);
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
      setBrowserSessionDetail(null);
      setSystemMessage("Browser session geschlossen.");
      await refreshBrowserSessions();
    } catch (error) {
      setBrowserSessionError(error instanceof Error ? error.message : "Browser session konnte nicht geschlossen werden.");
    }
  }

  function outreachCampaignPayload(): { campaign_id?: number } {
    return selectedOutreachCampaignId != null ? { campaign_id: selectedOutreachCampaignId } : {};
  }

  function campaignSubmissionPayload() {
    return {
      submission_defaults: {
        contact_name: campaignDraft.contactName,
        contact_email: campaignDraft.contactEmail,
        contact_phone: campaignDraft.contactPhone,
        label_name: campaignDraft.labelName,
      },
      artist_profile: {
        artist_city: campaignDraft.artistCity,
        artist_country: campaignDraft.artistCountry,
        artist_website: campaignDraft.artistWebsite,
        genre: campaignDraft.genre,
        spotify_url: campaignDraft.spotifyUrl,
        soundcloud_url: campaignDraft.soundcloudUrl,
        youtube_url: campaignDraft.youtubeUrl,
        bandcamp_url: campaignDraft.bandcampUrl,
        artist_bio_short: campaignDraft.artistBioShort,
        artist_bio_long: campaignDraft.artistBioLong,
      },
      release_assets: {
        audio_file_url: campaignDraft.audioFileUrl,
        download_url: campaignDraft.downloadUrl,
        press_photo_url: campaignDraft.pressPhotoUrl,
        cover_art_url: campaignDraft.coverArtUrl,
        press_kit_url: campaignDraft.pressKitUrl,
        lyrics_url: campaignDraft.lyricsUrl,
        label_code: campaignDraft.labelCode,
      },
    };
  }

  async function handleSaveOutreachCampaign(): Promise<void> {
    if (!campaignDraft.name.trim() || !campaignDraft.artist.trim() || !campaignDraft.songTitle.trim()) {
      setSystemMessage("Campaign name, artist und song title sind Pflichtfelder.");
      return;
    }
    setCampaignWorkspaceBusy(true);
    try {
      if (selectedOutreachCampaignId != null) {
        await patchOutreachCampaign(selectedOutreachCampaignId, {
          name: campaignDraft.name.trim(),
          artist_name: campaignDraft.artist.trim(),
          song_title: campaignDraft.songTitle.trim(),
          release_date: campaignDraft.releaseDate || null,
          song_language: campaignDraft.primaryLanguage || null,
          press_release_url: campaignDraft.pressReleaseUrl || null,
          tracking_code: campaignDraft.trackingCode || null,
          pitch_text: campaignDraft.pitch,
          reference_template: campaignDraft.referenceTemplate,
          operator_notes: campaignDraft.notes || null,
          ...campaignSubmissionPayload(),
        });
        setSystemMessage("Kampagne gespeichert.");
      } else {
        const created = await createOutreachCampaign({
          name: campaignDraft.name.trim(),
          artist_name: campaignDraft.artist.trim(),
          song_title: campaignDraft.songTitle.trim(),
          release_date: campaignDraft.releaseDate || null,
          song_language: campaignDraft.primaryLanguage || null,
          press_release_url: campaignDraft.pressReleaseUrl || null,
          tracking_code: campaignDraft.trackingCode || null,
          pitch_text: campaignDraft.pitch,
          reference_template: campaignDraft.referenceTemplate,
          operator_notes: campaignDraft.notes || null,
          ...campaignSubmissionPayload(),
        });
        setSelectedOutreachCampaignId(created.id);
        setSystemMessage(`Kampagne #${created.id} angelegt.`);
      }
      await refreshOutreachCampaigns();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Kampagne konnte nicht gespeichert werden.");
    } finally {
      setCampaignWorkspaceBusy(false);
    }
  }

  async function handleArchiveOutreachCampaign(): Promise<void> {
    if (selectedOutreachCampaignId == null) {
      return;
    }
    setCampaignWorkspaceBusy(true);
    try {
      await archiveOutreachCampaign(selectedOutreachCampaignId);
      setSelectedOutreachCampaignId(null);
      setCampaignDraft(emptyCampaignDraft());
      setSystemMessage("Kampagne archiviert (deaktiviert).");
      await refreshOutreachCampaigns();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Archivieren fehlgeschlagen.");
    } finally {
      setCampaignWorkspaceBusy(false);
    }
  }

  async function handleGenerateOutreachEmail(): Promise<void> {
    if (!selectedStationId || selectedOutreachCampaignId == null) {
      setSystemMessage("Wähle eine aktive Kampagne und einen Sender für die LLM-Generierung.");
      return;
    }
    setOutreachGenerateLoading(true);
    try {
      const result = await generateOutreachEmail(selectedOutreachCampaignId, { station_id: selectedStationId });
      setContactDraft((current) => (current ? { ...current, subject: result.subject, body: result.body } : current));
      setOutreachMirrorEn({ subject_en: result.subject_en, body_en: result.body_en });
      setSystemMessage("Betreff und Text generiert (Sprache der Station + englischer Spiegel).");
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Generierung fehlgeschlagen.");
    } finally {
      setOutreachGenerateLoading(false);
    }
  }

  async function handleCreatePersistedDraft(): Promise<void> {
    if (!selectedStationId || !contactDraft) {
      return;
    }
    setContactSendLoading(true);
    try {
      const response = await createStationContactDraft(selectedStationId, {
        template_id: selectedTemplateId === "default" ? null : selectedTemplateId,
        subject: contactDraft.subject,
        body: contactDraft.body,
        ...outreachCampaignPayload(),
      });
      setContactDraft(response);
      setSystemMessage(`Draft ${response.draft_id ?? ""} gespeichert.`);
      await refreshContactDraft(selectedStationId);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Draft konnte nicht gespeichert werden.");
    } finally {
      setContactSendLoading(false);
    }
  }

  async function handleRefreshContactDraftPreview(): Promise<void> {
    if (!selectedStationId || !contactDraft) {
      return;
    }
    setContactSendLoading(true);
    try {
      let activeDraft = contactDraft;
      if (!activeDraft.draft_id) {
        activeDraft = await createStationContactDraft(selectedStationId, {
          template_id: selectedTemplateId === "default" ? null : selectedTemplateId,
          subject: activeDraft.subject,
          body: activeDraft.body,
          ...outreachCampaignPayload(),
        });
        setContactDraft(activeDraft);
      }
      const response = await previewStationContactDraft(activeDraft.draft_id as number, {
        subject: activeDraft.subject,
        body: activeDraft.body,
      });
      setContactDraft(response);
      setSystemMessage("Draft preview aktualisiert.");
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Preview fehlgeschlagen.");
    } finally {
      setContactSendLoading(false);
    }
  }

  async function handleDryRunSend(): Promise<void> {
    if (!selectedStationId || !contactDraft) {
      return;
    }
    setContactSendLoading(true);
    try {
      let activeDraft = contactDraft;
      if (!activeDraft.draft_id) {
        activeDraft = await createStationContactDraft(selectedStationId, {
          template_id: selectedTemplateId === "default" ? null : selectedTemplateId,
          subject: activeDraft.subject,
          body: activeDraft.body,
          ...outreachCampaignPayload(),
        });
        setContactDraft(activeDraft);
      }
      const response = await createContactSend(activeDraft.draft_id as number, {
        mode: "dry-run",
        channel: activeDraft.recommended_channel,
      });
      setSystemMessage(`Dry-run send #${response.id} erstellt: ${response.status}.`);
      await refreshContactDraft(selectedStationId);
      await refreshDraftSends(activeDraft.draft_id as number);
      setSelectedSendId(response.id);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Dry-run send fehlgeschlagen.");
    } finally {
      setContactSendLoading(false);
    }
  }

  async function handleExecuteSend(): Promise<void> {
    if (!selectedStationId || !contactDraft) {
      return;
    }
    setContactSendLoading(true);
    try {
      let activeDraft = contactDraft;
      if (!activeDraft.draft_id) {
        activeDraft = await createStationContactDraft(selectedStationId, {
          template_id: selectedTemplateId === "default" ? null : selectedTemplateId,
          subject: activeDraft.subject,
          body: activeDraft.body,
          ...outreachCampaignPayload(),
        });
        setContactDraft(activeDraft);
      }
      const response = await createContactSend(activeDraft.draft_id as number, {
        mode: "execute",
        channel: activeDraft.recommended_channel,
      });
      setSystemMessage(`Execute send #${response.id} erstellt: ${response.status}.`);
      await refreshContactDraft(selectedStationId);
      await refreshDraftSends(activeDraft.draft_id as number);
      setSelectedSendId(response.id);
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Execute send fehlgeschlagen.");
    } finally {
      setContactSendLoading(false);
    }
  }

  async function handleCopyDraft(): Promise<void> {
    if (!contactDraft) {
      return;
    }
    try {
      await navigator.clipboard.writeText(`${contactDraft.subject}\n\n${contactDraft.body}`);
      setSystemMessage("Draft in die Zwischenablage kopiert.");
    } catch {
      setSystemMessage("Zwischenablage nicht verfügbar.");
    }
  }

  async function handleUpdateScanFocus(): Promise<void> {
    try {
      const enabled = scanFocusDraft !== "off";
      const marketFocus = scanFocusDraft === "off" ? "international" : scanFocusDraft;
      await updateScanFocus({
        enabled,
        market_focus: marketFocus,
      });
      setSystemMessage("Scan focus aktualisiert.");
      await refreshDataControl();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Scan focus konnte nicht gesetzt werden.");
    }
  }

  async function handleStartCountryDiscovery(): Promise<void> {
    try {
      const response = await startCountryDiscovery();
      setSystemMessage(response.message);
      await refreshCountryDiscovery();
    } catch (error) {
      setSystemMessage(error instanceof Error ? error.message : "Country discovery konnte nicht gestartet werden.");
    }
  }

  function openArtifact(path: string | null | undefined): void {
    if (!path) {
      return;
    }
    window.open(getArtifactUrl(path), "_blank", "noopener,noreferrer");
  }

  function removeCampaignStation(stationId: number): void {
    setCampaignStationIds((current) => current.filter((item) => item !== stationId));
  }

  function groupLabel(group: StationGroupListItem): string {
    return group.artist_key ? `${group.name} · ${group.artist_key}` : group.name;
  }

  const selectedRunArtifactCount = selectedRun ? selectedRun.steps.filter((step) => Boolean(step.screenshot_path || step.dom_snapshot_path)).length : 0;
  const browserEvents = (browserSessionDetail?.timeline ?? []).slice().reverse();
  const latestRunStep = selectedRun?.steps.at(-1) ?? null;
  const selectedRunSummary = asRecord(selectedRun?.summary);
  const latestRunObservation = asRecord(latestRunStep?.agent_observation);
  const latestRunAction = asRecord(latestRunStep?.proposed_action);
  const latestRunResult = asRecord(latestRunStep?.execution_result);
  const hasLatestRunStep = Boolean(latestRunStep);
  const runBlocker = selectedRun?.blocked_reason || firstText(selectedRunSummary.reason);
  const agentObservationTitle = hasLatestRunStep
    ? firstText(latestRunObservation.summary, latestRunObservation.message, latestRunObservation.page_type, latestRunResult.details, "Agent step recorded without summary.")
    : runBlocker
      ? `Run blocked before browser/LLM step: ${runBlocker}`
      : "No browser/LLM step recorded for this run yet.";
  const agentObservationAction = hasLatestRunStep
    ? firstText(latestRunAction.type, latestRunAction.action, latestRunResult.reason, "Waiting for next scan action")
    : runBlocker === "excluded_meta_station"
      ? "Scanner skipped this station because it looks like a directory, list, article, or other meta entry."
      : "Start or select a scan run with steps to see LLM context.";
  const agentObservationMeta = hasLatestRunStep
    ? `${formatDate(latestRunStep?.created_at)} · confidence ${formatPercent(latestRunStep?.confidence ?? 0)}`
    : selectedRun?.finished_at
      ? `Finished ${formatDate(selectedRun.finished_at)}`
      : "No step artifact available.";
  const browserScreenshotUrl = browserSessionDetail?.screenshot_path
    ? `${getArtifactUrl(browserSessionDetail.screenshot_path)}&t=${encodeURIComponent(browserSessionDetail.updated_at)}`
    : "";
  const selectedRunLatestScreenshotUrl = latestRunStep?.screenshot_path
    ? `${getArtifactUrl(latestRunStep.screenshot_path)}&t=${encodeURIComponent(latestRunStep.created_at)}`
    : "";
  const monitorCandidates = useMemo<MonitorCandidate[]>(() => {
    const formCandidates = (selectedStation?.forms ?? []).map((form) => ({
      id: `form-${form.id}`,
      label: form.page_title || form.url,
      route: form.url,
      type: form.form_type,
      status: form.status,
      confidence: form.confidence,
      note: [
        form.requires_login ? "Login" : "public",
        form.has_captcha ? "Captcha" : "no captcha",
        form.language || "lang unknown",
      ].join(" · "),
    }));
    const submissionCandidates = (selectedStation?.submissions ?? []).map((submission) => ({
      id: `submission-${submission.id}`,
      label: submission.method,
      route: submission.email || submission.url || "no route",
      type: submission.method,
      status: submission.accepts_newcomers ? "newcomers ok" : "recorded",
      confidence: null,
      note: submission.requirements || "No requirements recorded.",
    }));
    return [...formCandidates, ...submissionCandidates];
  }, [selectedStation]);

  return (
    <div className="radio-db-app">
      <header className="radio-db-hero">
        <div className="radio-db-hero__copy">
          <p className="radio-db-kicker">Radio DB Control Center</p>
          <h1>Stations first. Manual control now. Automation next.</h1>
          <p className="radio-db-hero__lede">
            Die Oberfläche ist jetzt um die Senderliste als Master-Ansicht aufgebaut. Kontakt, Form-Scans und Browser-Supervision hängen direkt an Stationen und Runs statt an isolierten Seiten.
          </p>
        </div>
        <div className="radio-db-hero__stats">
          <MetricCard
            label={filters.status === "all" ? "Sender raw" : "Aktive Sender"}
            value={summary.total.toString()}
            sublabel={`${summary.countries} Länder auf dieser Seite · Seite ${filters.page} (${filters.pageSize} Zeilen)`}
          />
          <MetricCard
            label={filters.status === "verified" ? "Verified total" : "Verified im Fenster"}
            value={summary.verifiedTotal.toString()}
            sublabel={
              filters.status === "verified"
                ? `${summary.total} Sender im Filter`
                : `${summary.rejectedTotal} rejected im Fenster`
            }
          />
          <MetricCard label="Selection" value={selectedStationIds.length.toString()} sublabel={selectedStationIds.length ? "für Batch/Campaign markiert" : "keine Batch-Auswahl"} />
          <MetricCard label="Ø Confidence" value={formatPercent(summary.avgConfidence)} sublabel={currentStation ? currentStation.canonical_name : "kein Sender selektiert"} />
          <label className="radio-db-mode-switch">
            <span>Mode</span>
            <select value={permissionMode} onChange={(event) => setPermissionMode(event.target.value as ApiPermissionMode)}>
              <option value="read-only">read-only</option>
              <option value="dry-run">dry-run</option>
              <option value="execute">execute</option>
            </select>
          </label>
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
        {dataOverviewError ? <div className="radio-db-banner radio-db-banner--error">{dataOverviewError}</div> : null}
        {countryDiscoveryError ? <div className="radio-db-banner radio-db-banner--error">{countryDiscoveryError}</div> : null}
        {browserSessionError ? <div className="radio-db-banner radio-db-banner--error">{browserSessionError}</div> : null}

        {activeTab === "stations" && (
          <section className="radio-db-workbench">
            <article className="radio-db-panel radio-db-panel--wide">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Stations Master List</h2>
                  <p>Lange Senderlisten effizient filtern, markieren und in einen klaren nächsten Schritt überführen.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <label className="radio-db-inline-control">
                    <span>Group</span>
                    <select value={groupTargetId} onChange={(event) => setGroupTargetId(event.target.value ? Number(event.target.value) : "")}>
                      <option value="">choose group</option>
                      {stationGroups.map((group) => (
                        <option key={group.id} value={group.id}>{groupLabel(group)}</option>
                      ))}
                    </select>
                  </label>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshStations()} disabled={stationsLoading}>
                    Refresh
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={clearSelection} disabled={!selectedStationIds.length}>
                    Auswahl leeren
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleAddSelectionToGroup()} disabled={!selectedStationIds.length || groupTargetId === "" || groupBusy}>
                    Zur Gruppe hinzufügen
                  </button>
                  <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleDeleteSelectedStations()} disabled={!selectedStationIds.length || bulkDeleteBusy}>
                    Delete selected
                  </button>
                  <button type="button" className="radio-db-button" onClick={addSelectionToCampaign} disabled={!selectedStationIds.length}>
                    In Campaign übernehmen
                  </button>
                </div>
              </div>

              <form
                className="radio-db-filters radio-db-filters--dense"
                onSubmit={(event) => {
                  event.preventDefault();
                  setFilters((current) => ({ ...current, q: searchDraft.trim(), page: 1 }));
                }}
              >
                <label>
                  Search
                  <input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="Station, Stadt, Website" />
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
                    <option value="">active</option>
                    <option value="candidate">candidate</option>
                    <option value="verified">verified</option>
                    <option value="rejected">rejected</option>
                    <option value="archived">archived</option>
                    <option value="all">all raw</option>
                  </select>
                </label>
                <label>
                  Submissions
                  <select value={filters.hasSubmission} onChange={(event) => setFilters((current) => ({ ...current, hasSubmission: event.target.value as typeof current.hasSubmission, page: 1 }))}>
                    <option value="any">any</option>
                    <option value="yes">has submission</option>
                    <option value="no">no submission</option>
                  </select>
                </label>
                <label>
                  Category
                  <select value={filters.outreach} onChange={(event) => setFilters((current) => ({ ...current, outreach: event.target.value as typeof current.outreach, page: 1 }))}>
                    <option value="">all stations</option>
                    <option value="verified_submission">verified submission</option>
                    <option value="contact_only">contact only</option>
                  </select>
                </label>
                <label>
                  People
                  <select value={filters.hasPeople} onChange={(event) => setFilters((current) => ({ ...current, hasPeople: event.target.value as typeof current.hasPeople, page: 1 }))}>
                    <option value="any">any</option>
                    <option value="yes">has people</option>
                    <option value="no">no people</option>
                  </select>
                </label>
                <label>
                  Min confidence
                  <select value={filters.minConfidence} onChange={(event) => setFilters((current) => ({ ...current, minConfidence: Number(event.target.value), page: 1 }))}>
                    <option value={0}>0%</option>
                    <option value={0.4}>40%</option>
                    <option value={0.6}>60%</option>
                    <option value={0.75}>75%</option>
                    <option value={0.9}>90%</option>
                  </select>
                </label>
                <label>
                  Sort by
                  <select value={filters.sortBy} onChange={(event) => setFilters((current) => ({ ...current, sortBy: event.target.value as typeof current.sortBy, page: 1 }))}>
                    <option value="updated_at">updated</option>
                    <option value="confidence">confidence</option>
                    <option value="name">name</option>
                  </select>
                </label>
                <label>
                  Order
                  <select value={filters.sortOrder} onChange={(event) => setFilters((current) => ({ ...current, sortOrder: event.target.value as typeof current.sortOrder, page: 1 }))}>
                    <option value="desc">desc</option>
                    <option value="asc">asc</option>
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

              {selectedStationIds.length > 0 ? (
                <div className="radio-db-selection-bar">
                  <strong>{pluralize(selectedStationIds.length, "Sender", "Sender")} markiert.</strong>
                  <span>Nutze die Auswahl für Kampagnenvorbereitung oder um nacheinander manuell zu arbeiten.</span>
                </div>
              ) : null}

              <div className="radio-db-table-shell">
                {stationsLoading ? <div className="radio-db-empty">Loading stations…</div> : null}
                {!stationsLoading && stations?.items.length === 0 ? <div className="radio-db-empty">No stations match the current filter.</div> : null}
                <table className="radio-db-table">
                  <thead>
                    <tr>
                      <th>Select</th>
                      <th>Name</th>
                      <th>Country</th>
                      <th>Status</th>
                      <th>Confidence</th>
                      <th>Outreach</th>
                      <th>Signals</th>
                      <th>Next step</th>
                      <th>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stations?.items.map((item) => {
                      const isSelected = selectedStationId === item.id;
                      const isMarked = selectedStationIds.includes(item.id);
                      return (
                        <tr
                          key={item.id}
                          className={isSelected ? "radio-db-row radio-db-row--active" : "radio-db-row"}
                          onClick={() => setPrimaryStation(item.id)}
                        >
                          <td onClick={(event) => event.stopPropagation()}>
                            <input type="checkbox" checked={isMarked} onChange={() => toggleStationSelection(item.id)} aria-label={`Mark ${item.canonical_name}`} />
                          </td>
                          <td>
                            <strong>{item.canonical_name}</strong>
                            <div className="radio-db-subtle">
                              {item.website_url ? (
                                <StationWebsiteLink url={item.website_url} stopRowClick />
                              ) : (
                                "no website"
                              )}
                            </div>
                            <div className="radio-db-inline-actions">
                              <button type="button" className="radio-db-link-button" onClick={() => { setPrimaryStation(item.id); setStationPanelTab("outreach"); }}>
                                Contact
                              </button>
                              <button type="button" className="radio-db-link-button" onClick={() => { setPrimaryStation(item.id); setStationPanelTab("forms"); }}>
                                Forms
                              </button>
                              <button type="button" className="radio-db-link-button" onClick={() => { setPrimaryStation(item.id); setStationPanelTab("activity"); setActiveTab("runs"); }}>
                                Runs
                              </button>
                            </div>
                          </td>
                          <td>{item.country_code || "--"}<div className="radio-db-subtle">{item.city || "no city"}</div></td>
                          <td><span className={`radio-db-pill radio-db-pill--${statusTone(item.status)}`}>{item.status}</span></td>
                          <td>{formatPercent(item.confidence_score)}</td>
                          <td>
                            {item.outreach_bucket ? (
                              <>
                                <span className={`radio-db-pill radio-db-pill--${outreachTone(item.outreach_bucket)}`}>
                                  {outreachLabel(item.outreach_bucket)}
                                </span>
                                <div className="radio-db-subtle">
                                  {item.submission_path_quality || "no path quality"}
                                  {item.quality_score !== null ? ` · ${Math.round(item.quality_score)} pts` : ""}
                                </div>
                              </>
                            ) : (
                              <span className="radio-db-subtle">unassessed</span>
                            )}
                          </td>
                          <td>
                            <div className="radio-db-mini-metrics">
                              <span>{item.submission_count} submissions</span>
                              <span>{item.people_count} people</span>
                              <span>{item.genre_count} genres</span>
                            </div>
                          </td>
                          <td>{item.submission_count > 0 ? "Outreach oder Submit" : item.website_url ? "Scan website" : "Review record"}</td>
                          <td>{formatDate(item.updated_at)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </article>

            <aside className="radio-db-panel radio-db-panel--sticky">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Station Workbench</h2>
                  <p>Alles Relevante an einem Sender in einem persistenten Arbeitsbereich.</p>
                </div>
              </div>

              {currentStation ? (
                <div className="radio-db-stack">
                  <div className="radio-db-context-card">
                    <div className="radio-db-context-card__head">
                      <div>
                        <h3>{currentStation.canonical_name}</h3>
                        <p>{currentStation.country_code || "--"} · {currentStation.city || "no city"} · {currentStation.language || "no language"}</p>
                      </div>
                      <span className={`radio-db-pill radio-db-pill--${statusTone(currentStation.status)}`}>{currentStation.status}</span>
                    </div>
                    <div className="radio-db-subtle">Nächster sinnvoller Schritt: {nextBestAction(currentStation, selectedStation, contactDraft)}</div>
                    {editorialSummary || editorialFormat || pitchAngleHint || styleTags.length || campaignFitTags.length ? (
                      <div className="radio-db-stack">
                        {editorialSummary ? <div>{editorialSummary}</div> : null}
                        <div className="radio-db-summary-list">
                          {editorialFormat ? <SummaryRow label="Format" value={editorialFormat} /> : null}
                          {pitchAngleHint ? <SummaryRow label="Pitch angle" value={pitchAngleHint} /> : null}
                        </div>
                        {styleTags.length ? (
                          <div>
                            <div className="radio-db-section-title">Style tags</div>
                            <div className="radio-db-tag-row">
                              {styleTags.map((tag) => <span key={tag} className="radio-db-tag">{tag}</span>)}
                            </div>
                          </div>
                        ) : null}
                        {campaignFitTags.length ? (
                          <div>
                            <div className="radio-db-section-title">Campaign fit</div>
                            <div className="radio-db-tag-row">
                              {campaignFitTags.map((tag) => <span key={tag} className="radio-db-tag">{tag}</span>)}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>

                  <div className="radio-db-inline-tabs">
                    {STATION_PANEL_TABS.map((tab) => (
                      <button
                        key={tab.id}
                        type="button"
                        className={`radio-db-inline-tab ${stationPanelTab === tab.id ? "radio-db-inline-tab--active" : ""}`}
                        onClick={() => setStationPanelTab(tab.id)}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>

                  {stationLoading ? <div className="radio-db-empty">Loading station details…</div> : null}

                  {stationPanelTab === "overview" && (
                    <div className="radio-db-stack">
                      <div className="radio-db-summary-list">
                        <SummaryRow
                          label="Website"
                          value={
                            selectedStation?.website_url || currentStation.website_url ? (
                              <StationWebsiteLink url={(selectedStation?.website_url || currentStation.website_url)!} />
                            ) : (
                              "-"
                            )
                          }
                        />
                        <SummaryRow label="Language" value={selectedStation?.language || currentStation.language || "-"} />
                        <SummaryRow label="Forms" value={String(selectedStation?.forms.length ?? 0)} />
                        <SummaryRow label="Contacts" value={String(selectedStation?.contacts.length ?? 0)} />
                        <SummaryRow label="People" value={String(selectedStation?.people.length ?? 0)} />
                        <SummaryRow label="Last update" value={formatDate(selectedStation?.updated_at || currentStation.updated_at)} />
                      </div>
                      <div className="radio-db-action-grid">
                        <button type="button" className="radio-db-button" onClick={() => setStationPanelTab("outreach")}>Open outreach</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setStationPanelTab("forms")}>Review forms</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleStartManualScan()} disabled={manualScanBusy}>Start scan</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleOpenBrowserSession()}>Open browser</button>
                      </div>
                      <div className="radio-db-box">
                        <h3>Editorial details</h3>
                        {editorialSummary || editorialFormat || styleTags.length || campaignFitTags.length || pitchAngleHint ? (
                          <div className="radio-db-stack">
                            <div className="radio-db-summary-list">
                              {editorialFormat ? <SummaryRow label="Format" value={editorialFormat} /> : null}
                              {pitchAngleHint ? <SummaryRow label="Pitch angle" value={pitchAngleHint} /> : null}
                            </div>
                            {styleTags.length ? (
                              <div>
                                <div className="radio-db-section-title">Style tags</div>
                                <div className="radio-db-tag-row">
                                  {styleTags.map((tag) => <span key={tag} className="radio-db-tag">{tag}</span>)}
                                </div>
                              </div>
                            ) : null}
                            {campaignFitTags.length ? (
                              <div>
                                <div className="radio-db-section-title">Campaign fit</div>
                                <div className="radio-db-tag-row">
                                  {campaignFitTags.map((tag) => <span key={tag} className="radio-db-tag">{tag}</span>)}
                                </div>
                              </div>
                            ) : null}
                          </div>
                        ) : (
                          <div className="radio-db-empty">Noch kein Editorial-Fit-Profil für diese Station vorhanden.</div>
                        )}
                      </div>
                      <div className="radio-db-box">
                        <h3>Station groups</h3>
                        <div className="radio-db-list">
                          {stationMemberships.length ? stationMemberships.map((group) => (
                            <div key={group.id} className="radio-db-list-item">
                              <div className="radio-db-run__head">
                                <strong>{groupLabel(group)}</strong>
                                <span>{group.station_count} stations</span>
                              </div>
                              <div className="radio-db-inline-actions">
                                <button type="button" className="radio-db-link-button" onClick={() => { setSelectedGroupId(group.id); setActiveTab("campaigns"); }}>
                                  Open group
                                </button>
                                <button type="button" className="radio-db-link-button" onClick={() => void handleRemoveCurrentStationFromGroup(group.id)}>
                                  Remove
                                </button>
                              </div>
                            </div>
                          )) : <div className="radio-db-empty">Diese Station ist noch keiner Gruppe zugeordnet.</div>}
                        </div>
                        {stationGroups.length ? (
                          <div className="radio-db-inline-actions">
                            {stationGroups
                              .filter((group) => !stationMemberships.some((membership) => membership.id === group.id))
                              .slice(0, 4)
                              .map((group) => (
                                <button key={group.id} type="button" className="radio-db-link-button" onClick={() => void handleAddCurrentStationToGroup(group.id)}>
                                  Add to {group.name}
                                </button>
                              ))}
                          </div>
                        ) : null}
                      </div>
                      <div className="radio-db-box">
                        <h3>Known routes</h3>
                        <div className="radio-db-list">
                          {contactDraft?.available_routes?.length ? contactDraft.available_routes.map((route) => (
                            <div key={`${route.kind}-${route.value}`} className="radio-db-list-item">
                              <strong>{route.label}</strong>
                              <span>{route.kind}</span>
                              <small>{route.value}</small>
                            </div>
                          )) : <div className="radio-db-empty">Noch keine empfohlenen Routen geladen.</div>}
                        </div>
                      </div>
                      <div className="radio-db-box">
                        <h3>Evidence summary</h3>
                        <div className="radio-db-list">
                          {contactDraft?.evidence_summary?.length ? contactDraft.evidence_summary.map((entry, index) => (
                            <div key={`${entry}-${index}`} className="radio-db-list-item">
                              <strong>Hint {index + 1}</strong>
                              <small>{entry}</small>
                            </div>
                          )) : <div className="radio-db-empty">Keine Evidence-Zusammenfassung für diesen Sender.</div>}
                        </div>
                      </div>
                    </div>
                  )}

                  {stationPanelTab === "outreach" && (
                    <div className="radio-db-stack">
                      {contactDraftLoading ? <div className="radio-db-empty">Loading contact draft…</div> : null}
                      {contactDraft ? (
                        <>
                          <div className="radio-db-summary-list">
                            <SummaryRow label="Locale" value={contactDraft.locale || `${contactDraft.country_code} / ${contactDraft.language}`} />
                            <SummaryRow label="Recommended" value={contactDraft.recommended_channel} />
                            <SummaryRow
                              label="Campaign"
                              value={
                                selectedOutreachCampaignId != null
                                  ? (() => {
                                      const row = outreachCampaigns.find((item) => item.id === selectedOutreachCampaignId);
                                      return row ? `${row.name} (#${row.id})` : `#${selectedOutreachCampaignId}`;
                                    })()
                                  : "—"
                              }
                            />
                            <SummaryRow label="Draft ID" value={contactDraft.draft_id ? String(contactDraft.draft_id) : "not persisted"} />
                            <SummaryRow label="Saved campaign link" value={contactDraft.campaign_id ? String(contactDraft.campaign_id) : "—"} />
                            <SummaryRow label="Status" value={contactDraft.status || "draft"} />
                          </div>
                          <div className="radio-db-draft">
                            <label>
                              Aktive Kampagne (Outreach)
                              <select
                                value={selectedOutreachCampaignId ?? ""}
                                onChange={(event) => {
                                  const value = event.target.value;
                                  if (!value) {
                                    setSelectedOutreachCampaignId(null);
                                    return;
                                  }
                                  const idNum = Number(value);
                                  const row = outreachCampaigns.find((item) => item.id === idNum);
                                  setSelectedOutreachCampaignId(idNum);
                                  if (row) {
                                    setCampaignDraft(campaignDtoToForm(row));
                                  }
                                }}
                              >
                                <option value="">— keine —</option>
                                {activeOutreachCampaigns.map((item) => (
                                  <option key={item.id} value={item.id}>{item.name} · {item.artist_name} — {item.song_title}</option>
                                ))}
                              </select>
                            </label>
                            <div className="radio-db-panel__actions">
                              <button
                                type="button"
                                className="radio-db-button"
                                onClick={() => void handleGenerateOutreachEmail()}
                                disabled={
                                  permissionMode === "read-only"
                                  || outreachGenerateLoading
                                  || selectedOutreachCampaignId == null
                                  || !selectedStationId
                                }
                              >
                                {outreachGenerateLoading ? "Generiere…" : "LLM: E-Mail generieren"}
                              </button>
                            </div>
                            <label>
                              Template
                              <select
                                value={selectedTemplateId}
                                onChange={(event) => setSelectedTemplateId(event.target.value === "default" ? "default" : Number(event.target.value))}
                              >
                                <option value="default">Default template</option>
                                {contactTemplates.map((template) => (
                                  <option key={template.id} value={template.id}>{template.name}</option>
                                ))}
                              </select>
                            </label>
                            <label>
                              Subject (Station-Sprache)
                              <input value={contactDraft.subject} onChange={(event) => setContactDraft((current) => current ? { ...current, subject: event.target.value } : current)} />
                            </label>
                            <label>
                              Draft body (Station-Sprache)
                              <textarea rows={12} value={contactDraft.body} onChange={(event) => setContactDraft((current) => current ? { ...current, body: event.target.value } : current)} />
                            </label>
                            {outreachMirrorEn ? (
                              <div className="radio-db-box">
                                <h3>English mirror (read-only)</h3>
                                <label>
                                  Subject (EN)
                                  <input readOnly value={outreachMirrorEn.subject_en} />
                                </label>
                                <label>
                                  Body (EN)
                                  <textarea readOnly rows={10} value={outreachMirrorEn.body_en} />
                                </label>
                              </div>
                            ) : null}
                          </div>
                          <div className="radio-db-panel__actions">
                            <button type="button" className="radio-db-button" onClick={() => void handleCopyDraft()}>Copy draft</button>
                            <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleCreatePersistedDraft()} disabled={permissionMode === "read-only" || contactSendLoading}>Save draft</button>
                            <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleRefreshContactDraftPreview()} disabled={permissionMode === "read-only" || contactSendLoading}>Refresh preview</button>
                            <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleDryRunSend()} disabled={permissionMode === "read-only" || contactSendLoading}>Dry-run send</button>
                            <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleExecuteSend()} disabled={permissionMode !== "execute" || contactSendLoading}>Execute send</button>
                          </div>
                          <div className="radio-db-box">
                            <h3>Stored drafts</h3>
                            <div className="radio-db-list">
                              {stationDrafts.length > 0 ? stationDrafts.map((draft) => (
                                <button
                                  type="button"
                                  key={draft.draft_id || `${draft.station_id}-${draft.subject}`}
                                  className={`radio-db-run ${contactDraft?.draft_id === draft.draft_id ? "radio-db-run--active" : ""}`}
                                  onClick={() => setContactDraft(draft)}
                                >
                                  <div className="radio-db-run__head">
                                    <strong>{draft.subject}</strong>
                                    <span>{draft.status || "draft"}</span>
                                  </div>
                                  <div className="radio-db-subtle">{draft.updated_at ? formatDate(draft.updated_at) : "unsaved"}</div>
                                </button>
                              )) : <div className="radio-db-empty">No stored drafts yet.</div>}
                            </div>
                          </div>
                          <div className="radio-db-box">
                            <h3>Sends and outcomes</h3>
                            <div className="radio-db-list">
                              {contactSends.length > 0 ? contactSends.map((send) => (
                                <button
                                  type="button"
                                  key={send.id}
                                  className={`radio-db-run ${selectedSendId === send.id ? "radio-db-run--active" : ""}`}
                                  onClick={() => setSelectedSendId(send.id)}
                                >
                                  <div className="radio-db-run__head">
                                    <strong>Send #{send.id}</strong>
                                    <span>{send.status}</span>
                                  </div>
                                  <small>{send.channel} · {send.target_value || "no target"} · {formatDate(send.created_at)}</small>
                                </button>
                              )) : <div className="radio-db-empty">Noch keine Send-Versuche.</div>}
                            </div>
                            <div className="radio-db-list">
                              {contactOutcomes.length > 0 ? contactOutcomes.map((outcome) => (
                                <div key={outcome.id} className="radio-db-list-item">
                                  <strong>{outcome.outcome_type}</strong>
                                  <span>{outcome.status}</span>
                                  <small>{outcome.details || formatDate(outcome.created_at)}</small>
                                </div>
                              )) : <div className="radio-db-empty">Noch keine Outcome-Timeline.</div>}
                            </div>
                          </div>
                        </>
                      ) : (
                        <div className="radio-db-empty">Kein Draft verfügbar.</div>
                      )}
                    </div>
                  )}

                  {stationPanelTab === "forms" && (
                    <div className="radio-db-stack">
                      <div className="radio-db-summary-list">
                        <SummaryRow label="Submissions" value={String(selectedStation?.submissions.length ?? 0)} />
                        <SummaryRow label="Forms" value={String(selectedStation?.forms.length ?? 0)} />
                        <SummaryRow label="Manual mode" value={manualScanMode} />
                        <SummaryRow label="Max pages" value={String(manualScanPages)} />
                      </div>
                      <div className="radio-db-run-launcher">
                        <div className="radio-db-grid radio-db-grid--two-column">
                          <label>
                            Scan mode
                            <input value={manualScanMode} onChange={(event) => setManualScanMode(event.target.value)} />
                          </label>
                          <label>
                            Max pages
                            <input type="number" min={1} max={10} value={manualScanPages} onChange={(event) => setManualScanPages(Number(event.target.value))} />
                          </label>
                        </div>
                        <label>
                          Start URL
                          <input value={browserUrlDraft} onChange={(event) => setBrowserUrlDraft(event.target.value)} placeholder={selectedStation?.website_url || currentStation.website_url || "https://example.com"} />
                        </label>
                        <div className="radio-db-panel__actions">
                          <button type="button" className="radio-db-button" onClick={() => void handleStartManualScan()} disabled={manualScanBusy}>Start manual scan</button>
                          <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleOpenBrowserSession()}>Open supervised browser</button>
                          <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("runs")}>Open runs console</button>
                        </div>
                      </div>
                      <div className="radio-db-box">
                        <h3>Known forms</h3>
                        <div className="radio-db-list">
                          {selectedStation?.forms.length ? selectedStation.forms.map((form) => (
                            <div key={form.id} className="radio-db-list-item">
                              <strong>{form.page_title || form.url}</strong>
                              <span>{form.form_type} · {form.status}</span>
                              <small>{form.language || "lang?"} · {form.requires_login ? "login" : "public"} · {form.has_captcha ? "captcha" : "no captcha"}</small>
                            </div>
                          )) : <div className="radio-db-empty">Keine Forms für diese Station gespeichert.</div>}
                        </div>
                      </div>
                      <div className="radio-db-box">
                        <h3>Submission routes</h3>
                        <div className="radio-db-list">
                          {selectedStation?.submissions.length ? selectedStation.submissions.map((submission) => (
                            <div key={submission.id} className="radio-db-list-item">
                              <strong>{submission.method}</strong>
                              <span>{submission.email || submission.url || "no route"}</span>
                              <small>{submission.requirements || "No requirements recorded."}</small>
                            </div>
                          )) : <div className="radio-db-empty">Keine Submission-Routen im Datensatz.</div>}
                        </div>
                      </div>
                    </div>
                  )}

                  {stationPanelTab === "activity" && (
                    <div className="radio-db-stack">
                      <div className="radio-db-box">
                        <h3>Recent runs for station</h3>
                        <div className="radio-db-list">
                          {agentRuns.length > 0 ? agentRuns.filter((run) => run.station_id === selectedStationId).slice(0, 12).map((run) => (
                            <button
                              type="button"
                              key={run.id}
                              className={`radio-db-run ${selectedRunId === run.id ? "radio-db-run--active" : ""}`}
                              onClick={() => {
                                setSelectedRunId(run.id);
                                setActiveTab("runs");
                              }}
                            >
                              <div className="radio-db-run__head">
                                <strong>Run #{run.id}</strong>
                                <span className={`radio-db-pill radio-db-pill--${statusTone(run.status)}`}>{run.status}</span>
                              </div>
                              <small>{run.goal} · {formatDate(run.started_at)}</small>
                            </button>
                          )) : <div className="radio-db-empty">Keine Runs für diese Station geladen.</div>}
                        </div>
                      </div>
                      <div className="radio-db-box">
                        <h3>Programs and people</h3>
                        <div className="radio-db-list">
                          {selectedStation?.programs.map((program) => (
                            <div key={program.id} className="radio-db-list-item">
                              <strong>{program.name}</strong>
                              <small>{program.schedule || "unscheduled"} · {program.description || "No description."}</small>
                            </div>
                          ))}
                          {selectedStation?.people.map((person) => (
                            <div key={person.id} className="radio-db-list-item">
                              <strong>{person.name || "unknown"}</strong>
                              <span>{person.role}</span>
                              <small>{person.email || person.contact_url || "no direct route"}</small>
                            </div>
                          ))}
                          {!selectedStation?.programs.length && !selectedStation?.people.length ? <div className="radio-db-empty">Keine Programme oder Personen gespeichert.</div> : null}
                        </div>
                      </div>
                    </div>
                  )}

                  {stationPanelTab === "edit" && stationDraft && (
                    <div className="radio-db-stack">
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
                        <button type="button" className="radio-db-button" onClick={() => void handleStationSave()} disabled={stationSaving}>Save station</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleManualConfirm()} disabled={manualConfirmBusy}>Manual confirm</button>
                        <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleDeleteStation()} disabled={deleteBusy}>Delete station</button>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="radio-db-empty">Select a station to open the workbench.</div>
              )}
            </aside>
          </section>
        )}

        {activeTab === "runs" && (
          <section className="radio-db-grid radio-db-grid--runs">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Run Queue</h2>
                  <p>Manuelle Form-Scans, Review-Läufe und Browser-Supervision in einer Konsole.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshRuns(selectedStationId ?? undefined, runStatusFilter || undefined)} disabled={runsLoading}>
                    Refresh
                  </button>
                  <button type="button" className="radio-db-button" onClick={() => void handleOpenBrowserSession()}>
                    Open browser
                  </button>
                </div>
              </div>

              <div className="radio-db-grid radio-db-grid--two-column">
                <label>
                  Run status
                  <select value={runStatusFilter} onChange={(event) => setRunStatusFilter(event.target.value)}>
                    <option value="">all</option>
                    <option value="running">running</option>
                    <option value="blocked">blocked</option>
                    <option value="completed">completed</option>
                    <option value="failed">failed</option>
                  </select>
                </label>
                <label>
                  Station context
                  <input value={selectedStation?.canonical_name || currentStation?.canonical_name || ""} readOnly />
                </label>
              </div>

              <div className="radio-db-summary-strip">
                <div><span>Total runs</span><strong>{runsTotal}</strong></div>
                <div><span>Selected run</span><strong>{selectedRunId ?? "-"}</strong></div>
                <div><span>Browser sessions</span><strong>{browserSessions.length}</strong></div>
              </div>

              {runsLoading ? <div className="radio-db-empty">Loading runs…</div> : null}
              <div className="radio-db-list radio-db-list--runs">
                {agentRuns.length > 0 ? agentRuns.map((run) => (
                  <button
                    type="button"
                    key={run.id}
                    className={`radio-db-run ${selectedRunId === run.id ? "radio-db-run--active" : ""}`}
                    onClick={() => setSelectedRunId(run.id)}
                  >
                    <div className="radio-db-run__head">
                      <strong>#{run.id} · {run.station_name}</strong>
                      <span className={`radio-db-pill radio-db-pill--${statusTone(run.status)}`}>{run.status}</span>
                    </div>
                    <div className="radio-db-subtle">{run.goal}</div>
                    <div className="radio-db-mini-metrics">
                      <span>{run.mode}</span>
                      <span>{run.step_count} steps</span>
                      <span>{run.issue_count} issues</span>
                      <span>{formatDate(run.started_at)}</span>
                    </div>
                  </button>
                )) : <div className="radio-db-empty">Keine Runs im aktuellen Filter.</div>}
              </div>
            </article>

            <article className="radio-db-panel radio-db-panel--wide">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Supervisor Console</h2>
                  <p>Run-Detail, Artefakte und Browser-Überwachung für manuelle Steuerung in Phase 1 bis 3.</p>
                </div>
              </div>

              {selectedRun ? (
                <div className="radio-db-stack">
                  <div className="radio-db-box-grid">
                    <div className="radio-db-box">
                      <h3>Run overview</h3>
                      <div className="radio-db-summary-list">
                        <SummaryRow label="Station" value={selectedRun.station_name} />
                        <SummaryRow label="Goal" value={selectedRun.goal} />
                        <SummaryRow label="State" value={selectedRun.current_state} />
                        <SummaryRow label="Confidence" value={formatPercent(selectedRun.confidence)} />
                        <SummaryRow label="Artifacts" value={String(selectedRunArtifactCount)} />
                        <SummaryRow label="Blocked" value={selectedRun.blocked_reason || "-"} />
                      </div>
                    </div>
                    <div className="radio-db-box">
                      <h3>Supervisor actions</h3>
                      <div className="radio-db-panel__actions">
                        <button type="button" className="radio-db-button" onClick={() => void handleOpenBrowserSession()}>Browser for run</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setPrimaryStation(selectedRun.station_id)}>Open station</button>
                        <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => openArtifact(selectedRun.steps.at(-1)?.screenshot_path)}>Latest screenshot</button>
                      </div>
                      <pre>{JSON.stringify(selectedRun.summary, null, 2)}</pre>
                    </div>
                  </div>

                  <div className="radio-db-box-grid">
                    <div className="radio-db-box">
                      <h3>Issues</h3>
                      <div className="radio-db-list">
                        {selectedRun.issues.length ? selectedRun.issues.map((issue) => (
                          <div key={issue.id} className="radio-db-list-item">
                            <strong>{issue.title}</strong>
                            <span>{issue.issue_type} · {issue.severity}</span>
                            <small>{issue.details || issue.status}</small>
                          </div>
                        )) : <div className="radio-db-empty">Keine Issues für diesen Run.</div>}
                      </div>
                    </div>
                    <div className="radio-db-box">
                      <h3>Steps</h3>
                      <div className="radio-db-list radio-db-list--timeline">
                        {selectedRun.steps.slice().reverse().map((step) => (
                          <div key={step.id} className="radio-db-list-item">
                            <div className="radio-db-run__head">
                              <strong>Step {step.step_index}</strong>
                              <span>{step.state_after}</span>
                            </div>
                            <small>{formatDate(step.created_at)} · {step.latency_ms} ms · confidence {formatPercent(step.confidence)}</small>
                            <div className="radio-db-inline-actions">
                              {step.screenshot_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(step.screenshot_path)}>Screenshot</button> : null}
                              {step.dom_snapshot_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(step.dom_snapshot_path)}>DOM</button> : null}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              ) : runLoading ? (
                <div className="radio-db-empty">Loading run detail…</div>
              ) : (
                <div className="radio-db-empty">Select a run to inspect it.</div>
              )}

              <div className="radio-db-box">
                <div className="radio-db-panel__head">
                  <div>
                    <h3>Live Browser Supervision</h3>
                    <p>Visuelle manuelle Kontrolle für Forms und navigierte Sessions.</p>
                  </div>
                </div>
                <div className="radio-db-run-launcher">
                  <label>
                    Start URL
                    <input value={browserUrlDraft} onChange={(event) => setBrowserUrlDraft(event.target.value)} placeholder={selectedRun?.target_url || selectedStation?.website_url || currentStation?.website_url || "https://example.com"} />
                  </label>
                  <div className="radio-db-panel__actions">
                    <button type="button" className="radio-db-button" onClick={() => void handleOpenBrowserSession()}>Open browser session</button>
                    <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleBrowserNavigate()} disabled={!browserSessionId}>Navigate</button>
                    <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleBrowserSnapshot()} disabled={!browserSessionId}>Snapshot</button>
                    <button type="button" className="radio-db-button radio-db-button--danger" onClick={() => void handleBrowserClose()} disabled={!browserSessionId}>Close</button>
                  </div>
                  <details className="radio-db-debug-panel">
                    <summary>Debug controls</summary>
                    <div className="radio-db-grid radio-db-grid--two-column">
                      <label>
                        Action
                        <select value={browserAction} onChange={(event) => setBrowserAction(event.target.value as BrowserActionType)}>
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
                    <button type="button" className="radio-db-button" onClick={() => void handleBrowserAction()} disabled={!browserSessionId}>Run action</button>
                  </details>
                </div>

                {browserSessionDetail || selectedRunLatestScreenshotUrl ? (
                  <div className="radio-db-browser-monitor">
                    <div className="radio-db-browser-preview">
                      <div className="radio-db-browser-preview__head">
                        <div>
                          <strong>{browserSessionDetail?.title || selectedRun?.station_name || "Form scan screenshot"}</strong>
                          <span>{browserSessionDetail?.url || selectedRun?.target_url || firstText(latestRunAction.form_url, latestRunResult.form_url, "")}</span>
                        </div>
                        <span className={`radio-db-pill radio-db-pill--${statusTone(browserSessionDetail?.status || selectedRun?.status || "muted")}`}>{browserSessionDetail?.status || selectedRun?.status || "snapshot"}</span>
                      </div>
                      {browserScreenshotUrl ? (
                        <button type="button" className="radio-db-browser-shot" onClick={() => openArtifact(browserSessionDetail?.screenshot_path)}>
                          <img src={browserScreenshotUrl} alt="Current supervised browser screenshot" />
                        </button>
                      ) : selectedRunLatestScreenshotUrl ? (
                        <button type="button" className="radio-db-browser-shot" onClick={() => openArtifact(latestRunStep?.screenshot_path)}>
                          <img src={selectedRunLatestScreenshotUrl} alt="Latest form scan screenshot" />
                        </button>
                      ) : (
                        <div className="radio-db-browser-shot radio-db-browser-shot--empty">
                          <span>No screenshot captured yet.</span>
                        </div>
                      )}
                      <div className="radio-db-summary-strip radio-db-summary-strip--compact">
                        <div><span>Events</span><strong>{browserSessionDetail?.event_count ?? selectedRun?.steps.length ?? 0}</strong></div>
                        <div><span>Actions</span><strong>{browserSessionDetail?.action_count ?? selectedRunArtifactCount}</strong></div>
                        <div><span>Updated</span><strong>{formatDate(browserSessionDetail?.updated_at || latestRunStep?.created_at || selectedRun?.updated_at)}</strong></div>
                      </div>
                      <div className="radio-db-inline-actions">
                        {browserSessionDetail?.screenshot_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(browserSessionDetail.screenshot_path)}>Open full screenshot</button> : null}
                        {!browserSessionDetail?.screenshot_path && latestRunStep?.screenshot_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(latestRunStep.screenshot_path)}>Open full screenshot</button> : null}
                        {browserSessionDetail?.html_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(browserSessionDetail.html_path)}>Open HTML snapshot</button> : null}
                        {latestRunStep?.dom_snapshot_path ? <button type="button" className="radio-db-link-button" onClick={() => openArtifact(latestRunStep.dom_snapshot_path)}>Open DOM snapshot</button> : null}
                      </div>
                    </div>

                    <div className="radio-db-browser-inspector">
                      <div className="radio-db-box">
                        <h3>Scan signal</h3>
                        <div className="radio-db-summary-list">
                          <SummaryRow label="Station" value={selectedRun?.station_name || selectedStation?.canonical_name || currentStation?.canonical_name || "-"} />
                          <SummaryRow label="Run state" value={selectedRun?.current_state || browserSessionDetail?.status || "-"} />
                          <SummaryRow label="Last step" value={latestRunStep ? `${latestRunStep.state_before} -> ${latestRunStep.state_after}` : "-"} />
                          <SummaryRow label="Blocker" value={runBlocker || browserSessionDetail?.last_error || "-"} />
                        </div>
                      </div>
                      <div className="radio-db-box">
                        <h3>Agent observation</h3>
                        <div className="radio-db-list">
                          <div className="radio-db-list-item">
                            <strong>{agentObservationTitle}</strong>
                            <span>{agentObservationAction}</span>
                            <small>{agentObservationMeta}</small>
                          </div>
                        </div>
                        <details className="radio-db-debug-panel">
                          <summary>Raw latest step JSON</summary>
                          <pre>{JSON.stringify({ observation: latestRunObservation, action: latestRunAction, result: latestRunResult }, null, 2)}</pre>
                        </details>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="radio-db-empty">Select or open a session to follow its live screenshot and scan signals.</div>
                )}

                <div className="radio-db-box-grid radio-db-browser-lower">
                  <div className="radio-db-box">
                    <h3>Sessions</h3>
                    {browserSessionsLoading ? <div className="radio-db-empty">Loading sessions…</div> : null}
                    <div className="radio-db-list">
                      {browserSessions.map((session) => (
                        <button
                          type="button"
                          key={session.id}
                          className={`radio-db-run ${browserSessionId === session.id ? "radio-db-run--active" : ""}`}
                          onClick={() => setBrowserSessionId(session.id)}
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
                    <h3>Found routes</h3>
                    <div className="radio-db-list">
                      {monitorCandidates.length ? monitorCandidates.map((candidate) => (
                        <div key={candidate.id} className="radio-db-list-item">
                          <div className="radio-db-run__head">
                            <strong>{candidate.label}</strong>
                            <span className={`radio-db-pill radio-db-pill--${statusTone(candidate.status)}`}>{candidate.status}</span>
                          </div>
                          <span>{candidate.route}</span>
                          <small>{candidate.type}{candidate.confidence !== null ? ` · confidence ${formatPercent(candidate.confidence)}` : ""} · {candidate.note}</small>
                        </div>
                      )) : <div className="radio-db-empty">No saved forms or submission routes for the selected station yet.</div>}
                    </div>
                  </div>
                  <div className="radio-db-box radio-db-box--wide">
                    <h3>Readable timeline</h3>
                    {browserSessionDetail ? (
                      <div className="radio-db-stack">
                        <div className="radio-db-list radio-db-list--timeline">
                          {browserEvents.length ? browserEvents.map((event) => (
                            <div key={`${String(event.id)}-${String(event.at)}`} className="radio-db-list-item">
                              <div className="radio-db-run__head">
                                <strong>{browserEventLabel(event)}</strong>
                                <span className={`radio-db-pill radio-db-pill--${browserEventTone(event)}`}>{browserEventPhase(event)}</span>
                              </div>
                              <span>{String(event.url || "")}</span>
                              <small>{formatDate(String(event.at || ""))}</small>
                            </div>
                          )) : <div className="radio-db-empty">No browser events recorded yet.</div>}
                        </div>
                      </div>
                    ) : (
                      <div className="radio-db-empty">Select or open a session to follow its readable event stream.</div>
                    )}
                  </div>
                </div>
              </div>
            </article>
          </section>
        )}

        {activeTab === "campaigns" && (
          <section className="radio-db-grid radio-db-grid--campaigns">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Campaign Workspace</h2>
                  <p>Eine persistierte Kampagne pro Song: Pitch, Referenz-Stil und Operator-Infos; Outreach-TAB nutzt dieselbe Auswahl für LLM-Mails.</p>
                </div>
              </div>

              <div className="radio-db-grid radio-db-grid--two-column">
                <div className="radio-db-box">
                  <h3>Kampagnen</h3>
                  {outreachCampaignsLoading ? <div className="radio-db-empty">Lade Kampagnen…</div> : null}
                  <div className="radio-db-panel__actions">
                    <button
                      type="button"
                      className="radio-db-button radio-db-button--ghost"
                      onClick={() => {
                        setSelectedOutreachCampaignId(null);
                        setCampaignDraft(emptyCampaignDraft());
                      }}
                      disabled={campaignWorkspaceBusy}
                    >
                      Neue Kampagne
                    </button>
                    <button
                      type="button"
                      className="radio-db-button radio-db-button--ghost"
                      onClick={() => void refreshOutreachCampaigns()}
                      disabled={outreachCampaignsLoading || campaignWorkspaceBusy}
                    >
                      Aktualisieren
                    </button>
                  </div>
                  <div className="radio-db-list radio-db-list--runs">
                    {outreachCampaigns.length ? outreachCampaigns.map((campaignItem) => (
                      <button
                        type="button"
                        key={campaignItem.id}
                        className={`radio-db-run ${selectedOutreachCampaignId === campaignItem.id ? "radio-db-run--active" : ""}`}
                        onClick={() => {
                          setSelectedOutreachCampaignId(campaignItem.id);
                          setCampaignDraft(campaignDtoToForm(campaignItem));
                        }}
                      >
                        <div className="radio-db-run__head">
                          <strong>{campaignItem.name}</strong>
                          <span className={`radio-db-pill radio-db-pill--${campaignItem.is_active ? "positive" : "neutral"}`}>
                            {campaignItem.is_active ? "aktiv" : "archiv"}
                          </span>
                        </div>
                        <small>{campaignItem.artist_name} — {campaignItem.song_title}</small>
                        <small>
                          Press clicks: {campaignItem.press_release_click_count}
                          {campaignItem.press_release_last_clicked_at ? ` · zuletzt ${new Date(campaignItem.press_release_last_clicked_at).toLocaleString("de-DE")}` : ""}
                        </small>
                      </button>
                    )) : <div className="radio-db-empty">Noch keine Kampagne angelegt.</div>}
                  </div>
                </div>

                <div className="radio-db-edit-form">
                  <label>
                    Campaign name
                    <input value={campaignDraft.name} onChange={(event) => setCampaignDraft((current) => ({ ...current, name: event.target.value }))} placeholder="April indie push" />
                  </label>
                  <div className="radio-db-grid radio-db-grid--two-column">
                    <label>
                      Artist
                      <input value={campaignDraft.artist} onChange={(event) => setCampaignDraft((current) => ({ ...current, artist: event.target.value }))} placeholder="Artist name" />
                    </label>
                    <label>
                      Song title
                      <input value={campaignDraft.songTitle} onChange={(event) => setCampaignDraft((current) => ({ ...current, songTitle: event.target.value }))} placeholder="Song title" />
                    </label>
                  </div>
                  <div className="radio-db-grid radio-db-grid--two-column">
                    <label>
                      Track / pitch language hint
                      <input value={campaignDraft.primaryLanguage} onChange={(event) => setCampaignDraft((current) => ({ ...current, primaryLanguage: event.target.value }))} placeholder="EN / DE / ES" />
                    </label>
                    <label>
                      Release date
                      <input type="date" value={campaignDraft.releaseDate} onChange={(event) => setCampaignDraft((current) => ({ ...current, releaseDate: event.target.value }))} />
                    </label>
                  </div>
                  <label>
                    Press release link
                    <input
                      value={campaignDraft.pressReleaseUrl}
                      onChange={(event) => setCampaignDraft((current) => ({ ...current, pressReleaseUrl: event.target.value }))}
                      placeholder="https://public-air.net/press-releases/..."
                    />
                  </label>
                  <label>
                    Short tracking code / ISRC
                    <input
                      value={campaignDraft.trackingCode}
                      onChange={(event) => setCampaignDraft((current) => ({ ...current, trackingCode: event.target.value }))}
                      placeholder="z. B. DEABC2600001"
                    />
                  </label>
                  {selectedOutreachCampaignId != null ? (() => {
                    const selectedCampaign = outreachCampaigns.find((item) => item.id === selectedOutreachCampaignId);
                    return selectedCampaign?.press_release_short_tracking_url ? (
                      <div className="radio-db-box">
                        <div className="radio-db-section-title">Short Tracking Link</div>
                        <a href={selectedCampaign.press_release_short_tracking_url} target="_blank" rel="noreferrer">
                          {selectedCampaign.press_release_short_tracking_url}
                        </a>
                        <p className="radio-db-muted">
                          Wird in LLM-generierten E-Mails verwendet und leitet auf den Press-Release-Link weiter.
                        </p>
                        <p className="radio-db-muted">
                          Klicks: {selectedCampaign.press_release_click_count}
                          {selectedCampaign.press_release_last_clicked_at ? ` · letzter Klick: ${new Date(selectedCampaign.press_release_last_clicked_at).toLocaleString("de-DE")}` : ""}
                        </p>
                      </div>
                    ) : null;
                  })() : null}
                  <div className="radio-db-box">
                    <div className="radio-db-section-title">Submission identity</div>
                    <div className="radio-db-grid radio-db-grid--two-column">
                      <label>
                        Contact name
                        <input value={campaignDraft.contactName} onChange={(event) => setCampaignDraft((current) => ({ ...current, contactName: event.target.value }))} />
                      </label>
                      <label>
                        Contact email
                        <input value={campaignDraft.contactEmail} onChange={(event) => setCampaignDraft((current) => ({ ...current, contactEmail: event.target.value }))} />
                      </label>
                      <label>
                        Contact phone
                        <input value={campaignDraft.contactPhone} onChange={(event) => setCampaignDraft((current) => ({ ...current, contactPhone: event.target.value }))} />
                      </label>
                      <label>
                        Label
                        <input value={campaignDraft.labelName} onChange={(event) => setCampaignDraft((current) => ({ ...current, labelName: event.target.value }))} />
                      </label>
                    </div>
                  </div>
                  <div className="radio-db-box">
                    <div className="radio-db-section-title">Artist / release form data</div>
                    <div className="radio-db-grid radio-db-grid--two-column">
                      <label>
                        Artist city
                        <input value={campaignDraft.artistCity} onChange={(event) => setCampaignDraft((current) => ({ ...current, artistCity: event.target.value }))} />
                      </label>
                      <label>
                        Artist country
                        <input value={campaignDraft.artistCountry} onChange={(event) => setCampaignDraft((current) => ({ ...current, artistCountry: event.target.value }))} />
                      </label>
                      <label>
                        Genre
                        <input value={campaignDraft.genre} onChange={(event) => setCampaignDraft((current) => ({ ...current, genre: event.target.value }))} />
                      </label>
                      <label>
                        Artist website
                        <input value={campaignDraft.artistWebsite} onChange={(event) => setCampaignDraft((current) => ({ ...current, artistWebsite: event.target.value }))} />
                      </label>
                      <label>
                        Spotify URL
                        <input value={campaignDraft.spotifyUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, spotifyUrl: event.target.value }))} />
                      </label>
                      <label>
                        SoundCloud URL
                        <input value={campaignDraft.soundcloudUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, soundcloudUrl: event.target.value }))} />
                      </label>
                      <label>
                        YouTube URL
                        <input value={campaignDraft.youtubeUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, youtubeUrl: event.target.value }))} />
                      </label>
                      <label>
                        Bandcamp URL
                        <input value={campaignDraft.bandcampUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, bandcampUrl: event.target.value }))} />
                      </label>
                    </div>
                    <label>
                      Short artist bio
                      <textarea rows={3} value={campaignDraft.artistBioShort} onChange={(event) => setCampaignDraft((current) => ({ ...current, artistBioShort: event.target.value }))} />
                    </label>
                    <label>
                      Long artist bio
                      <textarea rows={4} value={campaignDraft.artistBioLong} onChange={(event) => setCampaignDraft((current) => ({ ...current, artistBioLong: event.target.value }))} />
                    </label>
                  </div>
                  <div className="radio-db-box">
                    <div className="radio-db-section-title">Release assets</div>
                    <div className="radio-db-grid radio-db-grid--two-column">
                      <label>
                        Audio file URL
                        <input value={campaignDraft.audioFileUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, audioFileUrl: event.target.value }))} />
                      </label>
                      <label>
                        Download URL
                        <input value={campaignDraft.downloadUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, downloadUrl: event.target.value }))} />
                      </label>
                      <label>
                        Press photo URL
                        <input value={campaignDraft.pressPhotoUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, pressPhotoUrl: event.target.value }))} />
                      </label>
                      <label>
                        Cover art URL
                        <input value={campaignDraft.coverArtUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, coverArtUrl: event.target.value }))} />
                      </label>
                      <label>
                        Press kit URL
                        <input value={campaignDraft.pressKitUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, pressKitUrl: event.target.value }))} />
                      </label>
                      <label>
                        Lyrics URL
                        <input value={campaignDraft.lyricsUrl} onChange={(event) => setCampaignDraft((current) => ({ ...current, lyricsUrl: event.target.value }))} />
                      </label>
                      <label>
                        Label code
                        <input value={campaignDraft.labelCode} onChange={(event) => setCampaignDraft((current) => ({ ...current, labelCode: event.target.value }))} />
                      </label>
                    </div>
                  </div>
                  <label>
                    Pitch (radio hook)
                    <textarea rows={5} value={campaignDraft.pitch} onChange={(event) => setCampaignDraft((current) => ({ ...current, pitch: event.target.value }))} placeholder="Kurzpitch, Story, relevante Hooks." />
                  </label>
                  <label>
                    Reference template (Stil-Vorlage)
                    <textarea rows={4} value={campaignDraft.referenceTemplate} onChange={(event) => setCampaignDraft((current) => ({ ...current, referenceTemplate: event.target.value }))} placeholder="Optional: Beispiel-Mail oder Satzführung; das LLM orientiert sich daran, ohne Fakten zu erfinden." />
                  </label>
                  <label>
                    Operator notes
                    <textarea rows={4} value={campaignDraft.notes} onChange={(event) => setCampaignDraft((current) => ({ ...current, notes: event.target.value }))} placeholder="Welche Märkte zuerst, welche Sender nur manuell, welche später automatisieren." />
                  </label>
                  <div className="radio-db-panel__actions">
                    <button
                      type="button"
                      className="radio-db-button"
                      onClick={() => void handleSaveOutreachCampaign()}
                      disabled={campaignWorkspaceBusy || permissionMode === "read-only"}
                    >
                      Speichern
                    </button>
                    <button
                      type="button"
                      className="radio-db-button radio-db-button--ghost"
                      onClick={() => void handleArchiveOutreachCampaign()}
                      disabled={campaignWorkspaceBusy || selectedOutreachCampaignId == null || permissionMode === "read-only"}
                    >
                      Archivieren
                    </button>
                  </div>
                </div>
              </div>
            </article>

            <article className="radio-db-panel radio-db-panel--wide">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Campaign Monitor</h2>
                  <p>Übersicht pro Kampagne: welche Sender vorbereitet, gesendet und über den Tracking-Link geklickt haben.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button
                    type="button"
                    className="radio-db-button radio-db-button--ghost"
                    onClick={() => void refreshCampaignMonitor()}
                    disabled={campaignMonitorLoading || selectedOutreachCampaignId == null}
                  >
                    Monitor aktualisieren
                  </button>
                </div>
              </div>
              {selectedOutreachCampaignId == null ? (
                <div className="radio-db-empty">Wähle eine Kampagne aus, um den Monitor zu sehen.</div>
              ) : (
                <>
                  <div className="radio-db-summary-strip">
                    <div><span>Sender</span><strong>{campaignMonitor?.summary.total ?? 0}</strong></div>
                    <div><span>Drafts</span><strong>{campaignMonitor?.summary.drafted ?? 0}</strong></div>
                    <div><span>Gesendet</span><strong>{campaignMonitor?.summary.sent ?? 0}</strong></div>
                    <div><span>Geklickt</span><strong>{campaignMonitor?.summary.clicked ?? 0}</strong></div>
                    <div><span>Fehler</span><strong>{(campaignMonitor?.summary.failed ?? 0) + (campaignMonitor?.summary.blocked ?? 0)}</strong></div>
                  </div>
                  <div className="radio-db-panel__actions">
                    {(["all", "drafted", "sent", "clicked", "failed", "blocked", "simulated"] as CampaignMonitorFilter[]).map((filter) => (
                      <button
                        type="button"
                        key={filter}
                        className={`radio-db-button ${campaignMonitorFilter === filter ? "" : "radio-db-button--ghost"}`}
                        onClick={() => setCampaignMonitorFilter(filter)}
                      >
                        {filter}
                      </button>
                    ))}
                  </div>
                  <div className="radio-db-table-shell">
                    {campaignMonitorLoading ? <div className="radio-db-empty">Lade Campaign Monitor…</div> : null}
                    {!campaignMonitorLoading && filteredCampaignMonitorItems.length === 0 ? <div className="radio-db-empty">Noch keine Drafts, Sends oder Clicks für diese Kampagne.</div> : null}
                    <table className="radio-db-table">
                      <thead>
                        <tr>
                          <th>Station</th>
                          <th>Status</th>
                          <th>Draft</th>
                          <th>Send</th>
                          <th>Clicks</th>
                          <th>Tracking Link</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredCampaignMonitorItems.map((item) => (
                          <tr key={item.station_id} className="radio-db-row">
                            <td>
                              <strong>{item.station_name}</strong>
                              <div className="radio-db-subtle">{item.country_code || "--"} / {item.language || "--"}</div>
                            </td>
                            <td><span className={`radio-db-pill radio-db-pill--${statusTone(item.monitor_status)}`}>{item.monitor_status}</span></td>
                            <td>
                              {item.draft_id ? `#${item.draft_id}` : "-"}
                              <div className="radio-db-subtle">{item.draft_status || "no draft"}</div>
                            </td>
                            <td>
                              {item.send_status || "-"}
                              <div className="radio-db-subtle">
                                {item.last_sent_at ? new Date(item.last_sent_at).toLocaleString("de-DE") : "not sent"}
                                {item.send_channel ? ` · ${item.send_channel}` : ""}
                              </div>
                            </td>
                            <td>
                              <strong>{item.click_count}</strong>
                              <div className="radio-db-subtle">{item.last_clicked_at ? new Date(item.last_clicked_at).toLocaleString("de-DE") : "not clicked"}</div>
                            </td>
                            <td>
                              {item.tracking_url ? (
                                <a href={item.tracking_url} target="_blank" rel="noreferrer">{item.tracking_url}</a>
                              ) : "-"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </article>

            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Station Groups</h2>
                  <p>Persistente Gruppen für Artists, Releases oder manuelle Bulk-Runden.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => setActiveTab("stations")}>Mehr Sender auswählen</button>
                </div>
              </div>
              <div className="radio-db-edit-form">
                <label>
                  Group name
                  <input value={groupDraft.name} onChange={(event) => setGroupDraft((current) => ({ ...current, name: event.target.value }))} placeholder="Indie artists DE" />
                </label>
                <label>
                  Artist key
                  <input value={groupDraft.artistKey} onChange={(event) => setGroupDraft((current) => ({ ...current, artistKey: event.target.value }))} placeholder="optional artist or release tag" />
                </label>
                <label>
                  Description
                  <textarea rows={3} value={groupDraft.description} onChange={(event) => setGroupDraft((current) => ({ ...current, description: event.target.value }))} placeholder="Wofür ist diese Gruppe gedacht?" />
                </label>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button" onClick={() => void handleCreateGroup()} disabled={groupBusy}>Create group</button>
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshStationGroups()} disabled={stationGroupsLoading}>Refresh groups</button>
                </div>
              </div>

              <div className="radio-db-summary-strip">
                <div><span>Total groups</span><strong>{stationGroups.length}</strong></div>
                <div><span>Selected group</span><strong>{selectedGroupDetail?.name || "-"}</strong></div>
                <div><span>Selected targets</span><strong>{selectedGroupDetail?.station_count ?? 0}</strong></div>
                <div><span>Ad-hoc targets</span><strong>{campaignStations.length}</strong></div>
              </div>

              <div className="radio-db-list radio-db-list--runs">
                {stationGroups.length ? stationGroups.map((group) => (
                  <button
                    type="button"
                    key={group.id}
                    className={`radio-db-run ${selectedGroupId === group.id ? "radio-db-run--active" : ""}`}
                    onClick={() => setSelectedGroupId(group.id)}
                  >
                    <div className="radio-db-run__head">
                      <strong>{groupLabel(group)}</strong>
                      <span>{group.station_count} stations</span>
                    </div>
                    <small>{group.description || "Keine Beschreibung."}</small>
                  </button>
                )) : <div className="radio-db-empty">Noch keine persistente Gruppe erstellt.</div>}
              </div>
            </article>

            <article className="radio-db-panel radio-db-panel--wide">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Group Targets + rollout plan</h2>
                  <p>Persistente Sendergruppen werden hier zur operativen Einheit für spätere Bulk-Workflows.</p>
                </div>
              </div>
              {selectedGroupDetail ? (
                <>
                  <div className="radio-db-summary-strip">
                    <div><span>Total targets</span><strong>{selectedGroupDetail.station_count}</strong></div>
                    <div><span>Verified</span><strong>{selectedGroupDetail.stations.filter((station) => station.status === "verified").length}</strong></div>
                    <div><span>Artist key</span><strong>{selectedGroupDetail.artist_key || "-"}</strong></div>
                    <div><span>Updated</span><strong>{formatDate(selectedGroupDetail.updated_at)}</strong></div>
                  </div>
                  <div className="radio-db-list radio-db-list--runs">
                    {selectedGroupDetail.stations.length ? selectedGroupDetail.stations.map((station: StationGroupStationDTO) => (
                      <div key={station.station_id} className="radio-db-list-item">
                        <div className="radio-db-run__head">
                          <strong>{station.canonical_name}</strong>
                          <span className={`radio-db-pill radio-db-pill--${statusTone(station.status)}`}>{station.status}</span>
                        </div>
                        <div className="radio-db-mini-metrics">
                          <span>{station.country_code || "--"}</span>
                          <span>{formatPercent(station.confidence_score)}</span>
                          <span>{formatDate(station.added_at)}</span>
                        </div>
                        <div className="radio-db-inline-actions">
                          <button type="button" className="radio-db-link-button" onClick={() => setPrimaryStation(station.station_id)}>Open station</button>
                        </div>
                      </div>
                    )) : <div className="radio-db-empty">Diese Gruppe enthält noch keine Sender.</div>}
                  </div>
                </>
              ) : null}
              <div className="radio-db-box-grid">
                <div className="radio-db-box">
                  <h3>Phase 1</h3>
                  <div className="radio-db-list">
                    <div className="radio-db-list-item">
                      <strong>Sender einzeln prüfen</strong>
                      <small>Über die Stations-Workbench Contacts, Forms und Website sichten.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Drafts manuell freigeben</strong>
                      <small>Pro Sender Text, Sprache und Route verifizieren.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Browser überwachen</strong>
                      <small>Scans oder Form-Interaktionen im Supervisor manuell verfolgen.</small>
                    </div>
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Phase 2</h3>
                  <div className="radio-db-list">
                    <div className="radio-db-list-item">
                      <strong>Mehrere Sender gruppieren</strong>
                      <small>Batch-Auswahl aus der Master-Liste in diese Campaign übernehmen.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Kontrollierte Serienarbeit</strong>
                      <small>Nacheinander Drafts erzeugen, dry-run senden, Scan-Runs starten.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Operator review queue</strong>
                      <small>Blocked oder review-pflichtige Runs zentral in der Runs-Konsole bearbeiten.</small>
                    </div>
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Phase 3</h3>
                  <div className="radio-db-list">
                    <div className="radio-db-list-item">
                      <strong>Song-zentrierte Inputs standardisieren</strong>
                      <small>Artist, Song, Pitch, Sprache und Regeln als zentrales Kampagnenobjekt vorbereiten.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Automationslogik vorbereiten</strong>
                      <small>Später dieselben Sender- und Run-Strukturen für automatische Dispatches nutzen.</small>
                    </div>
                    <div className="radio-db-list-item">
                      <strong>Kein zweites System bauen</strong>
                      <small>Die manuelle Workbench bleibt das Trainings- und Kontrollmodell für spätere Automatisierung.</small>
                    </div>
                  </div>
                </div>
              </div>
              {campaignStations.length ? (
                <div className="radio-db-box">
                  <h3>Ad-hoc selection</h3>
                  <div className="radio-db-list">
                    {campaignStations.map((station) => (
                      <div key={station.id} className="radio-db-list-item">
                        <div className="radio-db-run__head">
                          <strong>{station.canonical_name}</strong>
                          <span>{station.country_code || "--"}</span>
                        </div>
                        <div className="radio-db-inline-actions">
                          <button type="button" className="radio-db-link-button" onClick={() => setPrimaryStation(station.id)}>Open station</button>
                          <button type="button" className="radio-db-link-button" onClick={() => removeCampaignStation(station.id)}>Remove</button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </article>
          </section>
        )}

        {activeTab === "system" && (
          <section className="radio-db-grid radio-db-grid--two">
            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>System Monitor</h2>
                  <p>Ops- und Worker-Daten bleiben bewusst sekundär, aber klar lesbar.</p>
                </div>
                <div className="radio-db-panel__actions">
                  <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void refreshDataControl()} disabled={dataOverviewLoading}>
                    Refresh
                  </button>
                  <button type="button" className="radio-db-button" onClick={() => void handleStartCountryDiscovery()}>
                    Start discovery
                  </button>
                </div>
              </div>
              {dataOverviewLoading ? <div className="radio-db-empty">Loading system overview…</div> : null}
              <div className="radio-db-box-grid">
                <div className="radio-db-box">
                  <h3>Catalog + coverage</h3>
                  <div className="radio-db-summary-list">
                    {Object.entries(dataCatalog).slice(0, 6).map(([key, value]) => (
                      <SummaryRow key={key} label={key} value={String(value)} />
                    ))}
                    {Object.entries(dataCoverage).slice(0, 6).map(([key, value]) => (
                      <SummaryRow key={key} label={key} value={String(value)} />
                    ))}
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Scan focus</h3>
                  <div className="radio-db-summary-list">
                    <SummaryRow label="Enabled" value={String(Boolean(dataScanFocus.enabled))} />
                    <SummaryRow label="Current market" value={String(dataScanFocus.market_focus ?? "-")} />
                    <SummaryRow label="Updated at" value={formatDate(String(dataScanFocus.updated_at ?? ""))} />
                  </div>
                  <div className="radio-db-run-launcher">
                    <label>
                      New focus
                      <select value={scanFocusDraft} onChange={(event) => setScanFocusDraft(event.target.value as ScanFocus)}>
                        <option value="off">off</option>
                        <option value="international">international</option>
                        <option value="dach">dach</option>
                        <option value="anglo">anglo</option>
                        <option value="eu_core">eu_core</option>
                        <option value="top_major">top_major</option>
                      </select>
                    </label>
                    <button type="button" className="radio-db-button radio-db-button--ghost" onClick={() => void handleUpdateScanFocus()} disabled={permissionMode !== "execute"}>
                      Apply focus
                    </button>
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>LLM quota</h3>
                  <div className="radio-db-summary-list">
                    <SummaryRow label="USD today" value={String(dataLlmQuota.usd_spent_today_estimate ?? 0)} />
                    <SummaryRow label="USD cap" value={String(dataLlmQuota.max_daily_usd ?? 0)} />
                    <SummaryRow label="Calls today" value={String(dataLlmQuota.llm_calls_today ?? 0)} />
                    <SummaryRow label="Call cap" value={String(dataLlmQuota.max_llm_calls_per_day ?? 0)} />
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Boost + high priority</h3>
                  <div className="radio-db-summary-list">
                    <SummaryRow label="Boost" value={String(dataBoost.is_running ? "running" : "idle")} />
                    <SummaryRow label="High priority" value={String(dataHighPriority.is_running ? "running" : "idle")} />
                    <SummaryRow label="Boost calls" value={String(dataBoost.month_calls ?? 0)} />
                    <SummaryRow label="High priority results" value={String(dataHighPriority.last_results ?? 0)} />
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Services</h3>
                  <div className="radio-db-list">
                    {Object.entries(dataServices).map(([key, value]) => (
                      <div key={key} className="radio-db-list-item">
                        <strong>{key}</strong>
                        <span>{String(value)}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Rejected scan</h3>
                  <div className="radio-db-summary-list">
                    <SummaryRow label="Worker" value={String(rejectedScan.is_running ? "running" : "idle")} />
                    <SummaryRow label="PID" value={String(rejectedWorker.pid ?? "-")} />
                    <SummaryRow label="Elapsed" value={`${String(rejectedWorker.elapsed_seconds ?? 0)}s`} />
                    <SummaryRow label="Provider" value={String(rejectedScan.llm_provider ?? "-")} />
                    <SummaryRow label="Model" value={String(rejectedScan.llm_model ?? "-")} />
                    <SummaryRow label="Browser" value={String(rejectedScan.browser_worker_enabled ? "playwright on" : "off")} />
                    <SummaryRow label="Queue" value={String(rejectedScan.queue_rejected_total ?? 0)} />
                    <SummaryRow label="Progress" value={formatPercent(Number(rejectedScan.progress_ratio ?? 0))} />
                  </div>
                  <div className="radio-db-subtle">{String(rejectedWorker.command ?? "") || "No rejected-scan worker command detected."}</div>
                </div>
              </div>
            </article>

            <article className="radio-db-panel">
              <div className="radio-db-panel__head">
                <div>
                  <h2>Discovery + history</h2>
                  <p>Country discovery state, latest runs und API-Aktivität.</p>
                </div>
              </div>
              {countryDiscoveryLoading ? <div className="radio-db-empty">Loading country discovery…</div> : null}
              <div className="radio-db-box-grid">
                <div className="radio-db-box">
                  <h3>Country discovery</h3>
                  <pre>{JSON.stringify(dataCountryDiscovery.state ?? countryDiscovery?.state ?? {}, null, 2)}</pre>
                </div>
                <div className="radio-db-box">
                  <h3>Memory</h3>
                  <pre>{JSON.stringify(dataCountryDiscovery.memory ?? countryDiscovery?.memory ?? {}, null, 2)}</pre>
                </div>
              </div>
              <div className="radio-db-box">
                <h3>Latest rejected scan runs</h3>
                <div className="radio-db-list">
                  {Array.isArray(rejectedScan.latest_runs) && rejectedScan.latest_runs.length > 0 ? rejectedScan.latest_runs.map((row, index) => {
                    const runRow = asRecord(row);
                    return (
                      <div key={`${String(runRow.station_id ?? "run")}-${index}`} className="radio-db-list-item">
                        <strong>{String(runRow.station_name ?? runRow.station_id ?? "station")}</strong>
                        <span>{String(runRow.status ?? "-")}</span>
                        <small>
                          forms {String(runRow.forms_saved ?? 0)} · emails {String(runRow.emails_saved ?? 0)}{String(runRow.blocked_reason ?? "") ? ` · ${String(runRow.blocked_reason)}` : ""}
                        </small>
                      </div>
                    );
                  }) : <div className="radio-db-empty">No rejected scan runs loaded.</div>}
                </div>
              </div>
              <div className="radio-db-box-grid">
                <div className="radio-db-box">
                  <h3>Top issue types</h3>
                  <div className="radio-db-list">
                    {Array.isArray(rejectedScan.top_issue_types) && rejectedScan.top_issue_types.length > 0 ? rejectedScan.top_issue_types.map((row, index) => {
                      const issueRow = asRecord(row);
                      return (
                        <div key={`${String(issueRow.issue_type ?? "issue")}-${index}`} className="radio-db-list-item">
                          <strong>{String(issueRow.issue_type ?? "-")}</strong>
                          <span>{String(issueRow.total ?? 0)}</span>
                        </div>
                      );
                    }) : <div className="radio-db-empty">No issue summary loaded.</div>}
                  </div>
                </div>
                <div className="radio-db-box">
                  <h3>Recent API history</h3>
                  <div className="radio-db-list">
                    {dataOverview?.api_history?.length ? dataOverview.api_history.map((row, index) => (
                      <div key={`${String(row.ts_utc ?? "row")}-${index}`} className="radio-db-list-item">
                        <strong>{String(row.source ?? row.provider ?? row.kind ?? "api")}</strong>
                        <span>{String(row.status ?? row.result ?? row.event ?? "-")}</span>
                        <small>{String(row.ts_utc ?? row.updated_at ?? "-")}</small>
                      </div>
                    )) : <div className="radio-db-empty">No API history captured yet.</div>}
                  </div>
                </div>
              </div>
            </article>
          </section>
        )}
      </main>
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

function normalizeStationWebsiteHref(raw: string): string {
  const t = raw.trim();
  if (!t) {
    return "#";
  }
  if (/^https?:\/\//i.test(t)) {
    return t;
  }
  return `https://${t}`;
}

function StationWebsiteLink({
  url,
  stopRowClick,
  className = "radio-db-station-url",
}: {
  url: string;
  stopRowClick?: boolean;
  className?: string;
}): JSX.Element {
  const href = normalizeStationWebsiteHref(url);
  const label = url.trim() || href;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      {...(stopRowClick ? { onClick: (event) => event.stopPropagation() } : {})}
    >
      {label}
    </a>
  );
}

function SummaryRow({ label, value }: { label: string; value: string | ReactNode }): JSX.Element {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default App;

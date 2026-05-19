function resolveApiRoot(): string {
  const configured = import.meta.env.VITE_API_ROOT;
  if (configured) {
    return configured.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    const host = window.location.hostname;
    const isLocal = host === "localhost" || host === "127.0.0.1";
    const isRadioHost = host === "radio.public-air.net";
    if (!isLocal && !isRadioHost) {
      return "https://radio.public-air.net/api/v1";
    }
  }
  return "/api/v1";
}

const API_ROOT = resolveApiRoot();

export type ApiPermissionMode = "read-only" | "dry-run" | "execute";

let currentPermissionMode: ApiPermissionMode = "dry-run";

export function setApiPermissionMode(mode: ApiPermissionMode): void {
  currentPermissionMode = mode;
}

export interface StationListItem {
  id: number;
  canonical_name: string;
  country_code: string;
  city: string | null;
  language: string;
  website_url: string | null;
  status: string;
  confidence_score: number;
  updated_at: string;
  genre_count: number;
  people_count: number;
  submission_count: number;
  quality_score: number | null;
  submission_path_quality: string;
  outreach_bucket: string;
}

export interface StationListResponse {
  total: number;
  page: number;
  page_size: number;
  items: StationListItem[];
}

export interface StationProgramDTO {
  id: number;
  name: string;
  description: string | null;
  schedule: string | null;
}

export interface StationSubmissionDTO {
  id: number;
  method: string;
  url: string | null;
  email: string | null;
  requirements: string | null;
  accepts_newcomers: boolean;
  manual_confirmed: boolean;
}

export interface StationContactDTO {
  id: number;
  name: string | null;
  role: string;
  show_name: string | null;
  email: string | null;
  contact_url: string | null;
  notes: string | null;
  confidence: number;
}

export interface StationPersonDTO {
  id: number;
  name: string | null;
  role: string;
  show_name: string | null;
  email: string | null;
  contact_url: string | null;
  linkedin_url: string | null;
  musical_preferences: string | null;
  genre_affinities: string[];
  confidence: number;
}

export interface StationFormDTO {
  id: number;
  url: string;
  page_title: string | null;
  language: string | null;
  form_type: string;
  status: string;
  requires_login: boolean;
  has_captcha: boolean;
  confidence: number;
  last_verified_at: string | null;
}

export interface StationDetailResponse {
  id: number;
  canonical_name: string;
  normalized_name: string;
  country_code: string;
  city: string | null;
  language: string;
  website_url: string | null;
  stream_url: string | null;
  status: string;
  confidence_score: number;
  created_at: string;
  updated_at: string;
  aliases: string[];
  genres: string[];
  programs: StationProgramDTO[];
  submissions: StationSubmissionDTO[];
  contacts: StationContactDTO[];
  people: StationPersonDTO[];
  forms: StationFormDTO[];
  best_submission_route_type: string;
  best_submission_route_url: string | null;
  best_submission_route_email: string | null;
  best_submission_route_confidence: number | null;
  best_submission_route_reason: string | null;
  secondary_submission_routes: Array<Record<string, unknown>>;
}

export interface StationUpdateRequest {
  canonical_name?: string;
  country_code?: string;
  city?: string | null;
  language?: string;
  website_url?: string | null;
  stream_url?: string | null;
  status?: "candidate" | "verified" | "rejected";
  confidence_score?: number;
}

export interface StationControlSubmissionDTO {
  id: number;
  station_id: number;
  method: string;
  url: string | null;
  email: string | null;
  requirements: string | null;
  accepts_newcomers: boolean;
}

export interface StationControlAssessmentDTO {
  id: number;
  station_id: number;
  assessment_kind: string;
  status: string;
  is_real_station: boolean;
  has_real_editorial_surface: boolean;
  accepts_music_submissions: boolean;
  accepts_new_artists: boolean;
  automation_readiness: number;
  risk_score: number;
  notes: string | null;
  evidence: unknown;
  created_at: string;
  updated_at: string;
}

export interface StationControlDetailResponse {
  station_id: number;
  station_name: string;
  status: string;
  confidence_score: number;
  website_url: string | null;
  city: string | null;
  language: string | null;
  stream_url: string | null;
  submissions: StationControlSubmissionDTO[];
  assessments: StationControlAssessmentDTO[];
}

export interface ManualConfirmResponse {
  station_id: number;
  manual_confirmed: boolean;
  manual_confirmed_at: string | null;
}

export interface DeleteStationResponse {
  station_id: number;
  status: string;
}

export interface AgentRunListItem {
  id: number;
  station_id: number;
  station_name: string;
  form_id: number | null;
  mode: string;
  goal: string;
  status: string;
  current_state: string;
  confidence: number;
  requires_approval: boolean;
  blocked_reason: string | null;
  target_url: string | null;
  started_at: string;
  finished_at: string | null;
  step_count: number;
  issue_count: number;
}

export interface AgentRunListResponse {
  total: number;
  items: AgentRunListItem[];
}

export interface AgentStepDTO {
  id: number;
  step_index: number;
  state_before: string;
  state_after: string;
  screenshot_path: string | null;
  dom_snapshot_path: string | null;
  agent_observation: unknown;
  proposed_action: unknown;
  executed_action: unknown;
  execution_result: unknown;
  confidence: number;
  latency_ms: number;
  created_at: string;
}

export interface AgentIssueDTO {
  id: number;
  step_id: number | null;
  issue_type: string;
  severity: string;
  status: string;
  title: string;
  details: string | null;
  payload: unknown;
  created_at: string;
}

export interface AgentAssessmentDTO {
  id: number;
  assessment_kind: string;
  status: string;
  is_real_station: boolean;
  has_real_editorial_surface: boolean;
  accepts_music_submissions: boolean;
  accepts_new_artists: boolean;
  automation_readiness: number;
  risk_score: number;
  notes: string | null;
  evidence: unknown;
  updated_at: string;
}

export interface AgentRunDetailResponse {
  id: number;
  station_id: number;
  station_name: string;
  station_website_url: string | null;
  form_id: number | null;
  mode: string;
  goal: string;
  status: string;
  current_state: string;
  confidence: number;
  requires_approval: boolean;
  blocked_reason: string | null;
  target_url: string | null;
  summary: unknown;
  started_at: string;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  steps: AgentStepDTO[];
  issues: AgentIssueDTO[];
  latest_assessment: AgentAssessmentDTO | null;
}

export interface CountryDiscoverySnapshotResponse {
  state: unknown;
  memory: unknown;
  recent_runs: unknown[];
}

export interface DataControlOverviewResponse {
  generated_at: string;
  monitor: Record<string, unknown>;
  country_discovery: Record<string, unknown>;
  api_history: Array<Record<string, unknown>>;
}

export interface ScanFocusUpdateResponse {
  enabled: boolean;
  market_focus: string;
  updated_by: string;
  updated_at: string;
}

export interface BrowserSessionState {
  id: string;
  station_id: number | null;
  run_id: number | null;
  url: string;
  title: string;
  status: string;
  last_error: string;
  screenshot_path: string | null;
  html_path: string | null;
  action_count: number;
  actions: Array<Record<string, unknown>>;
  timeline: Array<Record<string, unknown>>;
  event_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface BrowserSessionListResponse {
  items: BrowserSessionState[];
}

export interface BrowserSessionCreateRequest {
  start_url?: string | null;
  station_id?: number | null;
  run_id?: number | null;
  label?: string | null;
}

export interface BrowserSessionNavigateRequest {
  url: string;
}

export interface BrowserSessionActionRequest {
  action: "click" | "fill" | "press" | "select" | "goto" | "wait";
  selector?: string | null;
  value?: string | null;
}

export interface ContactRouteDTO {
  kind: string;
  label: string;
  value: string;
}

export interface ContactDraftResponse {
  station_id: number;
  station_name: string;
  locale: string;
  language: string;
  country_code: string;
  city: string | null;
  subject: string;
  body: string;
  locale_hint: string;
  recommended_channel: string;
  available_routes: ContactRouteDTO[];
  evidence_summary: string[];
  permission_mode: ApiPermissionMode;
  template_id?: number | null;
  campaign_id?: number | null;
  draft_id?: number | null;
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ContactTemplateLocaleDTO {
  id: number;
  locale_key: string;
  language_code: string;
  subject_template: string;
  body_template: string;
  created_at: string;
  updated_at: string;
}

export interface ContactTemplateDTO {
  id: number;
  template_key: string;
  name: string;
  description: string | null;
  channel: string;
  variables: string[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
  locales: ContactTemplateLocaleDTO[];
}

export interface ContactTemplateListResponse {
  items: ContactTemplateDTO[];
}

export interface ContactDraftListResponse {
  items: ContactDraftResponse[];
}

export interface ContactSendResponse {
  id: number;
  draft_id: number;
  station_id: number;
  channel: string;
  target_value: string | null;
  mode: string;
  status: string;
  payload: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  outcome_count: number;
}

export interface ContactSendListResponse {
  items: ContactSendResponse[];
}

export interface ContactOutcomeResponse {
  id: number;
  send_id: number;
  outcome_type: string;
  status: string;
  details: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ContactOutcomeListResponse {
  items: ContactOutcomeResponse[];
}

export interface StationGroupListItem {
  id: number;
  name: string;
  description: string | null;
  artist_key: string | null;
  color_hint: string | null;
  is_active: boolean;
  station_count: number;
  created_at: string;
  updated_at: string;
}

export interface StationGroupStationDTO {
  station_id: number;
  canonical_name: string;
  country_code: string;
  city: string | null;
  status: string;
  confidence_score: number;
  website_url: string | null;
  added_at: string;
  note: string | null;
}

export interface StationGroupDetailResponse extends StationGroupListItem {
  stations: StationGroupStationDTO[];
}

export interface StationGroupListResponse {
  items: StationGroupListItem[];
}

function toQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    searchParams.set(key, String(value));
  });
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: {
      "Content-Type": "application/json",
      "X-Radio-DB-Mode": currentPermissionMode,
      ...(init?.headers || {}),
    },
    ...init,
  });

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const body = isJson ? await response.json().catch(() => null) : await response.text().catch(() => "");

  if (!response.ok) {
    const detail =
      typeof body === "string"
        ? body
        : body && typeof body === "object" && "detail" in body
          ? String((body as { detail?: unknown }).detail ?? "Unknown error")
          : `HTTP ${response.status}`;
    throw new Error(detail);
  }

  return body as T;
}

export function getArtifactUrl(path: string): string {
  return `${API_ROOT}/agent/artifact${toQuery({ path })}`;
}

export function listStations(params: {
  q?: string;
  country?: string;
  status?: string;
  has_submission?: "any" | "yes" | "no";
  has_people?: "any" | "yes" | "no";
  outreach?: "" | "verified_submission" | "contact_only";
  min_confidence?: number;
  page?: number;
  page_size?: number;
  sort_by?: "updated_at" | "confidence" | "name";
  sort_order?: "asc" | "desc";
} = {}): Promise<StationListResponse> {
  return requestJson<StationListResponse>(`/stations${toQuery(params)}`);
}

export function getStationDetail(stationId: number): Promise<StationDetailResponse> {
  return requestJson<StationDetailResponse>(`/stations/${stationId}`);
}

export function updateStation(stationId: number, payload: StationUpdateRequest): Promise<StationDetailResponse> {
  return requestJson<StationDetailResponse>(`/stations/${stationId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function getStationControl(stationId: number): Promise<StationControlDetailResponse> {
  return requestJson<StationControlDetailResponse>(`/control/stations/${stationId}`);
}

export function setStationManualConfirm(stationId: number, value: boolean): Promise<ManualConfirmResponse> {
  return requestJson<ManualConfirmResponse>(`/control/stations/${stationId}/manual-confirm`, {
    method: "PATCH",
    body: JSON.stringify({ value }),
  });
}

export function deleteStation(stationId: number): Promise<DeleteStationResponse> {
  return requestJson<DeleteStationResponse>(`/control/stations/${stationId}`, {
    method: "DELETE",
  });
}

export function listAgentRuns(params: { stationId?: number; status?: string; limit?: number; offset?: number } = {}): Promise<AgentRunListResponse> {
  return requestJson<AgentRunListResponse>(
    `/agent/runs${toQuery({ station_id: params.stationId, status: params.status, limit: params.limit, offset: params.offset })}`
  );
}

export function getAgentRunDetail(runId: number): Promise<AgentRunDetailResponse> {
  return requestJson<AgentRunDetailResponse>(`/agent/runs/${runId}`);
}

export function startManualScan(payload: { station_id: number; mode?: string; target_url?: string | null; max_pages?: number; force_rescan?: boolean }): Promise<AgentRunDetailResponse> {
  return requestJson<AgentRunDetailResponse>(`/agent/runs/manual-scan`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getCountryDiscoverySnapshot(limit = 25): Promise<CountryDiscoverySnapshotResponse> {
  return requestJson<CountryDiscoverySnapshotResponse>(`/agent/country-discovery${toQuery({ limit })}`);
}

export function startCountryDiscovery(): Promise<{ started: boolean; message: string }> {
  return requestJson<{ started: boolean; message: string }>(`/agent/country-discovery/start`, {
    method: "POST",
  });
}

export function getDataControlOverview(params: { history_limit?: number; history_hours?: number } = {}): Promise<DataControlOverviewResponse> {
  return requestJson<DataControlOverviewResponse>(`/data-control/overview${toQuery(params)}`);
}

export function updateScanFocus(payload: { enabled: boolean; market_focus: "international" | "dach" | "anglo" | "eu_core" | "top_major" }): Promise<ScanFocusUpdateResponse> {
  return requestJson<ScanFocusUpdateResponse>(`/data-control/scan-focus`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listBrowserSessions(): Promise<BrowserSessionListResponse> {
  return requestJson<BrowserSessionListResponse>(`/browser/sessions`);
}

export function createBrowserSession(payload: BrowserSessionCreateRequest): Promise<BrowserSessionState> {
  return requestJson<BrowserSessionState>(`/browser/sessions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBrowserSession(sessionId: string): Promise<BrowserSessionState> {
  return requestJson<BrowserSessionState>(`/browser/sessions/${sessionId}`);
}

export function navigateBrowserSession(sessionId: string, payload: BrowserSessionNavigateRequest): Promise<BrowserSessionState> {
  return requestJson<BrowserSessionState>(`/browser/sessions/${sessionId}/navigate`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function performBrowserAction(sessionId: string, payload: BrowserSessionActionRequest): Promise<BrowserSessionState> {
  return requestJson<BrowserSessionState>(`/browser/sessions/${sessionId}/action`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function snapshotBrowserSession(sessionId: string): Promise<BrowserSessionState> {
  return requestJson<BrowserSessionState>(`/browser/sessions/${sessionId}/snapshot`, {
    method: "POST",
  });
}

export function closeBrowserSession(sessionId: string): Promise<{ deleted: true; session_id: string }> {
  return requestJson<{ deleted: true; session_id: string }>(`/browser/sessions/${sessionId}`, {
    method: "DELETE",
  });
}

export function getStationContactDraft(stationId: number): Promise<ContactDraftResponse> {
  return requestJson<ContactDraftResponse>(`/contact-center/stations/${stationId}/draft`);
}

export function listContactTemplates(): Promise<ContactTemplateListResponse> {
  return requestJson<ContactTemplateListResponse>(`/contact-center/templates`);
}

export function listStationContactDrafts(stationId: number): Promise<ContactDraftListResponse> {
  return requestJson<ContactDraftListResponse>(`/contact-center/stations/${stationId}/drafts`);
}

export function createStationContactDraft(
  stationId: number,
  payload: { template_id?: number | null; campaign_id?: number | null; subject?: string | null; body?: string | null },
): Promise<ContactDraftResponse> {
  return requestJson<ContactDraftResponse>(`/contact-center/stations/${stationId}/drafts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewStationContactDraft(
  draftId: number,
  payload: { subject?: string | null; body?: string | null },
): Promise<ContactDraftResponse> {
  return requestJson<ContactDraftResponse>(`/contact-center/drafts/${draftId}/preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createContactSend(
  draftId: number,
  payload: { mode?: "dry-run" | "execute"; channel?: string | null; target_value?: string | null } = {},
): Promise<ContactSendResponse> {
  return requestJson<ContactSendResponse>(`/contact-center/drafts/${draftId}/send`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listDraftSends(draftId: number): Promise<ContactSendListResponse> {
  return requestJson<ContactSendListResponse>(`/contact-center/drafts/${draftId}/sends`);
}

export function listSendOutcomes(sendId: number): Promise<ContactOutcomeListResponse> {
  return requestJson<ContactOutcomeListResponse>(`/contact-center/sends/${sendId}/outcomes`);
}

export function listStationGroups(): Promise<StationGroupListResponse> {
  return requestJson<StationGroupListResponse>(`/station-groups`);
}

export function getStationGroup(groupId: number): Promise<StationGroupDetailResponse> {
  return requestJson<StationGroupDetailResponse>(`/station-groups/${groupId}`);
}

export function createStationGroup(payload: {
  name: string;
  description?: string | null;
  artist_key?: string | null;
  color_hint?: string | null;
}): Promise<StationGroupDetailResponse> {
  return requestJson<StationGroupDetailResponse>(`/station-groups`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function addStationsToGroup(groupId: number, payload: {
  station_ids: number[];
  note?: string | null;
}): Promise<Array<{ group_id: number; station_id: number; created: boolean }>> {
  return requestJson<Array<{ group_id: number; station_id: number; created: boolean }>>(`/station-groups/${groupId}/stations`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listStationMemberships(stationId: number): Promise<StationGroupListResponse> {
  return requestJson<StationGroupListResponse>(`/station-groups/stations/${stationId}`);
}

export function addStationToGroup(stationId: number, groupId: number): Promise<{ group_id: number; station_id: number; created: boolean }> {
  return requestJson<{ group_id: number; station_id: number; created: boolean }>(`/station-groups/stations/${stationId}/${groupId}`, {
    method: "POST",
  });
}

export function removeStationFromGroup(stationId: number, groupId: number): Promise<{ group_id: number; station_id: number; created: boolean }> {
  return requestJson<{ group_id: number; station_id: number; created: boolean }>(`/station-groups/stations/${stationId}/${groupId}`, {
    method: "DELETE",
  });
}

export interface OutreachCampaignDTO {
  id: number;
  name: string;
  artist_name: string;
  song_title: string;
  release_date: string | null;
  song_language: string | null;
  pitch_text: string;
  reference_template: string;
  operator_notes: string | null;
  press_release_url: string | null;
  tracking_code: string | null;
  submission_defaults: Record<string, string>;
  artist_profile: Record<string, string>;
  release_assets: Record<string, string>;
  press_release_tracking_url: string | null;
  press_release_short_tracking_url: string | null;
  press_release_click_count: number;
  press_release_last_clicked_at: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface OutreachCampaignListResponse {
  items: OutreachCampaignDTO[];
}

export interface OutreachCampaignMonitorSummary {
  total: number;
  not_started: number;
  drafted: number;
  sent: number;
  clicked: number;
  failed: number;
  blocked: number;
  simulated: number;
  unknown_clicks: number;
}

export interface OutreachCampaignMonitorItem {
  station_id: number;
  station_name: string;
  country_code: string;
  language: string;
  draft_id: number | null;
  draft_status: string | null;
  send_id: number | null;
  send_status: string | null;
  send_channel: string | null;
  send_target: string | null;
  send_count: number;
  last_sent_at: string | null;
  click_count: number;
  last_clicked_at: string | null;
  tracking_url: string | null;
  monitor_status: string;
}

export interface OutreachCampaignMonitorResponse {
  summary: OutreachCampaignMonitorSummary;
  items: OutreachCampaignMonitorItem[];
}

export function listOutreachCampaigns(activeOnly = false): Promise<OutreachCampaignListResponse> {
  return requestJson<OutreachCampaignListResponse>(`/outreach-campaigns${toQuery({ active_only: activeOnly || undefined })}`);
}

export function createOutreachCampaign(payload: {
  name: string;
  artist_name: string;
  song_title: string;
  release_date?: string | null;
  song_language?: string | null;
  pitch_text?: string;
  reference_template?: string;
  operator_notes?: string | null;
  press_release_url?: string | null;
  tracking_code?: string | null;
  submission_defaults?: Record<string, string>;
  artist_profile?: Record<string, string>;
  release_assets?: Record<string, string>;
}): Promise<OutreachCampaignDTO> {
  return requestJson<OutreachCampaignDTO>(`/outreach-campaigns`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function patchOutreachCampaign(
  campaignId: number,
  payload: {
    name?: string | null;
    artist_name?: string | null;
    song_title?: string | null;
    release_date?: string | null;
    song_language?: string | null;
    pitch_text?: string | null;
    reference_template?: string | null;
    operator_notes?: string | null;
    press_release_url?: string | null;
    tracking_code?: string | null;
    submission_defaults?: Record<string, string> | null;
    artist_profile?: Record<string, string> | null;
    release_assets?: Record<string, string> | null;
    is_active?: boolean | null;
  },
): Promise<OutreachCampaignDTO> {
  return requestJson<OutreachCampaignDTO>(`/outreach-campaigns/${campaignId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function archiveOutreachCampaign(campaignId: number): Promise<OutreachCampaignDTO> {
  return requestJson<OutreachCampaignDTO>(`/outreach-campaigns/${campaignId}`, {
    method: "DELETE",
  });
}

export function generateOutreachEmail(
  campaignId: number,
  payload: { station_id: number },
): Promise<{ subject: string; body: string; subject_en: string; body_en: string }> {
  return requestJson<{ subject: string; body: string; subject_en: string; body_en: string }>(
    `/outreach-campaigns/${campaignId}/generate-email`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getOutreachCampaignMonitor(campaignId: number): Promise<OutreachCampaignMonitorResponse> {
  return requestJson<OutreachCampaignMonitorResponse>(`/outreach-campaigns/${campaignId}/monitor`);
}

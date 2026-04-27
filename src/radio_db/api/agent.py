from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.db import SessionLocal
from radio_db.models.entities import (
    Station,
    StationSubmissionAssessment,
    SubmissionAgentIssue,
    SubmissionAgentRun,
    SubmissionAgentStep,
)
from radio_db.services.forms_agent import start_manual_scan_run
from radio_db.services.pipeline import load_country_discovery_intelligence

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

_AGENT_LOCK = threading.Lock()
AGENT_MANUAL_STATE_PATH = Path(".radio_db_state/agent_manual_state.json")


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_json(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


class AgentRunListItem(BaseModel):
    id: int
    station_id: int
    station_name: str
    form_id: int | None = None
    mode: str
    goal: str
    status: str
    current_state: str
    confidence: float
    requires_approval: bool
    blocked_reason: str | None = None
    target_url: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    step_count: int = 0
    issue_count: int = 0


class AgentRunListResponse(BaseModel):
    total: int
    items: list[AgentRunListItem]


class AgentStepDTO(BaseModel):
    id: int
    step_index: int
    state_before: str
    state_after: str
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    agent_observation: object = Field(default_factory=dict)
    proposed_action: object = Field(default_factory=dict)
    executed_action: object = Field(default_factory=dict)
    execution_result: object = Field(default_factory=dict)
    confidence: float
    latency_ms: int
    created_at: datetime


class AgentIssueDTO(BaseModel):
    id: int
    step_id: int | None = None
    issue_type: str
    severity: str
    status: str
    title: str
    details: str | None = None
    payload: object = Field(default_factory=dict)
    created_at: datetime


class AgentAssessmentDTO(BaseModel):
    id: int
    assessment_kind: str
    status: str
    is_real_station: bool
    has_real_editorial_surface: bool
    accepts_music_submissions: bool
    accepts_new_artists: bool
    automation_readiness: float
    risk_score: float
    notes: str | None = None
    evidence: object = Field(default_factory=dict)
    updated_at: datetime


class AgentRunDetailResponse(BaseModel):
    id: int
    station_id: int
    station_name: str
    station_website_url: str | None = None
    form_id: int | None = None
    mode: str
    goal: str
    status: str
    current_state: str
    confidence: float
    requires_approval: bool
    blocked_reason: str | None = None
    target_url: str | None = None
    summary: object = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    steps: list[AgentStepDTO] = Field(default_factory=list)
    issues: list[AgentIssueDTO] = Field(default_factory=list)
    latest_assessment: AgentAssessmentDTO | None = None


class ManualScanRequest(BaseModel):
    station_id: int = Field(ge=1)
    mode: str = Field(default="scan", min_length=1, max_length=32)
    target_url: str | None = Field(default=None, max_length=1024)
    max_pages: int = Field(default=1, ge=1, le=10)
    force_rescan: bool = False


class AgentRunUpdateRequest(BaseModel):
    status: Literal["running", "blocked", "completed", "failed"] | None = None
    current_state: str | None = Field(default=None, min_length=1, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    requires_approval: bool | None = None
    blocked_reason: str | None = Field(default=None, max_length=2000)
    target_url: str | None = Field(default=None, max_length=1024)


class CountryDiscoverySnapshotResponse(BaseModel):
    state: object = Field(default_factory=dict)
    memory: object = Field(default_factory=dict)
    recent_runs: list[object] = Field(default_factory=list)


class CountryDiscoveryStartResponse(BaseModel):
    started: bool
    message: str


def _station_name(db: Session, station_id: int) -> str:
    station_name = db.scalar(select(Station.canonical_name).where(Station.id == station_id))
    if station_name is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return str(station_name)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _agent_manual_state() -> dict:
    default = {
        "is_running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "last_exit_code": None,
        "last_error": "",
        "last_output": "",
    }
    payload = _load_json(AGENT_MANUAL_STATE_PATH)
    if not payload:
        return default
    out = dict(default)
    out.update(payload)
    return out


def _save_agent_manual_state(state: dict) -> None:
    _save_json(AGENT_MANUAL_STATE_PATH, state)


def _start_country_discovery_background() -> bool:
    with _AGENT_LOCK:
        state = _agent_manual_state()
        if state.get("is_running"):
            return False
        state.update(
            {
                "is_running": True,
                "last_started_at": datetime.now(UTC).isoformat(),
                "last_error": "",
            }
        )
        _save_agent_manual_state(state)

    repo_root = Path(__file__).resolve().parents[3]

    def _worker() -> None:
        try:
            result = subprocess.run(
                [
                    "bash",
                    "-lc",
                    f"cd {repo_root!s} && ./scripts/run_country_discovery_cycle.sh",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            output = (result.stdout or "").strip()
            error = (result.stderr or "").strip()
            state = _agent_manual_state()
            state.update(
                {
                    "is_running": False,
                    "last_finished_at": datetime.now(UTC).isoformat(),
                    "last_exit_code": int(result.returncode),
                    "last_output": (output[-4000:] if output else ""),
                    "last_error": (error[-2000:] if error else ""),
                }
            )
            _save_agent_manual_state(state)
        except Exception as exc:
            state = _agent_manual_state()
            state.update(
                {
                    "is_running": False,
                    "last_finished_at": datetime.now(UTC).isoformat(),
                    "last_exit_code": 1,
                    "last_error": str(exc),
                }
            )
            _save_agent_manual_state(state)

    thread = threading.Thread(target=_worker, name="radio-db-country-discovery", daemon=True)
    thread.start()
    return True


@router.get("/artifact")
def get_artifact(path: str = Query(...)) -> FileResponse:
    resolved = Path(path).resolve()
    state_root = Path(".radio_db_state").resolve()
    snapshot_root = Path(settings.browser_snapshot_dir).resolve()
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="artifact_not_found")
    if resolved != state_root and state_root not in resolved.parents and snapshot_root not in resolved.parents:
        raise HTTPException(status_code=404, detail="artifact_not_found")
    return FileResponse(resolved)


@router.get("/country-discovery", response_model=CountryDiscoverySnapshotResponse)
def country_discovery_snapshot(limit: int = Query(default=25, ge=1, le=100)) -> CountryDiscoverySnapshotResponse:
    payload = load_country_discovery_intelligence(limit=limit)
    return CountryDiscoverySnapshotResponse(
        state=payload.get("state", {}),
        memory=payload.get("memory", {}),
        recent_runs=payload.get("recent_runs", []),
    )


@router.post("/country-discovery/start", response_model=CountryDiscoveryStartResponse)
def start_country_discovery() -> CountryDiscoveryStartResponse:
    started = _start_country_discovery_background()
    return CountryDiscoveryStartResponse(
        started=started,
        message="started" if started else "already_running",
    )


def _serialize_run(run: SubmissionAgentRun, station_name: str) -> AgentRunListItem:
    return AgentRunListItem(
        id=run.id,
        station_id=run.station_id,
        station_name=station_name,
        form_id=run.form_id,
        mode=run.mode,
        goal=run.goal,
        status=run.status,
        current_state=run.current_state,
        confidence=float(run.confidence or 0.0),
        requires_approval=bool(run.requires_approval),
        blocked_reason=run.blocked_reason,
        target_url=run.target_url,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _serialize_step(step: SubmissionAgentStep) -> AgentStepDTO:
    return AgentStepDTO(
        id=step.id,
        step_index=step.step_index,
        state_before=step.state_before,
        state_after=step.state_after,
        screenshot_path=step.screenshot_path,
        dom_snapshot_path=step.dom_snapshot_path,
        agent_observation=_parse_json(step.agent_observation_json, {}),
        proposed_action=_parse_json(step.proposed_action_json, {}),
        executed_action=_parse_json(step.executed_action_json, {}),
        execution_result=_parse_json(step.execution_result_json, {}),
        confidence=float(step.confidence or 0.0),
        latency_ms=int(step.latency_ms or 0),
        created_at=step.created_at,
    )


def _serialize_issue(issue: SubmissionAgentIssue) -> AgentIssueDTO:
    return AgentIssueDTO(
        id=issue.id,
        step_id=issue.step_id,
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        title=issue.title,
        details=issue.details,
        payload=_parse_json(issue.payload_json, {}),
        created_at=issue.created_at,
    )


def _serialize_assessment(assessment: StationSubmissionAssessment) -> AgentAssessmentDTO:
    return AgentAssessmentDTO(
        id=assessment.id,
        assessment_kind=assessment.assessment_kind,
        status=assessment.status,
        is_real_station=bool(assessment.is_real_station),
        has_real_editorial_surface=bool(assessment.has_real_editorial_surface),
        accepts_music_submissions=bool(assessment.accepts_music_submissions),
        accepts_new_artists=bool(assessment.accepts_new_artists),
        automation_readiness=float(assessment.automation_readiness or 0.0),
        risk_score=float(assessment.risk_score or 0.0),
        notes=assessment.notes,
        evidence=_parse_json(assessment.evidence_json, {}),
        updated_at=assessment.updated_at,
    )


@router.get("/runs", response_model=AgentRunListResponse)
def list_runs(
    station_id: int | None = Query(default=None, ge=1),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
) -> AgentRunListResponse:
    stmt: Select[tuple[SubmissionAgentRun, str]] = select(SubmissionAgentRun, Station.canonical_name).join(
        Station, Station.id == SubmissionAgentRun.station_id
    )
    if station_id is not None:
        stmt = stmt.where(SubmissionAgentRun.station_id == station_id)
    if status.strip():
        stmt = stmt.where(func.lower(SubmissionAgentRun.status) == status.strip().lower())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    runs = db.execute(
        stmt.order_by(SubmissionAgentRun.created_at.desc(), SubmissionAgentRun.id.desc()).offset(offset).limit(limit)
    ).all()

    run_ids = [int(run.id) for run, _ in runs]
    step_counts: dict[int, int] = {}
    issue_counts: dict[int, int] = {}
    if run_ids:
        step_counts = {
            int(run_id): int(count)
            for run_id, count in db.execute(
                select(SubmissionAgentStep.run_id, func.count(SubmissionAgentStep.id))
                .where(SubmissionAgentStep.run_id.in_(run_ids))
                .group_by(SubmissionAgentStep.run_id)
            ).all()
        }
        issue_counts = {
            int(run_id): int(count)
            for run_id, count in db.execute(
                select(SubmissionAgentIssue.run_id, func.count(SubmissionAgentIssue.id))
                .where(SubmissionAgentIssue.run_id.in_(run_ids))
                .group_by(SubmissionAgentIssue.run_id)
            ).all()
        }

    items: list[AgentRunListItem] = []
    for run, station_name in runs:
        item = _serialize_run(run, station_name)
        items.append(
            item.model_copy(
                update={
                    "step_count": step_counts.get(run.id, 0),
                    "issue_count": issue_counts.get(run.id, 0),
                }
            )
        )

    return AgentRunListResponse(total=int(total), items=items)


@router.get("/runs/{run_id}", response_model=AgentRunDetailResponse)
def get_run(run_id: int, db: Session = Depends(_get_db)) -> AgentRunDetailResponse:
    run = db.scalar(select(SubmissionAgentRun).where(SubmissionAgentRun.id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")

    station_name = _station_name(db, run.station_id)
    station = db.scalar(select(Station).where(Station.id == run.station_id))
    latest_assessment = db.scalar(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == run.station_id)
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )

    return AgentRunDetailResponse(
        id=run.id,
        station_id=run.station_id,
        station_name=station_name,
        station_website_url=station.website_url if station is not None else None,
        form_id=run.form_id,
        mode=run.mode,
        goal=run.goal,
        status=run.status,
        current_state=run.current_state,
        confidence=float(run.confidence or 0.0),
        requires_approval=bool(run.requires_approval),
        blocked_reason=run.blocked_reason,
        target_url=run.target_url,
        summary=_parse_json(run.summary_json, {}),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        steps=[_serialize_step(step) for step in run.steps],
        issues=[_serialize_issue(issue) for issue in run.issues],
        latest_assessment=_serialize_assessment(latest_assessment) if latest_assessment is not None else None,
    )


@router.post("/runs/manual-scan", response_model=AgentRunDetailResponse)
def create_manual_scan(request: ManualScanRequest, db: Session = Depends(_get_db)) -> AgentRunDetailResponse:
    _station_name(db, request.station_id)
    run = start_manual_scan_run(
        session=db,
        station_id=request.station_id,
        mode=request.mode,
        target_url=request.target_url,
        max_pages=request.max_pages,
        force_rescan=request.force_rescan,
    )
    return get_run(run.id, db)


@router.patch("/runs/{run_id}", response_model=AgentRunDetailResponse)
def update_run(run_id: int, request: AgentRunUpdateRequest, db: Session = Depends(_get_db)) -> AgentRunDetailResponse:
    run = db.scalar(select(SubmissionAgentRun).where(SubmissionAgentRun.id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")

    if request.status is not None:
        run.status = request.status
    if request.current_state is not None:
        run.current_state = request.current_state
    if request.confidence is not None:
        run.confidence = request.confidence
    if request.requires_approval is not None:
        run.requires_approval = request.requires_approval
    if request.blocked_reason is not None:
        run.blocked_reason = request.blocked_reason
    if request.target_url is not None:
        run.target_url = request.target_url

    if run.status in {"completed", "failed"} and run.finished_at is None:
        run.finished_at = datetime.utcnow()
    if run.status == "running":
        run.finished_at = None

    db.commit()
    return get_run(run_id, db)
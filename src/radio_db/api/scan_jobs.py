from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from radio_db.api.common import PermissionMode, require_permission_mode
from radio_db.db import SessionLocal
from radio_db.models.entities import ScanJob
from radio_db.services.scan_jobs import SUPPORTED_JOB_TYPES, enqueue_scan_job, scan_job_overview

router = APIRouter(prefix="/api/v1/scan-jobs", tags=["scan-jobs"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ScanJobCreateRequest(BaseModel):
    job_type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=1000)
    requested_by: str = Field(default="api", min_length=1, max_length=255)


class ScanJobDTO(BaseModel):
    id: int
    job_type: str
    status: str
    priority: int
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ScanJobOverviewResponse(BaseModel):
    supported_job_types: list[str] = Field(default_factory=list)
    by_status: dict[str, int] = Field(default_factory=dict)
    latest: list[dict[str, Any]] = Field(default_factory=list)


def _json_loads(value: str | None) -> dict[str, Any]:
    import json

    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _dto(row: ScanJob) -> ScanJobDTO:
    return ScanJobDTO(
        id=row.id,
        job_type=row.job_type,
        status=row.status,
        priority=row.priority,
        payload=_json_loads(row.payload_json),
        requested_by=row.requested_by,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        heartbeat_at=row.heartbeat_at,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=ScanJobOverviewResponse)
def list_scan_jobs(db: Session = Depends(_get_db)) -> ScanJobOverviewResponse:
    overview = scan_job_overview(db)
    return ScanJobOverviewResponse(
        supported_job_types=sorted(SUPPORTED_JOB_TYPES),
        by_status=overview["by_status"],
        latest=overview["latest"],
    )


@router.post("", response_model=ScanJobDTO)
def create_scan_job(
    request: ScanJobCreateRequest,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> ScanJobDTO:
    if request.job_type not in SUPPORTED_JOB_TYPES:
        raise HTTPException(status_code=400, detail="unsupported_job_type")
    try:
        row = enqueue_scan_job(
            session=db,
            job_type=request.job_type,
            payload=request.payload,
            priority=request.priority,
            requested_by=request.requested_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _dto(row)


@router.get("/{job_id}", response_model=ScanJobDTO)
def get_scan_job(job_id: int, db: Session = Depends(_get_db)) -> ScanJobDTO:
    row = db.get(ScanJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scan_job_not_found")
    return _dto(row)

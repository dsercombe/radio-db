from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.api.common import require_permission_mode
from radio_db.db import SessionLocal
from radio_db.models.entities import (
    FormStatus,
    FormType,
    Station,
    StationStatus,
    StationSubmissionAssessment,
    StationContact,
    StationPerson,
    SubmissionChannel,
    SubmissionMethod,
)

router = APIRouter(prefix="/api/v1/control", tags=["control"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _enum_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    return str(value or "")


def _parse_json(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        import json

        return json.loads(raw)
    except Exception:
        return fallback


class StationControlSubmissionDTO(BaseModel):
    id: int
    station_id: int
    method: str
    url: str | None = None
    email: str | None = None
    requirements: str | None = None
    accepts_newcomers: bool


class StationControlAssessmentDTO(BaseModel):
    id: int
    station_id: int
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
    created_at: datetime
    updated_at: datetime


class StationControlDetailResponse(BaseModel):
    station_id: int
    station_name: str
    status: str
    confidence_score: float
    website_url: str | None = None
    city: str | None = None
    language: str | None = None
    stream_url: str | None = None
    submissions: list[StationControlSubmissionDTO] = Field(default_factory=list)
    assessments: list[StationControlAssessmentDTO] = Field(default_factory=list)


class ManualConfirmRequest(BaseModel):
    value: bool = True


class ManualConfirmResponse(BaseModel):
    station_id: int
    manual_confirmed: bool
    manual_confirmed_at: datetime | None = None


class DeleteStationResponse(BaseModel):
    station_id: int
    status: str


class SubmissionChannelUpsertRequest(BaseModel):
    method: Literal["form", "email", "portal", "unknown"] | None = None
    url: str | None = Field(default=None, max_length=1024)
    email: str | None = Field(default=None, max_length=320)
    requirements: str | None = None
    accepts_newcomers: bool = False


class SubmissionChannelUpdateRequest(BaseModel):
    method: Literal["form", "email", "portal", "unknown"] | None = None
    url: str | None = Field(default=None, max_length=1024)
    email: str | None = Field(default=None, max_length=320)
    requirements: str | None = None
    accepts_newcomers: bool | None = None


class AssessmentUpsertRequest(BaseModel):
    assessment_kind: str = Field(default="manual_scan", min_length=1, max_length=32)
    status: str = Field(default="active", min_length=1, max_length=32)
    is_real_station: bool = False
    has_real_editorial_surface: bool = False
    accepts_music_submissions: bool = False
    accepts_new_artists: bool = False
    automation_readiness: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_score: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: str | None = None
    evidence: object = Field(default_factory=dict)


class AssessmentUpdateRequest(BaseModel):
    assessment_kind: str | None = Field(default=None, min_length=1, max_length=32)
    status: str | None = Field(default=None, min_length=1, max_length=32)
    is_real_station: bool | None = None
    has_real_editorial_surface: bool | None = None
    accepts_music_submissions: bool | None = None
    accepts_new_artists: bool | None = None
    automation_readiness: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: str | None = None
    evidence: object | None = None


def _station_name(db: Session, station_id: int) -> str:
    station_name = db.scalar(select(Station.canonical_name).where(Station.id == station_id))
    if station_name is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return str(station_name)


def _serialize_submission(submission: SubmissionChannel) -> StationControlSubmissionDTO:
    return StationControlSubmissionDTO(
        id=submission.id,
        station_id=submission.station_id,
        method=_enum_value(submission.method),
        url=submission.url,
        email=submission.email,
        requirements=submission.requirements,
        accepts_newcomers=bool(submission.accepts_newcomers),
    )


def _serialize_assessment(assessment: StationSubmissionAssessment) -> StationControlAssessmentDTO:
    return StationControlAssessmentDTO(
        id=assessment.id,
        station_id=assessment.station_id,
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
        created_at=assessment.created_at,
        updated_at=assessment.updated_at,
    )


@router.get("/stations/{station_id}", response_model=StationControlDetailResponse)
def get_station_control(station_id: int, db: Session = Depends(_get_db)) -> StationControlDetailResponse:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    submissions = db.scalars(
        select(SubmissionChannel).where(SubmissionChannel.station_id == station_id).order_by(SubmissionChannel.id.desc())
    ).all()
    assessments = db.scalars(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == station_id)
        .order_by(StationSubmissionAssessment.updated_at.desc(), StationSubmissionAssessment.id.desc())
    ).all()

    return StationControlDetailResponse(
        station_id=station.id,
        station_name=station.canonical_name,
        status=_enum_value(station.status),
        confidence_score=float(station.confidence_score or 0.0),
        website_url=station.website_url,
        city=station.city,
        language=station.language,
        stream_url=station.stream_url,
        submissions=[_serialize_submission(item) for item in submissions],
        assessments=[_serialize_assessment(item) for item in assessments],
    )


@router.post("/stations/{station_id}/submissions", response_model=StationControlSubmissionDTO)
def upsert_submission_channel(
    station_id: int,
    request: SubmissionChannelUpsertRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationControlSubmissionDTO:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    normalized_email = (request.email or "").strip().lower() or None
    normalized_url = (request.url or "").strip() or None
    existing = db.scalar(
        select(SubmissionChannel).where(
            SubmissionChannel.station_id == station_id,
            SubmissionChannel.email == normalized_email,
            SubmissionChannel.url == normalized_url,
        )
    )
    method_value = SubmissionMethod(request.method) if request.method is not None else None
    # Determine method: explicit method takes precedence, else infer from email/url
    inferred_method = SubmissionMethod.EMAIL if normalized_email else SubmissionMethod.FORM
    final_method = method_value or inferred_method
    
    # For EMAIL method, url should be None; for FORM/UNKNOWN, url should have the form URL
    final_url = None if final_method == SubmissionMethod.EMAIL else normalized_url
    
    if existing is None:
        existing = SubmissionChannel(
            station_id=station_id,
            method=final_method,
            url=final_url,
            email=normalized_email,
            requirements=request.requirements,
            accepts_newcomers=request.accepts_newcomers,
        )
        db.add(existing)
    else:
        if method_value is not None:
            existing.method = method_value
            # When method changes, sync url accordingly
            existing.url = None if method_value == SubmissionMethod.EMAIL else normalized_url
        if request.url is not None:
            existing.url = normalized_url if existing.method != SubmissionMethod.EMAIL else None
        if request.email is not None:
            existing.email = normalized_email
        if request.requirements is not None:
            existing.requirements = request.requirements
        existing.accepts_newcomers = request.accepts_newcomers or existing.accepts_newcomers

    db.commit()
    db.refresh(existing)
    return _serialize_submission(existing)


@router.patch("/submissions/{submission_id}", response_model=StationControlSubmissionDTO)
def update_submission_channel(
    submission_id: int,
    request: SubmissionChannelUpdateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationControlSubmissionDTO:
    submission = db.scalar(select(SubmissionChannel).where(SubmissionChannel.id == submission_id))
    if submission is None:
        raise HTTPException(status_code=404, detail="submission_not_found")

    if request.method is not None:
        submission.method = SubmissionMethod(request.method)
    if request.url is not None:
        submission.url = request.url.strip() or None
    if request.email is not None:
        submission.email = request.email.strip().lower() or None
    if request.requirements is not None:
        submission.requirements = request.requirements
    if request.accepts_newcomers is not None:
        submission.accepts_newcomers = request.accepts_newcomers

    db.commit()
    db.refresh(submission)
    return _serialize_submission(submission)


@router.delete("/submissions/{submission_id}")
def delete_submission_channel(
    submission_id: int,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> dict:
    submission = db.scalar(select(SubmissionChannel).where(SubmissionChannel.id == submission_id))
    if submission is None:
        raise HTTPException(status_code=404, detail="submission_not_found")

    station_id = submission.station_id
    db.delete(submission)
    db.commit()
    return {"deleted": True, "station_id": station_id, "submission_id": submission_id}


@router.patch("/stations/{station_id}/manual-confirm", response_model=ManualConfirmResponse)
def set_station_manual_confirm(
    station_id: int,
    request: ManualConfirmRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> ManualConfirmResponse:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    station.manual_confirmed = request.value
    station.manual_confirmed_at = datetime.now(UTC).replace(tzinfo=None) if request.value else None
    db.commit()
    return ManualConfirmResponse(
        station_id=station.id,
        manual_confirmed=bool(station.manual_confirmed),
        manual_confirmed_at=station.manual_confirmed_at,
    )


@router.delete("/stations/{station_id}", response_model=DeleteStationResponse)
def delete_station(
    station_id: int,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> DeleteStationResponse:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    station.status = StationStatus.REJECTED
    station.manual_confirmed = False
    station.manual_confirmed_at = None
    db.commit()
    return DeleteStationResponse(station_id=station.id, status=str(getattr(station.status, "value", station.status)))


@router.patch("/entries/{source}/{entry_id}/manual-confirm")
def set_entry_manual_confirm(
    source: str,
    entry_id: int,
    request: ManualConfirmRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> dict:
    source_key = (source or "").strip().lower()
    model_map: dict[str, type[StationContact] | type[StationPerson] | type[SubmissionChannel]] = {
        "submission": SubmissionChannel,
        "contact": StationContact,
        "people": StationPerson,
    }
    model = model_map.get(source_key)
    if model is None:
        raise HTTPException(status_code=404, detail="entry_source_not_found")

    entry = db.scalar(select(model).where(model.id == entry_id))
    if entry is None:
        raise HTTPException(status_code=404, detail="entry_not_found")

    entry.manual_confirmed = request.value
    entry.manual_confirmed_at = datetime.now(UTC).replace(tzinfo=None) if request.value else None
    db.commit()
    return {
        "entry_id": entry_id,
        "source": source_key,
        "manual_confirmed": bool(entry.manual_confirmed),
        "manual_confirmed_at": entry.manual_confirmed_at,
    }


@router.post("/stations/{station_id}/assessments", response_model=StationControlAssessmentDTO)
def upsert_station_assessment(
    station_id: int,
    request: AssessmentUpsertRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationControlAssessmentDTO:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    assessment = db.scalar(
        select(StationSubmissionAssessment).where(
            StationSubmissionAssessment.station_id == station_id,
            StationSubmissionAssessment.assessment_kind == request.assessment_kind,
        )
    )
    if assessment is None:
        assessment = StationSubmissionAssessment(
            station_id=station_id,
            assessment_kind=request.assessment_kind,
        )
        db.add(assessment)

    assessment.status = request.status
    assessment.is_real_station = request.is_real_station
    assessment.has_real_editorial_surface = request.has_real_editorial_surface
    assessment.accepts_music_submissions = request.accepts_music_submissions
    assessment.accepts_new_artists = request.accepts_new_artists
    assessment.automation_readiness = request.automation_readiness
    assessment.risk_score = request.risk_score
    assessment.notes = request.notes
    import json

    assessment.evidence_json = json.dumps(request.evidence, ensure_ascii=False)
    assessment.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(assessment)
    return _serialize_assessment(assessment)


@router.patch("/assessments/{assessment_id}", response_model=StationControlAssessmentDTO)
def update_station_assessment(
    assessment_id: int,
    request: AssessmentUpdateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationControlAssessmentDTO:
    assessment = db.scalar(select(StationSubmissionAssessment).where(StationSubmissionAssessment.id == assessment_id))
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment_not_found")

    if request.assessment_kind is not None:
        assessment.assessment_kind = request.assessment_kind
    if request.status is not None:
        assessment.status = request.status
    if request.is_real_station is not None:
        assessment.is_real_station = request.is_real_station
    if request.has_real_editorial_surface is not None:
        assessment.has_real_editorial_surface = request.has_real_editorial_surface
    if request.accepts_music_submissions is not None:
        assessment.accepts_music_submissions = request.accepts_music_submissions
    if request.accepts_new_artists is not None:
        assessment.accepts_new_artists = request.accepts_new_artists
    if request.automation_readiness is not None:
        assessment.automation_readiness = request.automation_readiness
    if request.risk_score is not None:
        assessment.risk_score = request.risk_score
    if request.notes is not None:
        assessment.notes = request.notes
    if request.evidence is not None:
        import json

        assessment.evidence_json = json.dumps(request.evidence, ensure_ascii=False)
    assessment.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(assessment)
    return _serialize_assessment(assessment)


@router.delete("/assessments/{assessment_id}")
def delete_station_assessment(
    assessment_id: int,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> dict:
    assessment = db.scalar(select(StationSubmissionAssessment).where(StationSubmissionAssessment.id == assessment_id))
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment_not_found")

    station_id = assessment.station_id
    db.delete(assessment)
    db.commit()
    return {"deleted": True, "station_id": station_id, "assessment_id": assessment_id}

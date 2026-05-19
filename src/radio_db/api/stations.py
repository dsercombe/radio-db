from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Float, Select, cast, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from radio_db.api.common import require_permission_mode
from radio_db.db import SessionLocal, engine
from radio_db.models.entities import (
    FormStatus,
    FormType,
    Station,
    StationAlias,
    StationContact,
    StationGenre,
    StationPerson,
    StationProgram,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionChannel,
    SubmissionMethod,
    SubmissionForm,
)

router = APIRouter(prefix="/api/v1/stations", tags=["stations"])


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
        return json.loads(raw)
    except Exception:
        return fallback


def _quality_evidence_text(key: str):
    if engine.dialect.name == "postgresql":
        return func.substring(StationSubmissionAssessment.evidence_json, f'"{key}"\\s*:\\s*"([^"]*)"')
    return func.json_extract(StationSubmissionAssessment.evidence_json, f"$.{key}")


def _quality_evidence_number(key: str):
    if engine.dialect.name == "postgresql":
        return func.substring(
            StationSubmissionAssessment.evidence_json,
            f'"{key}"\\s*:\\s*(-?[0-9]+(?:\\.[0-9]+)?)',
        )
    return func.json_extract(StationSubmissionAssessment.evidence_json, f"$.{key}")


VERIFIED_SUBMISSION_ROUTE_TYPES = {
    "direct_music_form",
    "gated_music_form",
    "explicit_submission_email",
    "music_director_contact",
}
CONTACT_ONLY_ROUTE_TYPES = {"contact_email_only", "contact_form_only"}
VERIFIED_FORM_TYPES = {FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER}


def _has_verified_submission_condition(station_id_column=Station.id):
    return or_(
        Station.best_submission_route_type.in_(list(VERIFIED_SUBMISSION_ROUTE_TYPES)),
        exists(
            select(SubmissionForm.id).where(
                SubmissionForm.station_id == station_id_column,
                SubmissionForm.status == FormStatus.ACTIVE,
                SubmissionForm.form_type.in_(list(VERIFIED_FORM_TYPES)),
            )
        ),
        exists(
            select(SubmissionChannel.id).where(
                SubmissionChannel.station_id == station_id_column,
                or_(
                    SubmissionChannel.manual_confirmed.is_(True),
                    SubmissionChannel.method.in_([SubmissionMethod.FORM, SubmissionMethod.PORTAL]),
                ),
            )
        ),
    )


def _route_bucket(route_type: str | None) -> str:
    if route_type in VERIFIED_SUBMISSION_ROUTE_TYPES:
        return "verified_submission"
    if route_type in CONTACT_ONLY_ROUTE_TYPES:
        return "contact_only"
    return ""


class StationListItem(BaseModel):
    id: int
    canonical_name: str
    country_code: str = ""
    city: str | None = None
    language: str = ""
    website_url: str | None = None
    status: str
    confidence_score: float
    updated_at: datetime
    genre_count: int = 0
    people_count: int = 0
    submission_count: int = 0
    quality_score: float | None = None
    submission_path_quality: str = ""
    outreach_bucket: str = ""
    best_submission_route_type: str = ""
    best_submission_route_url: str | None = None
    best_submission_route_email: str | None = None
    best_submission_route_confidence: float | None = None


class StationListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StationListItem]


class StationSubmissionDTO(BaseModel):
    id: int
    method: str
    url: str | None = None
    email: str | None = None
    requirements: str | None = None
    accepts_newcomers: bool = False


class StationContactDTO(BaseModel):
    id: int
    name: str | None = None
    role: str
    show_name: str | None = None
    email: str | None = None
    contact_url: str | None = None
    notes: str | None = None
    confidence: float


class StationPersonDTO(BaseModel):
    id: int
    name: str | None = None
    role: str
    show_name: str | None = None
    email: str | None = None
    contact_url: str | None = None
    linkedin_url: str | None = None
    musical_preferences: str | None = None
    genre_affinities: list[str] = Field(default_factory=list)
    confidence: float


class StationProgramDTO(BaseModel):
    id: int
    name: str
    description: str | None = None
    schedule: str | None = None


class StationFormDTO(BaseModel):
    id: int
    url: str
    page_title: str | None = None
    language: str | None = None
    form_type: str
    status: str
    requires_login: bool
    has_captcha: bool
    confidence: float
    last_verified_at: datetime | None = None


class StationDetailResponse(BaseModel):
    id: int
    canonical_name: str
    normalized_name: str
    country_code: str = ""
    city: str | None = None
    language: str = ""
    website_url: str | None = None
    stream_url: str | None = None
    status: str
    confidence_score: float
    created_at: datetime
    updated_at: datetime
    aliases: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    programs: list[StationProgramDTO] = Field(default_factory=list)
    submissions: list[StationSubmissionDTO] = Field(default_factory=list)
    contacts: list[StationContactDTO] = Field(default_factory=list)
    people: list[StationPersonDTO] = Field(default_factory=list)
    forms: list[StationFormDTO] = Field(default_factory=list)


class StationUpdateRequest(BaseModel):
    canonical_name: str | None = Field(default=None, min_length=1, max_length=255)
    country_code: str | None = Field(default=None, max_length=8)
    city: str | None = Field(default=None, max_length=120)
    language: str | None = Field(default=None, max_length=64)
    website_url: str | None = Field(default=None, max_length=1024)
    stream_url: str | None = Field(default=None, max_length=1024)
    status: Literal["candidate", "verified", "rejected"] | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)


def _apply_station_filters(
    stmt: Select[tuple[Station]],
    q: str,
    country: str,
    status: str,
    has_submission: bool | None,
    has_people: bool | None,
    min_confidence: float,
    outreach: str,
) -> Select[tuple[Station]]:
    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Station.canonical_name).like(needle),
                func.lower(func.coalesce(Station.city, "")).like(needle),
                func.lower(func.coalesce(Station.website_url, "")).like(needle),
            )
        )
    if country:
        stmt = stmt.where(func.upper(Station.country_code) == country.upper())
    safe_status = status.lower()
    if safe_status == "candidate":
        stmt = stmt.where(Station.status == StationStatus.CANDIDATE)
    elif safe_status == "verified":
        stmt = stmt.where(Station.status == StationStatus.VERIFIED)
    elif safe_status == "rejected":
        stmt = stmt.where(Station.status == StationStatus.REJECTED)
    elif safe_status == "archived":
        stmt = stmt.where(Station.status == StationStatus.ARCHIVED)
    elif safe_status in {"all", "raw"}:
        pass
    else:
        stmt = stmt.where(Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]))
    if has_people is True:
        stmt = stmt.where(exists(select(StationPerson.id).where(StationPerson.station_id == Station.id)))
    if has_people is False:
        stmt = stmt.where(~exists(select(StationPerson.id).where(StationPerson.station_id == Station.id)))
    if min_confidence > 0:
        stmt = stmt.where(Station.confidence_score >= min_confidence)
    latest_quality = (
        select(func.max(StationSubmissionAssessment.id))
        .where(
            StationSubmissionAssessment.station_id == Station.id,
            StationSubmissionAssessment.assessment_kind == "llm_quality_v1",
        )
        .correlate(Station)
        .scalar_subquery()
    )
    latest_quality_exists = exists(
        select(StationSubmissionAssessment.id).where(
            StationSubmissionAssessment.id == latest_quality,
        )
    )
    path_quality = _quality_evidence_text("submission_path_quality")
    quality_score = cast(_quality_evidence_number("quality_score"), Float)
    verified_submission_condition = or_(
        _has_verified_submission_condition(),
        exists(
            select(StationSubmissionAssessment.id).where(
                StationSubmissionAssessment.id == latest_quality,
                path_quality.in_(list(VERIFIED_SUBMISSION_ROUTE_TYPES)),
            )
        ),
    )
    if has_submission is True:
        stmt = stmt.where(verified_submission_condition)
    if has_submission is False:
        stmt = stmt.where(not_(verified_submission_condition))
    if outreach:
        stmt = stmt.outerjoin(
            StationSubmissionAssessment,
            StationSubmissionAssessment.id == latest_quality,
        )
        safe_outreach = outreach.strip().lower()
        if safe_outreach in {"verified_submission", "submission_confirmed"}:
            stmt = stmt.where(
                or_(
                    path_quality.in_(list(VERIFIED_SUBMISSION_ROUTE_TYPES)),
                    verified_submission_condition,
                ),
            )
        elif safe_outreach in {"contact_only", "contact_only_quality"}:
            stmt = stmt.where(
                not_(verified_submission_condition),
                or_(
                    path_quality.in_(list(CONTACT_ONLY_ROUTE_TYPES)),
                    Station.best_submission_route_type.in_(list(CONTACT_ONLY_ROUTE_TYPES)),
                ),
            )
        elif safe_outreach == "needs_review":
            stmt = stmt.where(
                latest_quality_exists,
                path_quality.in_(["submission_page_signal", "none", "unknown"]),
            )
    return stmt


@router.get("", response_model=StationListResponse)
def list_stations(
    q: str = Query(default=""),
    country: str = Query(default=""),
    status: str = Query(default=""),
    has_submission: Literal["any", "yes", "no"] = Query(default="any"),
    has_people: Literal["any", "yes", "no"] = Query(default="any"),
    outreach: Literal[
        "",
        "verified_submission",
        "contact_only",
        "submission_confirmed",
        "contact_only_quality",
        "needs_review",
    ] = Query(default=""),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: Literal["updated_at", "confidence", "name"] = Query(default="updated_at"),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    db: Session = Depends(_get_db),
) -> StationListResponse:
    has_submission_filter = True if has_submission == "yes" else False if has_submission == "no" else None
    has_people_filter = True if has_people == "yes" else False if has_people == "no" else None

    stmt: Select[tuple[Station]] = select(Station)
    stmt = _apply_station_filters(
        stmt=stmt,
        q=q.strip(),
        country=country.strip(),
        status=status.strip(),
        has_submission=has_submission_filter,
        has_people=has_people_filter,
        min_confidence=min_confidence,
        outreach=outreach.strip(),
    )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    order_column = Station.updated_at
    if sort_by == "confidence":
        order_column = Station.confidence_score
    elif sort_by == "name":
        order_column = Station.canonical_name

    if sort_order == "asc":
        stmt = stmt.order_by(order_column.asc(), Station.id.asc())
    else:
        stmt = stmt.order_by(order_column.desc(), Station.id.desc())

    offset = (page - 1) * page_size
    stations = db.scalars(stmt.offset(offset).limit(page_size)).all()
    station_ids = [row.id for row in stations]

    genre_counts: dict[int, int] = {}
    people_counts: dict[int, int] = {}
    submission_counts: dict[int, int] = {}
    verified_submission_flags: dict[int, bool] = {}
    quality_by_station: dict[int, dict[str, object]] = {}

    if station_ids:
        genre_counts = {
            int(sid): int(count)
            for sid, count in db.execute(
                select(StationGenre.station_id, func.count(StationGenre.id))
                .where(StationGenre.station_id.in_(station_ids))
                .group_by(StationGenre.station_id)
            ).all()
        }
        people_counts = {
            int(sid): int(count)
            for sid, count in db.execute(
                select(StationPerson.station_id, func.count(StationPerson.id))
                .where(StationPerson.station_id.in_(station_ids))
                .group_by(StationPerson.station_id)
            ).all()
        }
        submission_counts = {
            int(sid): int(count)
            for sid, count in db.execute(
                select(SubmissionChannel.station_id, func.count(SubmissionChannel.id))
                .where(SubmissionChannel.station_id.in_(station_ids))
                .group_by(SubmissionChannel.station_id)
            ).all()
        }
        verified_station_ids = set(
            int(sid)
            for sid, in db.execute(
                select(Station.id).where(
                    Station.id.in_(station_ids),
                    _has_verified_submission_condition(Station.id),
                )
            ).all()
        )
        verified_submission_flags = {sid: sid in verified_station_ids for sid in station_ids}
        quality_rows = db.execute(
            select(
                StationSubmissionAssessment.station_id,
                StationSubmissionAssessment.evidence_json,
                StationSubmissionAssessment.is_real_station,
                StationSubmissionAssessment.accepts_music_submissions,
            )
            .where(
                StationSubmissionAssessment.station_id.in_(station_ids),
                StationSubmissionAssessment.assessment_kind == "llm_quality_v1",
                StationSubmissionAssessment.id.in_(
                    select(func.max(StationSubmissionAssessment.id))
                    .where(
                        StationSubmissionAssessment.station_id.in_(station_ids),
                        StationSubmissionAssessment.assessment_kind == "llm_quality_v1",
                    )
                    .group_by(StationSubmissionAssessment.station_id)
                ),
            )
        ).all()
        for sid, evidence_json, is_real_station, accepts_music_submissions in quality_rows:
            payload = _parse_json(evidence_json, {})
            if not isinstance(payload, dict):
                payload = {}
            path_quality = str(payload.get("submission_path_quality") or "")
            quality_score_raw = payload.get("quality_score")
            try:
                quality_score_value = float(quality_score_raw) if quality_score_raw is not None else None
            except Exception:
                quality_score_value = None
            bucket = _route_bucket(path_quality)
            if bucket:
                pass
            elif is_real_station and path_quality:
                bucket = "needs_review"
            else:
                bucket = ""
            quality_by_station[int(sid)] = {
                "submission_path_quality": path_quality,
                "quality_score": quality_score_value,
                "outreach_bucket": bucket,
            }

    return StationListResponse(
        total=int(total),
        page=page,
        page_size=page_size,
        items=[
            StationListItem(
                id=row.id,
                canonical_name=row.canonical_name,
                country_code=row.country_code or "",
                city=row.city,
                language=row.language or "",
                website_url=row.website_url,
                status=_enum_value(row.status),
                confidence_score=float(row.confidence_score or 0.0),
                updated_at=row.updated_at,
                genre_count=genre_counts.get(row.id, 0),
                people_count=people_counts.get(row.id, 0),
                submission_count=submission_counts.get(row.id, 0),
                quality_score=quality_by_station.get(row.id, {}).get("quality_score"),  # type: ignore[arg-type]
                submission_path_quality=str(
                    row.best_submission_route_type
                    or quality_by_station.get(row.id, {}).get("submission_path_quality")
                    or ""
                ),
                outreach_bucket=str(
                    ("verified_submission" if verified_submission_flags.get(row.id) else "")
                    or _route_bucket(row.best_submission_route_type)
                    or quality_by_station.get(row.id, {}).get("outreach_bucket")
                    or ""
                ),
                best_submission_route_type=str(row.best_submission_route_type or ""),
                best_submission_route_url=row.best_submission_route_url,
                best_submission_route_email=row.best_submission_route_email,
                best_submission_route_confidence=(
                    float(row.best_submission_route_confidence)
                    if row.best_submission_route_confidence is not None
                    else None
                ),
            )
            for row in stations
        ],
    )


@router.get("/{station_id}", response_model=StationDetailResponse)
def get_station_detail(station_id: int, db: Session = Depends(_get_db)) -> StationDetailResponse:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    aliases = db.scalars(
        select(StationAlias.alias).where(StationAlias.station_id == station_id).order_by(StationAlias.alias.asc())
    ).all()
    genres = db.scalars(
        select(StationGenre.genre).where(StationGenre.station_id == station_id).order_by(StationGenre.genre.asc())
    ).all()
    programs = db.scalars(
        select(StationProgram).where(StationProgram.station_id == station_id).order_by(StationProgram.id.desc())
    ).all()
    submissions = db.scalars(
        select(SubmissionChannel).where(SubmissionChannel.station_id == station_id).order_by(SubmissionChannel.id.desc())
    ).all()
    contacts = db.scalars(
        select(StationContact).where(StationContact.station_id == station_id).order_by(StationContact.id.desc())
    ).all()
    people = db.scalars(
        select(StationPerson).where(StationPerson.station_id == station_id).order_by(StationPerson.id.desc())
    ).all()
    forms = db.scalars(
        select(SubmissionForm).where(SubmissionForm.station_id == station_id).order_by(SubmissionForm.updated_at.desc())
    ).all()

    return StationDetailResponse(
        id=station.id,
        canonical_name=station.canonical_name,
        normalized_name=station.normalized_name,
        country_code=station.country_code or "",
        city=station.city,
        language=station.language or "",
        website_url=station.website_url,
        stream_url=station.stream_url,
        status=_enum_value(station.status),
        confidence_score=float(station.confidence_score or 0.0),
        created_at=station.created_at,
        updated_at=station.updated_at,
        aliases=[str(item) for item in aliases],
        genres=[str(item) for item in genres],
        programs=[
            StationProgramDTO(
                id=item.id,
                name=item.name,
                description=item.description,
                schedule=item.schedule,
            )
            for item in programs
        ],
        submissions=[
            StationSubmissionDTO(
                id=item.id,
                method=_enum_value(item.method),
                url=item.url,
                email=item.email,
                requirements=item.requirements,
                accepts_newcomers=bool(item.accepts_newcomers),
            )
            for item in submissions
        ],
        contacts=[
            StationContactDTO(
                id=item.id,
                name=item.name,
                role=_enum_value(item.role),
                show_name=item.show_name,
                email=item.email,
                contact_url=item.contact_url,
                notes=item.notes,
                confidence=float(item.confidence or 0.0),
            )
            for item in contacts
        ],
        people=[
            StationPersonDTO(
                id=item.id,
                name=item.name,
                role=_enum_value(item.role),
                show_name=item.show_name,
                email=item.email,
                contact_url=item.contact_url,
                linkedin_url=item.linkedin_url,
                musical_preferences=item.musical_preferences,
                genre_affinities=list(_parse_json(item.genre_affinities_json, [])),
                confidence=float(item.confidence or 0.0),
            )
            for item in people
        ],
        forms=[
            StationFormDTO(
                id=item.id,
                url=item.url,
                page_title=item.page_title,
                language=item.language,
                form_type=_enum_value(item.form_type),
                status=_enum_value(item.status),
                requires_login=bool(item.requires_login),
                has_captcha=bool(item.has_captcha),
                confidence=float(item.confidence or 0.0),
                last_verified_at=item.last_verified_at,
            )
            for item in forms
        ],
    )


@router.patch("/{station_id}", response_model=StationDetailResponse)
def update_station(
    station_id: int,
    payload: StationUpdateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationDetailResponse:
    station = db.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")

    if payload.canonical_name is not None:
        station.canonical_name = payload.canonical_name.strip()
        station.normalized_name = payload.canonical_name.strip().lower()
    if payload.country_code is not None:
        station.country_code = payload.country_code.strip().upper()
    if payload.city is not None:
        station.city = payload.city.strip() or None
    if payload.language is not None:
        station.language = payload.language.strip()
    if payload.website_url is not None:
        station.website_url = payload.website_url.strip() or None
    if payload.stream_url is not None:
        station.stream_url = payload.stream_url.strip() or None
    if payload.status is not None:
        if payload.status == "candidate":
            station.status = StationStatus.CANDIDATE
        elif payload.status == "verified":
            station.status = StationStatus.VERIFIED
        elif payload.status == "rejected":
            station.status = StationStatus.REJECTED
    if payload.confidence_score is not None:
        station.confidence_score = float(payload.confidence_score)

    db.add(station)
    db.commit()
    db.refresh(station)

    return get_station_detail(station_id=station.id, db=db)

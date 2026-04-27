from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from radio_db.db import SessionLocal
from radio_db.models.entities import (
    FormRecipe,
    FormStatus,
    FormType,
    Station,
    SubmissionForm,
    SubmissionFormField,
)
from radio_db.services.forms import scan_submission_forms

router = APIRouter(prefix="/api/v1/forms", tags=["forms"])


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


class FormFieldDTO(BaseModel):
    id: int
    field_key: str
    name: str | None = None
    label: str | None = None
    input_type: str
    required: bool
    placeholder: str | None = None
    options: list[str] = Field(default_factory=list)
    validation_hint: str | None = None
    max_length: int | None = None
    accept_types: str | None = None
    upload_max_mb: float | None = None
    source_snapshot_path: str | None = None


class FormRecipeDTO(BaseModel):
    id: int
    version: int
    mode: str
    confidence_score: float
    status: str
    instructions_text: str | None = None
    machine_mapping: object = Field(default_factory=dict)
    field_order: list[str] = Field(default_factory=list)
    upload_strategy: object = Field(default_factory=dict)
    submit_strategy: object = Field(default_factory=dict)
    success_detection_rules: object = Field(default_factory=list)
    error_detection_rules: object = Field(default_factory=list)
    retry_rules: object = Field(default_factory=list)
    notes_for_future_runs: str | None = None
    discovered_at: datetime
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class FormListItem(BaseModel):
    id: int
    station_id: int
    station_name: str
    url: str
    page_title: str | None = None
    language: str | None = None
    form_type: str
    status: str
    requires_login: bool
    has_captcha: bool
    confidence: float
    field_count: int = 0
    recipe_count: int = 0
    discovered_at: datetime
    last_verified_at: datetime | None = None
    updated_at: datetime


class FormListResponse(BaseModel):
    total: int
    items: list[FormListItem]


class FormDetailResponse(BaseModel):
    id: int
    station_id: int
    station_name: str
    url: str
    page_title: str | None = None
    language: str | None = None
    form_type: str
    status: str
    requires_login: bool
    has_captcha: bool
    confidence: float
    entry_path: list[str] = Field(default_factory=list)
    snapshot_path: str | None = None
    dom_snapshot_path: str | None = None
    discovered_at: datetime
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    fields: list[FormFieldDTO] = Field(default_factory=list)
    recipes: list[FormRecipeDTO] = Field(default_factory=list)


class FormUpdateRequest(BaseModel):
    page_title: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=32)
    form_type: Literal["general_contact", "music_submission", "artist_upload", "show_pitch", "newcomer", "unknown"] | None = None
    status: Literal["active", "stale", "broken", "login_required", "captcha_present", "unknown"] | None = None
    requires_login: bool | None = None
    has_captcha: bool | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    snapshot_path: str | None = Field(default=None, max_length=1024)
    dom_snapshot_path: str | None = Field(default=None, max_length=1024)


class FormScanRequest(BaseModel):
    station_id: int = Field(ge=1)
    station_limit: int | None = Field(default=1, ge=1, le=100)
    country: str | None = Field(default=None, max_length=8)
    mode: str = Field(default="read", min_length=1, max_length=32)
    max_forms_per_station: int | None = Field(default=5, ge=1, le=20)


def _serialize_field(field: SubmissionFormField) -> FormFieldDTO:
    return FormFieldDTO(
        id=field.id,
        field_key=field.field_key,
        name=field.name,
        label=field.label,
        input_type=field.input_type,
        required=bool(field.required),
        placeholder=field.placeholder,
        options=list(_parse_json(field.options_json, [])),
        validation_hint=field.validation_hint,
        max_length=field.max_length,
        accept_types=field.accept_types,
        upload_max_mb=field.upload_max_mb,
        source_snapshot_path=field.source_snapshot_path,
    )


def _serialize_recipe(recipe: FormRecipe) -> FormRecipeDTO:
    return FormRecipeDTO(
        id=recipe.id,
        version=recipe.version,
        mode=recipe.mode,
        confidence_score=float(recipe.confidence_score or 0.0),
        status=recipe.status,
        instructions_text=recipe.instructions_text,
        machine_mapping=_parse_json(recipe.machine_mapping_json, {}),
        field_order=list(_parse_json(recipe.field_order_json, [])),
        upload_strategy=_parse_json(recipe.upload_strategy_json, {}),
        submit_strategy=_parse_json(recipe.submit_strategy_json, {}),
        success_detection_rules=_parse_json(recipe.success_detection_rules_json, []),
        error_detection_rules=_parse_json(recipe.error_detection_rules_json, []),
        retry_rules=_parse_json(recipe.retry_rules_json, []),
        notes_for_future_runs=recipe.notes_for_future_runs,
        discovered_at=recipe.discovered_at,
        last_verified_at=recipe.last_verified_at,
        created_at=recipe.created_at,
        updated_at=recipe.updated_at,
    )


def _station_name(db: Session, station_id: int) -> str:
    station_name = db.scalar(select(Station.canonical_name).where(Station.id == station_id))
    if station_name is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return str(station_name)


@router.get("", response_model=FormListResponse)
def list_forms(
    station_id: int | None = Query(default=None, ge=1),
    status: str = Query(default=""),
    form_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(_get_db),
) -> FormListResponse:
    stmt: Select[tuple[SubmissionForm, str, int, int]] = select(
        SubmissionForm,
        Station.canonical_name,
        func.count(SubmissionFormField.id).label("field_count"),
        func.count(FormRecipe.id).label("recipe_count"),
    ).join(Station, Station.id == SubmissionForm.station_id).outerjoin(
        SubmissionFormField, SubmissionFormField.form_id == SubmissionForm.id
    ).outerjoin(FormRecipe, FormRecipe.form_id == SubmissionForm.id)

    if station_id is not None:
        stmt = stmt.where(SubmissionForm.station_id == station_id)
    if status.strip():
        stmt = stmt.where(func.lower(SubmissionForm.status) == status.strip().lower())
    if form_type.strip():
        stmt = stmt.where(func.lower(SubmissionForm.form_type) == form_type.strip().lower())

    stmt = stmt.group_by(SubmissionForm.id, Station.canonical_name)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(SubmissionForm.updated_at.desc(), SubmissionForm.id.desc()).offset(offset).limit(limit)).all()

    return FormListResponse(
        total=int(total),
        items=[
            FormListItem(
                id=form.id,
                station_id=form.station_id,
                station_name=station_name,
                url=form.url,
                page_title=form.page_title,
                language=form.language,
                form_type=_enum_value(form.form_type),
                status=_enum_value(form.status),
                requires_login=bool(form.requires_login),
                has_captcha=bool(form.has_captcha),
                confidence=float(form.confidence or 0.0),
                field_count=int(field_count or 0),
                recipe_count=int(recipe_count or 0),
                discovered_at=form.discovered_at,
                last_verified_at=form.last_verified_at,
                updated_at=form.updated_at,
            )
            for form, station_name, field_count, recipe_count in rows
        ],
    )


@router.get("/{form_id}", response_model=FormDetailResponse)
def get_form(form_id: int, db: Session = Depends(_get_db)) -> FormDetailResponse:
    form = db.scalar(select(SubmissionForm).where(SubmissionForm.id == form_id))
    if form is None:
        raise HTTPException(status_code=404, detail="form_not_found")

    station_name = _station_name(db, form.station_id)
    fields = db.scalars(
        select(SubmissionFormField).where(SubmissionFormField.form_id == form_id).order_by(SubmissionFormField.id.asc())
    ).all()
    recipes = db.scalars(
        select(FormRecipe).where(FormRecipe.form_id == form_id).order_by(FormRecipe.version.desc(), FormRecipe.id.desc())
    ).all()

    return FormDetailResponse(
        id=form.id,
        station_id=form.station_id,
        station_name=station_name,
        url=form.url,
        page_title=form.page_title,
        language=form.language,
        form_type=_enum_value(form.form_type),
        status=_enum_value(form.status),
        requires_login=bool(form.requires_login),
        has_captcha=bool(form.has_captcha),
        confidence=float(form.confidence or 0.0),
        entry_path=list(_parse_json(form.entry_path_json, [])),
        snapshot_path=form.snapshot_path,
        dom_snapshot_path=form.dom_snapshot_path,
        discovered_at=form.discovered_at,
        last_verified_at=form.last_verified_at,
        created_at=form.created_at,
        updated_at=form.updated_at,
        fields=[_serialize_field(field) for field in fields],
        recipes=[_serialize_recipe(recipe) for recipe in recipes],
    )


@router.patch("/{form_id}", response_model=FormDetailResponse)
def update_form(form_id: int, request: FormUpdateRequest, db: Session = Depends(_get_db)) -> FormDetailResponse:
    form = db.scalar(select(SubmissionForm).where(SubmissionForm.id == form_id))
    if form is None:
        raise HTTPException(status_code=404, detail="form_not_found")

    if request.page_title is not None:
        form.page_title = request.page_title
    if request.language is not None:
        form.language = request.language
    if request.form_type is not None:
        form.form_type = FormType(request.form_type)
    if request.status is not None:
        form.status = FormStatus(request.status)
    if request.requires_login is not None:
        form.requires_login = request.requires_login
    if request.has_captcha is not None:
        form.has_captcha = request.has_captcha
    if request.confidence is not None:
        form.confidence = request.confidence
    if request.snapshot_path is not None:
        form.snapshot_path = request.snapshot_path
    if request.dom_snapshot_path is not None:
        form.dom_snapshot_path = request.dom_snapshot_path

    db.commit()
    return get_form(form_id, db)


@router.post("/scan")
def scan_forms(request: FormScanRequest, db: Session = Depends(_get_db)) -> dict:
    _station_name(db, request.station_id)
    result = scan_submission_forms(
        session=db,
        station_limit=request.station_limit,
        country=request.country,
        station_id=request.station_id,
        mode=request.mode,
        max_forms_per_station=request.max_forms_per_station,
    )
    return {"requested_station_id": request.station_id, "result": result}
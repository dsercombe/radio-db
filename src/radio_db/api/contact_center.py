from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from radio_db.api.common import PermissionMode, get_permission_mode, require_permission_mode
from radio_db.db import SessionLocal
from radio_db.services.contact_center import (
    build_contact_draft,
    create_dry_run_send,
    create_execute_send,
    create_contact_template,
    create_persisted_contact_draft,
    get_persisted_contact_draft,
    list_contact_templates,
    list_draft_sends,
    list_send_outcomes,
    list_station_drafts,
    preview_persisted_contact_draft,
)

router = APIRouter(prefix="/api/v1/contact-center", tags=["contact-center"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ContactRouteDTO(BaseModel):
    kind: str
    label: str
    value: str


class ContactDraftResponse(BaseModel):
    station_id: int
    station_name: str
    locale: str
    language: str
    country_code: str
    city: str | None = None
    subject: str
    body: str
    locale_hint: str
    recommended_channel: str
    available_routes: list[ContactRouteDTO] = Field(default_factory=list)
    evidence_summary: list[str] = Field(default_factory=list)
    permission_mode: PermissionMode
    template_id: int | None = None
    campaign_id: int | None = None
    draft_id: int | None = None
    status: str = "draft"
    created_at: str | None = None
    updated_at: str | None = None


class ContactTemplateLocaleDTO(BaseModel):
    id: int
    locale_key: str
    language_code: str
    subject_template: str
    body_template: str
    created_at: datetime
    updated_at: datetime


class ContactTemplateDTO(BaseModel):
    id: int
    template_key: str
    name: str
    description: str | None = None
    channel: str
    variables: list[str] = Field(default_factory=list)
    is_active: bool
    created_at: datetime
    updated_at: datetime
    locales: list[ContactTemplateLocaleDTO] = Field(default_factory=list)


class ContactTemplateListResponse(BaseModel):
    items: list[ContactTemplateDTO] = Field(default_factory=list)


class ContactTemplateCreateRequest(BaseModel):
    template_key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    channel: str = Field(default="email", min_length=1, max_length=32)
    variables: list[str] = Field(default_factory=list)
    locale_key: str = Field(default="default", min_length=1, max_length=32)
    language_code: str = Field(default="en", min_length=1, max_length=16)
    subject_template: str = Field(min_length=1)
    body_template: str = Field(min_length=1)


class ContactDraftPreviewRequest(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = None


class ContactDraftCreateRequest(BaseModel):
    template_id: int | None = Field(default=None, ge=1)
    campaign_id: int | None = Field(default=None, ge=1)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = None


class ContactDraftListResponse(BaseModel):
    items: list[ContactDraftResponse] = Field(default_factory=list)


class ContactSendResponse(BaseModel):
    id: int
    draft_id: int
    station_id: int
    channel: str
    target_value: str | None = None
    mode: str
    status: str
    payload: dict = Field(default_factory=dict)
    created_at: str
    updated_at: str
    outcome_count: int = 0


class ContactSendListResponse(BaseModel):
    items: list[ContactSendResponse] = Field(default_factory=list)


class ContactOutcomeResponse(BaseModel):
    id: int
    send_id: int
    outcome_type: str
    status: str
    details: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: str


class ContactOutcomeListResponse(BaseModel):
    items: list[ContactOutcomeResponse] = Field(default_factory=list)


class ContactSendCreateRequest(BaseModel):
    mode: str = Field(default="dry-run", pattern="^(dry-run|execute)$")
    channel: str | None = Field(default=None, max_length=64)
    target_value: str | None = Field(default=None, max_length=1024)


def _serialize_draft(payload: object, permission_mode: PermissionMode) -> ContactDraftResponse:
    return ContactDraftResponse(
        **payload.__dict__,
        permission_mode=permission_mode,
    )


def _serialize_template(template: object) -> ContactTemplateDTO:
    locales = [
        ContactTemplateLocaleDTO(
            id=locale.id,
            locale_key=locale.locale_key,
            language_code=locale.language_code,
            subject_template=locale.subject_template,
            body_template=locale.body_template,
            created_at=locale.created_at,
            updated_at=locale.updated_at,
        )
        for locale in template.locales
    ]
    import json

    return ContactTemplateDTO(
        id=template.id,
        template_key=template.template_key,
        name=template.name,
        description=template.description,
        channel=template.channel,
        variables=list(json.loads(template.variables_json or "[]")),
        is_active=bool(template.is_active),
        created_at=template.created_at,
        updated_at=template.updated_at,
        locales=locales,
    )


def _serialize_send(payload: object) -> ContactSendResponse:
    return ContactSendResponse(**payload.__dict__)


def _serialize_outcome(payload: object) -> ContactOutcomeResponse:
    return ContactOutcomeResponse(**payload.__dict__)


@router.get("/templates", response_model=ContactTemplateListResponse)
def get_contact_templates(db: Session = Depends(_get_db)) -> ContactTemplateListResponse:
    return ContactTemplateListResponse(items=[_serialize_template(item) for item in list_contact_templates(db)])


@router.post("/templates", response_model=ContactTemplateDTO)
def create_template(
    request: ContactTemplateCreateRequest,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("execute")),
) -> ContactTemplateDTO:
    template = create_contact_template(
        session=db,
        template_key=request.template_key.strip(),
        name=request.name.strip(),
        description=request.description,
        channel=request.channel.strip(),
        locale_key=request.locale_key.strip(),
        language_code=request.language_code.strip(),
        subject_template=request.subject_template,
        body_template=request.body_template,
        variables=request.variables,
    )
    return _serialize_template(template)


@router.get("/stations/{station_id}/draft", response_model=ContactDraftResponse)
def get_station_contact_draft(
    station_id: int,
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(get_permission_mode),
) -> ContactDraftResponse:
    payload = build_contact_draft(db, station_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return _serialize_draft(payload, permission_mode)


@router.get("/stations/{station_id}/drafts", response_model=ContactDraftListResponse)
def get_station_contact_drafts(
    station_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(get_permission_mode),
) -> ContactDraftListResponse:
    return ContactDraftListResponse(
        items=[_serialize_draft(item, permission_mode) for item in list_station_drafts(db, station_id, limit=limit)]
    )


@router.post("/stations/{station_id}/drafts", response_model=ContactDraftResponse)
def create_station_contact_draft(
    station_id: int,
    request: ContactDraftCreateRequest,
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> ContactDraftResponse:
    payload = create_persisted_contact_draft(
        session=db,
        station_id=station_id,
        template_id=request.template_id,
        subject=request.subject,
        body=request.body,
        campaign_id=request.campaign_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return _serialize_draft(payload, permission_mode)


@router.get("/drafts/{draft_id}", response_model=ContactDraftResponse)
def get_contact_draft(
    draft_id: int,
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(get_permission_mode),
) -> ContactDraftResponse:
    payload = get_persisted_contact_draft(db, draft_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return _serialize_draft(payload, permission_mode)


@router.post("/drafts/{draft_id}/preview", response_model=ContactDraftResponse)
def preview_contact_draft(
    draft_id: int,
    request: ContactDraftPreviewRequest,
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> ContactDraftResponse:
    payload = preview_persisted_contact_draft(
        session=db,
        draft_id=draft_id,
        subject=request.subject,
        body=request.body,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return _serialize_draft(payload, permission_mode)


@router.post("/drafts/{draft_id}/send", response_model=ContactSendResponse)
def create_contact_send(
    draft_id: int,
    request: ContactSendCreateRequest,
    db: Session = Depends(_get_db),
    permission_mode: PermissionMode = Depends(get_permission_mode),
) -> ContactSendResponse:
    if request.mode == "execute":
        if permission_mode != "execute":
            raise HTTPException(status_code=403, detail="insufficient_permission_mode:execute_required_current_" + permission_mode)
        payload = create_execute_send(
            session=db,
            draft_id=draft_id,
            target_value=request.target_value,
            channel=request.channel,
        )
    else:
        if permission_mode not in {"dry-run", "execute"}:
            raise HTTPException(status_code=403, detail="insufficient_permission_mode:dry-run_required_current_" + permission_mode)
        payload = create_dry_run_send(
            session=db,
            draft_id=draft_id,
            target_value=request.target_value,
            channel=request.channel,
        )
    if payload is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return _serialize_send(payload)


@router.get("/drafts/{draft_id}/sends", response_model=ContactSendListResponse)
def get_draft_sends(
    draft_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(_get_db),
) -> ContactSendListResponse:
    return ContactSendListResponse(items=[_serialize_send(item) for item in list_draft_sends(db, draft_id, limit=limit)])


@router.get("/sends/{send_id}/outcomes", response_model=ContactOutcomeListResponse)
def get_send_outcomes(
    send_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(_get_db),
) -> ContactOutcomeListResponse:
    return ContactOutcomeListResponse(items=[_serialize_outcome(item) for item in list_send_outcomes(db, send_id, limit=limit)])

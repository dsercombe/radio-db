from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from radio_db.api.common import PermissionMode, require_permission_mode
from radio_db.db import SessionLocal
from radio_db.services.outreach_campaign import (
    archive_outreach_campaign,
    build_short_press_release_tracking_url,
    build_press_release_tracking_url,
    campaign_click_stats,
    campaign_monitor,
    create_outreach_campaign,
    generate_outreach_email,
    get_outreach_campaign,
    list_outreach_campaigns,
    patch_outreach_campaign,
    record_press_release_click,
    record_press_release_click_by_code,
)

router = APIRouter(prefix="/api/v1/outreach-campaigns", tags=["outreach-campaigns"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class OutreachCampaignDTO(BaseModel):
    id: int
    name: str
    artist_name: str
    song_title: str
    release_date: str | None = None
    song_language: str | None = None
    pitch_text: str = ""
    reference_template: str = ""
    operator_notes: str | None = None
    press_release_url: str | None = None
    tracking_code: str | None = None
    press_release_tracking_url: str | None = None
    press_release_short_tracking_url: str | None = None
    press_release_click_count: int = 0
    press_release_last_clicked_at: datetime | None = None
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class OutreachCampaignListResponse(BaseModel):
    items: list[OutreachCampaignDTO] = Field(default_factory=list)


class OutreachCampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    artist_name: str = Field(min_length=1, max_length=255)
    song_title: str = Field(min_length=1, max_length=255)
    release_date: str | None = Field(default=None, max_length=64)
    song_language: str | None = Field(default=None, max_length=32)
    pitch_text: str = ""
    reference_template: str = ""
    operator_notes: str | None = None
    press_release_url: str | None = Field(default=None, max_length=2048)
    tracking_code: str | None = Field(default=None, max_length=120)


class OutreachCampaignPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    artist_name: str | None = Field(default=None, max_length=255)
    song_title: str | None = Field(default=None, max_length=255)
    release_date: str | None = Field(default=None, max_length=64)
    song_language: str | None = Field(default=None, max_length=32)
    pitch_text: str | None = None
    reference_template: str | None = None
    operator_notes: str | None = None
    press_release_url: str | None = Field(default=None, max_length=2048)
    tracking_code: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class GenerateEmailRequest(BaseModel):
    station_id: int = Field(ge=1)


class GenerateEmailResponse(BaseModel):
    subject: str
    body: str
    subject_en: str
    body_en: str


class OutreachCampaignMonitorSummary(BaseModel):
    total: int = 0
    not_started: int = 0
    drafted: int = 0
    sent: int = 0
    clicked: int = 0
    failed: int = 0
    blocked: int = 0
    simulated: int = 0
    unknown_clicks: int = 0


class OutreachCampaignMonitorItem(BaseModel):
    station_id: int
    station_name: str
    country_code: str = ""
    language: str = ""
    draft_id: int | None = None
    draft_status: str | None = None
    send_id: int | None = None
    send_status: str | None = None
    send_channel: str | None = None
    send_target: str | None = None
    send_count: int = 0
    last_sent_at: datetime | None = None
    click_count: int = 0
    last_clicked_at: datetime | None = None
    tracking_url: str | None = None
    monitor_status: str


class OutreachCampaignMonitorResponse(BaseModel):
    summary: OutreachCampaignMonitorSummary
    items: list[OutreachCampaignMonitorItem] = Field(default_factory=list)


def _dto(row: object, db: Session) -> OutreachCampaignDTO:
    stats = campaign_click_stats(db, row.id)
    return OutreachCampaignDTO(
        id=row.id,
        name=row.name,
        artist_name=row.artist_name,
        song_title=row.song_title,
        release_date=row.release_date,
        song_language=row.song_language,
        pitch_text=row.pitch_text or "",
        reference_template=row.reference_template or "",
        operator_notes=row.operator_notes,
        press_release_url=row.press_release_url,
        tracking_code=row.tracking_code,
        press_release_tracking_url=build_press_release_tracking_url(row.id) if row.press_release_url else None,
        press_release_short_tracking_url=build_short_press_release_tracking_url(row) if row.press_release_url else None,
        press_release_click_count=stats["press_release_click_count"],
        press_release_last_clicked_at=stats["press_release_last_clicked_at"],
        is_active=bool(row.is_active),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=OutreachCampaignListResponse)
def list_campaigns(
    active_only: bool = Query(default=False),
    db: Session = Depends(_get_db),
) -> OutreachCampaignListResponse:
    rows = list_outreach_campaigns(db, active_only=active_only)
    return OutreachCampaignListResponse(items=[_dto(r, db) for r in rows])


@router.post("", response_model=OutreachCampaignDTO)
def create_campaign(
    request: OutreachCampaignCreateRequest,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> OutreachCampaignDTO:
    row = create_outreach_campaign(
        db,
        name=request.name,
        artist_name=request.artist_name,
        song_title=request.song_title,
        release_date=request.release_date,
        song_language=request.song_language,
        pitch_text=request.pitch_text,
        reference_template=request.reference_template,
        operator_notes=request.operator_notes,
        press_release_url=request.press_release_url,
        tracking_code=request.tracking_code,
    )
    return _dto(row, db)


@router.get("/{campaign_id}", response_model=OutreachCampaignDTO)
def get_campaign(campaign_id: int, db: Session = Depends(_get_db)) -> OutreachCampaignDTO:
    row = get_outreach_campaign(db, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    return _dto(row, db)


@router.get("/{campaign_id}/monitor", response_model=OutreachCampaignMonitorResponse)
def get_campaign_monitor(campaign_id: int, db: Session = Depends(_get_db)) -> OutreachCampaignMonitorResponse:
    payload = campaign_monitor(db, campaign_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    return OutreachCampaignMonitorResponse(**payload)


@router.patch("/{campaign_id}", response_model=OutreachCampaignDTO)
def patch_campaign(
    campaign_id: int,
    request: OutreachCampaignPatchRequest,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> OutreachCampaignDTO:
    patch = request.model_dump(exclude_unset=True)
    row = patch_outreach_campaign(db, campaign_id, patch)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    return _dto(row, db)


@router.delete("/{campaign_id}", response_model=OutreachCampaignDTO)
def delete_campaign(
    campaign_id: int,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> OutreachCampaignDTO:
    row = archive_outreach_campaign(db, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign_not_found")
    return _dto(row, db)


@router.post("/{campaign_id}/generate-email", response_model=GenerateEmailResponse)
def post_generate_email(
    campaign_id: int,
    request: GenerateEmailRequest,
    db: Session = Depends(_get_db),
    _mode: PermissionMode = Depends(require_permission_mode("dry-run")),
) -> GenerateEmailResponse:
    try:
        payload = generate_outreach_email(db, campaign_id=campaign_id, station_id=request.station_id)
    except ValueError as exc:
        detail = str(exc)
        code = 404 if detail in {"campaign_not_found", "station_not_found"} else 400
        raise HTTPException(status_code=code, detail=detail) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return GenerateEmailResponse(**payload)


@router.get("/{campaign_id}/press-release-click")
def press_release_click(
    campaign_id: int,
    request: Request,
    station_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(_get_db),
) -> RedirectResponse:
    target_url = record_press_release_click(
        db,
        campaign_id=campaign_id,
        station_id=station_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
        referer=request.headers.get("referer"),
    )
    if not target_url:
        raise HTTPException(status_code=404, detail="press_release_not_found")
    return RedirectResponse(target_url, status_code=302)


def redirect_short_press_release_code(
    tracking_code: str,
    request: Request,
    station_id: int | None,
    db: Session,
) -> RedirectResponse | None:
    target_url = record_press_release_click_by_code(
        db,
        tracking_code=tracking_code,
        station_id=station_id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
        referer=request.headers.get("referer"),
    )
    if not target_url:
        return None
    return RedirectResponse(target_url, status_code=302)

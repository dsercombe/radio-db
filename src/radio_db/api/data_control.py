from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from radio_db.api.common import require_permission_mode
from radio_db.db import SessionLocal
from radio_db.services.data_control import build_data_control_overview, set_scan_focus
from sqlalchemy import select, func
from radio_db.models.entities import LinkTrackingCode, LinkClickEvent

router = APIRouter(prefix="/api/v1/data-control", tags=["data-control"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class DataControlOverviewResponse(BaseModel):
    generated_at: datetime
    monitor: object = Field(default_factory=dict)
    country_discovery: object = Field(default_factory=dict)
    api_history: list[object] = Field(default_factory=list)


class ScanFocusUpdateRequest(BaseModel):
    enabled: bool
    market_focus: Literal["international", "dach", "anglo", "eu_core", "top_major"] = "international"


class ScanFocusUpdateResponse(BaseModel):
    enabled: bool
    market_focus: str
    updated_by: str
    updated_at: str


@router.get("/overview", response_model=DataControlOverviewResponse)
def get_data_control_overview(
    history_limit: int = Query(default=30, ge=1, le=100),
    history_hours: int = Query(default=72, ge=1, le=24 * 30),
    db: Session = Depends(_get_db),
) -> DataControlOverviewResponse:
    return DataControlOverviewResponse(**build_data_control_overview(db, history_limit=history_limit, history_hours=history_hours))


@router.post("/scan-focus", response_model=ScanFocusUpdateResponse)
def update_scan_focus(
    request: ScanFocusUpdateRequest,
    _mode: str = Depends(require_permission_mode("execute")),
) -> ScanFocusUpdateResponse:
    payload = set_scan_focus(enabled=request.enabled, market_focus=request.market_focus, updated_by="api")
    return ScanFocusUpdateResponse(**payload)


@router.get("/link-tracking/metrics")
def data_control_link_metrics(campaign_id: int | None = Query(default=None)):
    db = SessionLocal()
    try:
        stmt = select(
            LinkTrackingCode.code,
            LinkTrackingCode.campaign_id,
            LinkTrackingCode.recipient_email,
            LinkTrackingCode.link_type,
            LinkTrackingCode.original_url,
            func.count(LinkClickEvent.id).label("clicks"),
        ).join(LinkClickEvent, LinkClickEvent.tracking_code_id == LinkTrackingCode.id, isouter=True)

        if campaign_id is not None:
            stmt = stmt.where(LinkTrackingCode.campaign_id == campaign_id)

        stmt = stmt.group_by(LinkTrackingCode.id).order_by(func.count(LinkClickEvent.id).desc())

        rows = db.execute(stmt).all()
        out = [
            {
                "code": r.code,
                "campaign_id": r.campaign_id,
                "recipient": r.recipient_email,
                "link_type": r.link_type,
                "original_url": r.original_url,
                "clicks": int(r.clicks or 0),
            }
            for r in rows
        ]
        return {"items": out}
    finally:
        db.close()

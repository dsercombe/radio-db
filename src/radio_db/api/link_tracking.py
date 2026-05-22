from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select, func

from radio_db.db import SessionLocal
from radio_db.models.entities import LinkTrackingCode, LinkClickEvent

# public redirect router (mounted at app root)
public_router = APIRouter()


@public_router.get("/r/{code}")
def redirect_tracked_link(code: str, request: Request):
    db = SessionLocal()
    try:
        stmt = select(LinkTrackingCode).where(LinkTrackingCode.code == code)
        rec = db.execute(stmt).scalars().first()
        if not rec:
            raise HTTPException(status_code=404, detail="tracking_code_not_found")

        # Log click event
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        referer = request.headers.get("referer")
        click = LinkClickEvent(tracking_code_id=rec.id, ip_address=ip, user_agent=ua, referer=referer)
        db.add(click)
        db.commit()

        return RedirectResponse(url=rec.original_url, status_code=302)
    finally:
        db.close()


# API router mounted under /api/v1/link-tracking
api_router = APIRouter(prefix="/api/v1/link-tracking", tags=["link-tracking"])


@api_router.get("/metrics")
def link_metrics(campaign_id: int | None = Query(default=None)):
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
        return JSONResponse(content={"items": out})
    finally:
        db.close()


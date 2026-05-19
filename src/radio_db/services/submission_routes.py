from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.models.entities import Station, SubmissionChannel, SubmissionForm, SubmissionMethod


@dataclass(frozen=True)
class RouteChoice:
    route_type: str
    url: str | None
    email: str | None
    confidence: float
    reason: str
    secondary: list[dict[str, object]]


def _method_value(value: object) -> str:
    return getattr(value, "value", str(value or ""))


def _score_channel(channel: SubmissionChannel) -> tuple[float, str, str]:
    method = _method_value(channel.method)
    value = channel.email or channel.url or ""
    requirements = channel.requirements or ""
    lowered = f"{method} {value} {requirements}".lower()
    score = 0.25
    route_type = "other_submission_route"
    reasons: list[str] = []

    if channel.manual_confirmed:
        score += 0.45
        reasons.append("manual confirmed")
    if channel.accepts_newcomers:
        score += 0.12
        reasons.append("accepts newcomers")

    if method in {SubmissionMethod.FORM.value, SubmissionMethod.PORTAL.value} and channel.url:
        score += 0.16
        route_type = "direct_music_form"
        reasons.append("form route")
        if any(token in lowered for token in ["submit", "submission", "new music", "music"]):
            score += 0.12
            reasons.append("music submission wording")
        if "google.com/forms" in lowered:
            score += 0.04
            reasons.append("google form")
        if any(token in lowered for token in ["captcha", "login", "restricted"]):
            route_type = "gated_music_form"
            score -= 0.1
            reasons.append("manual friction")
    elif method == SubmissionMethod.EMAIL.value and channel.email:
        route_type = "explicit_submission_email"
        score += 0.1
        if any(token in lowered for token in ["submit", "submission", "music", "newmusic", "playlist", "promo"]):
            score += 0.16
            reasons.append("dedicated submission email")
        elif channel.email.startswith("info@") or channel.email.startswith("contact@"):
            score -= 0.08
            route_type = "contact_email_only"
            reasons.append("generic email fallback")
    elif channel.url:
        score += 0.04
        if any(token in lowered for token in ["contact", "about"]):
            route_type = "contact_form_only"
            reasons.append("contact route")

    if not reasons:
        reasons.append("recorded route")
    return max(0.0, min(score, 1.0)), route_type, ", ".join(reasons)


def _score_form(form: SubmissionForm) -> tuple[float, str, str]:
    title = form.page_title or ""
    lowered = f"{form.url} {title} {form.form_type} {form.status}".lower()
    score = 0.35 + float(form.confidence or 0.0) * 0.35
    route_type = "direct_music_form"
    reasons = ["detected form"]
    if str(form.status).endswith("ACTIVE") or str(form.status).lower().endswith("active"):
        score += 0.08
        reasons.append("active")
    if any(token in lowered for token in ["submit", "submission", "music", "artist", "upload"]):
        score += 0.12
        reasons.append("music submission wording")
    if form.has_captcha or form.requires_login:
        route_type = "gated_music_form"
        score -= 0.12
        reasons.append("manual friction")
    return max(0.0, min(score, 1.0)), route_type, ", ".join(reasons)


def choose_primary_submission_route(db: Session, station: Station) -> RouteChoice | None:
    candidates: list[dict[str, object]] = []
    channels = db.scalars(select(SubmissionChannel).where(SubmissionChannel.station_id == station.id)).all()
    for channel in channels:
        score, route_type, reason = _score_channel(channel)
        candidates.append({
            "kind": "submission_channel",
            "id": channel.id,
            "route_type": route_type,
            "url": channel.url,
            "email": channel.email,
            "confidence": round(score, 3),
            "reason": reason,
            "manual_confirmed": bool(channel.manual_confirmed),
            "accepts_newcomers": bool(channel.accepts_newcomers),
            "method": _method_value(channel.method),
        })

    forms = db.scalars(select(SubmissionForm).where(SubmissionForm.station_id == station.id)).all()
    channel_urls = {str(c.url or "").rstrip("/") for c in channels if c.url}
    for form in forms:
        if form.url.rstrip("/") in channel_urls:
            continue
        score, route_type, reason = _score_form(form)
        candidates.append({
            "kind": "submission_form",
            "id": form.id,
            "route_type": route_type,
            "url": form.url,
            "email": None,
            "confidence": round(score, 3),
            "reason": reason,
            "manual_confirmed": False,
            "accepts_newcomers": True,
            "method": "form",
        })

    candidates = [c for c in candidates if c.get("url") or c.get("email")]
    if not candidates:
        return None

    route_priority = {
        "direct_music_form": 5,
        "gated_music_form": 4,
        "explicit_submission_email": 3,
        "music_director_contact": 2,
        "contact_email_only": 1,
        "contact_form_only": 1,
    }
    candidates.sort(
        key=lambda item: (
            float(item["confidence"]),
            bool(item.get("manual_confirmed")),
            route_priority.get(str(item.get("route_type")), 0),
        ),
        reverse=True,
    )
    primary = candidates[0]
    return RouteChoice(
        route_type=str(primary["route_type"]),
        url=str(primary["url"]) if primary.get("url") else None,
        email=str(primary["email"]) if primary.get("email") else None,
        confidence=float(primary["confidence"]),
        reason=str(primary["reason"]),
        secondary=candidates[1:8],
    )


def apply_primary_submission_route(db: Session, station: Station) -> bool:
    choice = choose_primary_submission_route(db, station)
    old = (
        station.best_submission_route_type,
        station.best_submission_route_url,
        station.best_submission_route_email,
        station.best_submission_route_confidence,
        station.best_submission_route_reason,
        station.secondary_submission_routes_json,
    )
    if choice is None:
        station.best_submission_route_type = None
        station.best_submission_route_url = None
        station.best_submission_route_email = None
        station.best_submission_route_confidence = None
        station.best_submission_route_reason = None
        station.secondary_submission_routes_json = "[]"
    else:
        station.best_submission_route_type = choice.route_type
        station.best_submission_route_url = choice.url
        station.best_submission_route_email = choice.email
        station.best_submission_route_confidence = choice.confidence
        station.best_submission_route_reason = choice.reason
        station.best_submission_route_updated_at = datetime.utcnow()
        station.secondary_submission_routes_json = json.dumps(choice.secondary, ensure_ascii=False)
    new = (
        station.best_submission_route_type,
        station.best_submission_route_url,
        station.best_submission_route_email,
        station.best_submission_route_confidence,
        station.best_submission_route_reason,
        station.secondary_submission_routes_json,
    )
    return old != new


def backfill_primary_submission_routes(db: Session, limit: int | None = None) -> dict[str, int]:
    stmt = select(Station).where(
        Station.submissions.any() | Station.forms.any()
    ).order_by(Station.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    total = 0
    changed = 0
    for station in db.scalars(stmt).all():
        total += 1
        if apply_primary_submission_route(db, station):
            changed += 1
    db.commit()
    return {"processed": total, "changed": changed}

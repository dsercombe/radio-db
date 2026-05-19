from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from radio_db.config import settings
from radio_db.connectors.gemini_generate import gemini_generate_content_json
from radio_db.models.entities import ContactDraft, ContactSend, OutreachCampaign, OutreachLinkClick, Station, StationSubmissionAssessment
from radio_db.services.budget import CostGuard


def _first_language(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "en"
    for separator in (",", "/", ";"):
        raw = raw.replace(separator, " ")
    token = raw.split()[0].strip()
    return token or "en"


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("outreach_empty_llm_response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    raise RuntimeError("outreach_invalid_llm_json")


def _estimate_gemini_usd(prompt: str) -> float:
    input_tokens = CostGuard.estimate_input_tokens(prompt)
    output_tokens = int(settings.outreach_llm_estimated_output_tokens)
    return (
        (input_tokens / 1_000_000) * float(settings.outreach_llm_gemini_input_price_per_1m)
        + (output_tokens / 1_000_000) * float(settings.outreach_llm_gemini_output_price_per_1m)
    )


def _editorial_context(session: Session, station_id: int) -> dict[str, Any]:
    row = session.scalar(
        select(StationSubmissionAssessment)
        .where(
            StationSubmissionAssessment.station_id == station_id,
            StationSubmissionAssessment.assessment_kind == settings.editorial_enrichment_assessment_kind,
        )
        .order_by(StationSubmissionAssessment.updated_at.desc())
        .limit(1)
    )
    if row is None or not row.evidence_json:
        return {}
    try:
        ev = json.loads(row.evidence_json)
        return ev if isinstance(ev, dict) else {}
    except Exception:
        return {}


def _clean_press_release_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if not raw.startswith(("https://", "http://")):
        raw = f"https://{raw}"
    return raw[:2048]


def _clean_tracking_code(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    code = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_")
    return code[:120] or None


def build_press_release_tracking_url(campaign_id: int, station_id: int | None = None) -> str:
    base = (settings.outreach_tracking_base_url or "http://radio.public-air.net").strip().rstrip("/")
    query = urlencode({"station_id": int(station_id)}) if station_id is not None else ""
    suffix = f"?{query}" if query else ""
    return f"{base}/api/v1/outreach-campaigns/{int(campaign_id)}/press-release-click{suffix}"


def build_short_press_release_tracking_url(
    campaign: OutreachCampaign,
    *,
    station_id: int | None = None,
) -> str:
    base = (settings.outreach_tracking_base_url or "http://radio.public-air.net").strip().rstrip("/")
    code = _clean_tracking_code(campaign.tracking_code) or f"c{int(campaign.id)}"
    query = urlencode({"s": int(station_id)}) if station_id is not None else ""
    suffix = f"?{query}" if query else ""
    return f"{base}/{code}{suffix}"


def build_press_release_email_url(
    url: str | None,
    *,
    campaign_id: int,
    station_id: int | None = None,
) -> str | None:
    clean_url = _clean_press_release_url(url)
    if not clean_url:
        return None
    parsed = urlparse(clean_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("utm_source", "radio_db")
    query.setdefault("utm_medium", "email")
    query.setdefault("utm_campaign", f"campaign_{int(campaign_id)}")
    if station_id is not None:
        query.setdefault("utm_content", f"station_{int(station_id)}")
    return urlunparse(parsed._replace(query=urlencode(query)))


def record_press_release_click(
    session: Session,
    *,
    campaign_id: int,
    station_id: int | None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    referer: str | None = None,
) -> str | None:
    campaign = session.get(OutreachCampaign, campaign_id)
    if campaign is None:
        return None
    target_url = _clean_press_release_url(campaign.press_release_url)
    if not target_url:
        return None
    session.add(
        OutreachLinkClick(
            campaign_id=campaign.id,
            station_id=station_id,
            target_url=target_url,
            user_agent=(user_agent or "")[:1000] or None,
            ip_address=(ip_address or "")[:128] or None,
            referer=(referer or "")[:2048] or None,
        )
    )
    session.commit()
    return target_url


def record_press_release_click_by_code(
    session: Session,
    *,
    tracking_code: str,
    station_id: int | None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    referer: str | None = None,
) -> str | None:
    code = _clean_tracking_code(tracking_code)
    if not code:
        return None
    campaign = session.scalar(select(OutreachCampaign).where(OutreachCampaign.tracking_code == code).limit(1))
    if campaign is None and code.startswith("c") and code[1:].isdigit():
        campaign = session.get(OutreachCampaign, int(code[1:]))
    if campaign is None:
        return None
    return record_press_release_click(
        session,
        campaign_id=campaign.id,
        station_id=station_id,
        user_agent=user_agent,
        ip_address=ip_address,
        referer=referer,
    )


def campaign_click_stats(session: Session, campaign_id: int) -> dict[str, Any]:
    row = session.execute(
        select(func.count(OutreachLinkClick.id), func.max(OutreachLinkClick.created_at)).where(
            OutreachLinkClick.campaign_id == campaign_id
        )
    ).one()
    return {"press_release_click_count": int(row[0] or 0), "press_release_last_clicked_at": row[1]}


def campaign_monitor(session: Session, campaign_id: int) -> dict[str, Any] | None:
    campaign = session.get(OutreachCampaign, campaign_id)
    if campaign is None:
        return None

    drafts = session.scalars(
        select(ContactDraft).where(ContactDraft.campaign_id == campaign_id).order_by(ContactDraft.updated_at.desc())
    ).all()
    draft_by_station: dict[int, ContactDraft] = {}
    for draft in drafts:
        current = draft_by_station.get(draft.station_id)
        if current is None or draft.updated_at > current.updated_at:
            draft_by_station[draft.station_id] = draft

    draft_ids = [draft.id for draft in drafts]
    sends: list[ContactSend] = []
    if draft_ids:
        sends = list(
            session.scalars(
                select(ContactSend)
                .where(ContactSend.draft_id.in_(draft_ids))
                .order_by(ContactSend.created_at.desc(), ContactSend.id.desc())
            ).all()
        )
    send_by_station: dict[int, ContactSend] = {}
    send_counts: dict[int, int] = {}
    for send in sends:
        send_counts[send.station_id] = send_counts.get(send.station_id, 0) + 1
        current = send_by_station.get(send.station_id)
        if current is None or send.created_at > current.created_at:
            send_by_station[send.station_id] = send

    click_rows = session.execute(
        select(
            OutreachLinkClick.station_id,
            func.count(OutreachLinkClick.id),
            func.max(OutreachLinkClick.created_at),
        )
        .where(OutreachLinkClick.campaign_id == campaign_id)
        .group_by(OutreachLinkClick.station_id)
    ).all()
    clicks_by_station: dict[int, dict[str, Any]] = {}
    unknown_click_count = 0
    unknown_last_clicked_at = None
    for station_id, click_count, last_clicked_at in click_rows:
        if station_id is None:
            unknown_click_count += int(click_count or 0)
            unknown_last_clicked_at = max(
                [dt for dt in (unknown_last_clicked_at, last_clicked_at) if dt is not None],
                default=None,
            )
            continue
        clicks_by_station[int(station_id)] = {
            "click_count": int(click_count or 0),
            "last_clicked_at": last_clicked_at,
        }

    station_ids = set(draft_by_station) | set(send_by_station) | set(clicks_by_station)
    stations = {}
    if station_ids:
        stations = {
            station.id: station
            for station in session.scalars(select(Station).where(Station.id.in_(station_ids))).all()
        }

    items: list[dict[str, Any]] = []
    summary = {
        "total": 0,
        "not_started": 0,
        "drafted": 0,
        "sent": 0,
        "clicked": 0,
        "failed": 0,
        "blocked": 0,
        "simulated": 0,
        "unknown_clicks": unknown_click_count,
    }

    for station_id in sorted(station_ids, key=lambda sid: (stations.get(sid).canonical_name if stations.get(sid) else str(sid)).lower()):
        station = stations.get(station_id)
        draft = draft_by_station.get(station_id)
        send = send_by_station.get(station_id)
        click = clicks_by_station.get(station_id, {"click_count": 0, "last_clicked_at": None})
        click_count = int(click["click_count"] or 0)
        send_status = send.status if send is not None else None
        if click_count > 0:
            monitor_status = "clicked"
        elif send_status == "sent":
            monitor_status = "sent"
        elif send_status == "failed":
            monitor_status = "failed"
        elif send_status == "blocked":
            monitor_status = "blocked"
        elif send_status == "simulated":
            monitor_status = "simulated"
        elif draft is not None:
            monitor_status = "drafted"
        else:
            monitor_status = "not_started"

        summary["total"] += 1
        summary[monitor_status] = int(summary.get(monitor_status, 0)) + 1
        items.append(
            {
                "station_id": station_id,
                "station_name": station.canonical_name if station is not None else f"Station {station_id}",
                "country_code": station.country_code if station is not None else "",
                "language": station.language if station is not None else "",
                "draft_id": draft.id if draft is not None else None,
                "draft_status": draft.status if draft is not None else None,
                "send_id": send.id if send is not None else None,
                "send_status": send_status,
                "send_channel": send.channel if send is not None else None,
                "send_target": send.target_value if send is not None else None,
                "send_count": int(send_counts.get(station_id, 0)),
                "last_sent_at": send.created_at if send is not None else None,
                "click_count": click_count,
                "last_clicked_at": click["last_clicked_at"],
                "tracking_url": build_short_press_release_tracking_url(campaign, station_id=station_id)
                if campaign.press_release_url
                else None,
                "monitor_status": monitor_status,
            }
        )

    return {"summary": summary, "items": items}


def list_outreach_campaigns(session: Session, *, active_only: bool = False) -> list[OutreachCampaign]:
    q = select(OutreachCampaign).order_by(OutreachCampaign.updated_at.desc(), OutreachCampaign.id.desc())
    if active_only:
        q = q.where(OutreachCampaign.is_active.is_(True))
    return list(session.scalars(q).all())


def get_outreach_campaign(session: Session, campaign_id: int) -> OutreachCampaign | None:
    return session.get(OutreachCampaign, campaign_id)


def create_outreach_campaign(
    session: Session,
    *,
    name: str,
    artist_name: str,
    song_title: str,
    release_date: str | None = None,
    song_language: str | None = None,
    pitch_text: str = "",
    reference_template: str = "",
    operator_notes: str | None = None,
    press_release_url: str | None = None,
    tracking_code: str | None = None,
    is_active: bool = True,
) -> OutreachCampaign:
    row = OutreachCampaign(
        name=name.strip(),
        artist_name=artist_name.strip(),
        song_title=song_title.strip(),
        release_date=(release_date.strip() if release_date else None) or None,
        song_language=(song_language.strip() if song_language else None) or None,
        pitch_text=pitch_text or "",
        reference_template=reference_template or "",
        operator_notes=(operator_notes.strip() if operator_notes else None) or None,
        press_release_url=_clean_press_release_url(press_release_url),
        tracking_code=_clean_tracking_code(tracking_code),
        is_active=is_active,
    )
    session.add(row)
    session.commit()
    if not row.tracking_code:
        row.tracking_code = f"c{row.id}"
        session.commit()
    session.refresh(row)
    return row


def patch_outreach_campaign(session: Session, campaign_id: int, patch: dict[str, Any]) -> OutreachCampaign | None:
    row = session.get(OutreachCampaign, campaign_id)
    if row is None:
        return None
    if "name" in patch and patch["name"] is not None:
        row.name = str(patch["name"]).strip()
    if "artist_name" in patch and patch["artist_name"] is not None:
        row.artist_name = str(patch["artist_name"]).strip()
    if "song_title" in patch and patch["song_title"] is not None:
        row.song_title = str(patch["song_title"]).strip()
    if "release_date" in patch:
        v = patch["release_date"]
        row.release_date = (str(v).strip() if v else None) or None
    if "song_language" in patch:
        v = patch["song_language"]
        row.song_language = (str(v).strip() if v else None) or None
    if "pitch_text" in patch and patch["pitch_text"] is not None:
        row.pitch_text = str(patch["pitch_text"])
    if "reference_template" in patch and patch["reference_template"] is not None:
        row.reference_template = str(patch["reference_template"])
    if "operator_notes" in patch:
        v = patch["operator_notes"]
        row.operator_notes = (str(v).strip() if v else None) or None
    if "press_release_url" in patch:
        row.press_release_url = _clean_press_release_url(patch["press_release_url"])
    if "tracking_code" in patch:
        row.tracking_code = _clean_tracking_code(patch["tracking_code"]) or f"c{row.id}"
    if "is_active" in patch and patch["is_active"] is not None:
        row.is_active = bool(patch["is_active"])
    session.commit()
    session.refresh(row)
    return row


def archive_outreach_campaign(session: Session, campaign_id: int) -> OutreachCampaign | None:
    row = session.get(OutreachCampaign, campaign_id)
    if row is None:
        return None
    row.is_active = False
    session.commit()
    session.refresh(row)
    return row


def _call_gemini_outreach(prompt: str) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    base_url = (settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = (settings.outreach_llm_gemini_model or "gemini-2.5-flash-lite").strip()
    url = f"{base_url}/models/{model}:generateContent"
    params = {"key": settings.gemini_api_key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": int(settings.outreach_llm_gemini_max_output_tokens),
            "responseMimeType": "application/json",
        },
    }
    data = gemini_generate_content_json(
        url=url,
        params=params,
        json_payload=payload,
        wall_timeout_per_attempt=float(settings.gemini_http_wall_timeout_seconds),
        connect_timeout=float(settings.gemini_http_connect_timeout_seconds),
        read_timeout=float(settings.gemini_http_read_timeout_seconds),
    )
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("gemini_no_candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
    return _extract_json_object("\n".join(text_parts).strip())


def generate_outreach_email(session: Session, *, campaign_id: int, station_id: int) -> dict[str, str]:
    campaign = session.get(OutreachCampaign, campaign_id)
    if campaign is None:
        raise ValueError("campaign_not_found")
    station = session.scalar(
        select(Station).options(joinedload(Station.genres)).where(Station.id == station_id)
    )
    if station is None:
        raise ValueError("station_not_found")

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "outreach_llm_budget.json",
        max_llm_calls_per_day=int(settings.outreach_llm_max_calls_per_day),
        max_daily_usd=float(settings.outreach_llm_max_daily_usd),
        input_price_per_1m=float(settings.outreach_llm_gemini_input_price_per_1m),
        output_price_per_1m=float(settings.outreach_llm_gemini_output_price_per_1m),
    )

    station_lang = _first_language(station.language)
    editorial = _editorial_context(session, station_id)
    genres = [g.genre for g in (station.genres or [])][:8]

    campaign_pkg = {
        "name": campaign.name,
        "artist_name": campaign.artist_name,
        "song_title": campaign.song_title,
        "release_date": campaign.release_date,
        "song_language_hint": campaign.song_language,
        "pitch_text": campaign.pitch_text,
        "reference_template": campaign.reference_template,
        "operator_notes": campaign.operator_notes,
        "press_release_url": campaign.press_release_url,
        "press_release_email_url": build_press_release_email_url(
            campaign.press_release_url,
            campaign_id=campaign.id,
            station_id=station_id,
        ),
        "press_release_short_tracking_url": (
            build_short_press_release_tracking_url(campaign, station_id=station_id)
            if campaign.press_release_url
            else None
        ),
        "press_release_tracking_url": (
            build_press_release_tracking_url(campaign.id, station_id) if campaign.press_release_url else None
        ),
    }
    station_pkg = {
        "station_name": station.canonical_name,
        "country_code": station.country_code,
        "city": station.city,
        "station_language": station.language,
        "primary_language_code": station_lang,
        "genres": genres,
        "website_url": station.website_url,
        "editorial_summary_short": editorial.get("editorial_summary_short"),
        "editorial_format": editorial.get("editorial_format"),
        "pitch_angle_hint": editorial.get("pitch_angle_hint"),
        "style_tags": editorial.get("style_tags"),
    }

    prompt = (
        "You write personal radio outreach emails pitching ONE song to a radio station.\n"
        "Return STRICT JSON with keys:\n"
        "subject (string),\n"
        "body (string, plain text, suitable for email),\n"
        "subject_en (string),\n"
        "body_en (string).\n\n"
        "Language rules:\n"
        "- Write subject and body in the station's primary language "
        f"('{station_lang}', ISO-style code derived from the station record). "
        "If that code is unclear, use English for subject/body.\n"
        "- subject_en and body_en must be faithful English translations of subject and body "
        "(same structure and intent).\n\n"
        "Content rules:\n"
        "- Mention the station by name naturally once.\n"
        "- Reference artist and song from CAMPAIGN; keep tone professional and warm.\n"
        "- If CAMPAIGN.reference_template is non-empty, treat it as the primary writing example.\n"
        "- Preserve the reference_template's human rhythm, paragraph shape, sign-off style, and level of warmth.\n"
        "- Replace placeholders like XXX with real campaign and station-specific facts.\n"
        "- Do not copy any reference_template wording that would mismatch the real facts.\n"
        "- Avoid generic phrases like 'excited to share', 'fans will appreciate', or 'thanks for your consideration' unless the template uses that tone.\n"
        "- Do not invent streaming URLs, attachments, or guarantees.\n"
        "- Keep the body compact, but template fidelity is more important than forcing it under an exact word count.\n"
        "- If CAMPAIGN.press_release_short_tracking_url is present, include it once as the central call-to-action.\n"
        "- Use the short tracking URL for the press release link, not the long original URL.\n"
        "- If the press release URL is present, do not list attachments or duplicate long background details.\n\n"
        f"CAMPAIGN:\n{json.dumps(campaign_pkg, ensure_ascii=False)}\n\n"
        f"STATION:\n{json.dumps(station_pkg, ensure_ascii=False)}\n"
    )

    estimated = max(0.00001, _estimate_gemini_usd(prompt))
    if not budget.can_call_llm_today() or not budget.can_spend(estimated):
        raise RuntimeError("outreach_llm_budget_exceeded")

    verdict = _call_gemini_outreach(prompt)
    subject = str(verdict.get("subject") or "").strip()
    body = str(verdict.get("body") or "").strip()
    subject_en = str(verdict.get("subject_en") or "").strip()
    body_en = str(verdict.get("body_en") or "").strip()
    if not subject or not body or not subject_en or not body_en:
        raise RuntimeError("outreach_llm_incomplete_fields")

    budget.register_call(estimated)
    return {"subject": subject, "body": body, "subject_en": subject_en, "body_en": body_en}

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.gemini_generate import gemini_generate_content_json
from radio_db.models.entities import Station, StationStatus, StationSubmissionAssessment
from radio_db.services.budget import CostGuard
from radio_db.services.station_quality import _build_station_features


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("editorial_empty_response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    raise RuntimeError("editorial_invalid_json")


def _estimate_call_usd(prompt: str, provider: str) -> float:
    input_tokens = max(1, int(len(prompt) / 3.5))
    output_tokens = int(settings.editorial_enrichment_estimated_output_tokens)
    mode = (provider or "").strip().lower()
    if mode == "gemini":
        return (
            (input_tokens / 1_000_000) * float(settings.editorial_enrichment_gemini_input_price_per_1m)
            + (output_tokens / 1_000_000) * float(settings.editorial_enrichment_gemini_output_price_per_1m)
        )
    return (
        (input_tokens / 1_000_000) * float(settings.openai_input_price_per_1m)
        + (output_tokens / 1_000_000) * float(settings.openai_output_price_per_1m)
    )


def _build_prompt(features: dict[str, Any]) -> str:
    compact = {
        "station_id": features.get("station_id"),
        "status": features.get("status"),
        "country_code": features.get("country_code"),
        "language": features.get("language"),
        "website_domain": features.get("website_domain"),
        "website_path": features.get("website_path"),
        "station_confidence_score": features.get("station_confidence_score"),
        "genre_samples": features.get("genre_samples", [])[:8],
        "submission_channel_form_count": features.get("submission_channel_form_count"),
        "submission_channel_email_count": features.get("submission_channel_email_count"),
        "contact_total": features.get("contact_total"),
        "contact_with_email_count": features.get("contact_with_email_count"),
        "forms_total": features.get("forms_total"),
        "active_music_form_count": features.get("active_music_form_count"),
        "gated_music_form_count": features.get("gated_music_form_count"),
        "newcomer_signal": features.get("newcomer_signal"),
        "playwright_interesting_url_samples": features.get("playwright_interesting_url_samples", [])[:5],
        "playwright_email_samples": features.get("playwright_email_samples", [])[:5],
    }
    return (
        "You are enriching a verified radio-station record for music campaign selection.\n"
        "Do not re-judge whether it should be accepted or rejected.\n"
        "Based on the FEATURES JSON only, return STRICT JSON with keys:\n"
        "editorial_summary_short (string), editorial_format (string), style_tags (array of strings),\n"
        "campaign_fit_tags (array of strings), pitch_angle_hint (string), confidence (float 0-1).\n"
        "Rules:\n"
        "- Keep editorial_summary_short under 240 chars.\n"
        "- editorial_format should be a short label like 'regional CHR', 'community', 'college', 'specialist', 'adult contemporary', 'public culture'.\n"
        "- style_tags and campaign_fit_tags should each contain 0-6 short lowercase tags.\n"
        "- pitch_angle_hint should be one short practical outreach hint.\n"
        "- Focus on editorial fit, likely style, and outreach framing for music pitching.\n"
        "- Do not include any promote/reject/review decision.\n"
        f"\nFEATURES:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def _call_openai_editorial(prompt: str) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing")
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "editorial_summary_short": {"type": "string"},
            "editorial_format": {"type": "string"},
            "style_tags": {"type": "array", "items": {"type": "string"}},
            "campaign_fit_tags": {"type": "array", "items": {"type": "string"}},
            "pitch_angle_hint": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "editorial_summary_short",
            "editorial_format",
            "style_tags",
            "campaign_fit_tags",
            "pitch_angle_hint",
            "confidence",
        ],
        "additionalProperties": False,
    }
    response = client.responses.create(
        model=settings.editorial_enrichment_openai_model,
        input=[{"role": "user", "content": prompt}],
        text={"format": {"type": "json_schema", "name": "editorial_fit_profile", "schema": schema, "strict": True}},
    )
    return _extract_json_object(response.output_text)


def _call_gemini_editorial(prompt: str) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    base_url = (settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = (settings.editorial_enrichment_gemini_model or "gemini-2.5-flash-lite").strip()
    url = f"{base_url}/models/{model}:generateContent"
    params = {"key": settings.gemini_api_key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": int(settings.editorial_enrichment_gemini_max_output_tokens),
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


def _call_editorial(provider: str, prompt: str) -> dict[str, Any]:
    mode = (provider or "").strip().lower()
    if mode == "gemini":
        return _call_gemini_editorial(prompt)
    if mode == "openai":
        return _call_openai_editorial(prompt)
    raise RuntimeError(f"unsupported_provider={provider}")


def _persist_editorial_assessment(
    session: Session,
    station: Station,
    features: dict[str, Any],
    verdict: dict[str, Any],
    provider: str,
) -> StationSubmissionAssessment:
    kind = settings.editorial_enrichment_assessment_kind
    row = session.scalar(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == station.id, StationSubmissionAssessment.assessment_kind == kind)
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )
    if row is None:
        row = StationSubmissionAssessment(station_id=station.id, assessment_kind=kind, status="active")
        session.add(row)
        session.flush()

    editorial_summary_short = str(verdict.get("editorial_summary_short") or "").strip()[:240]
    editorial_format = str(verdict.get("editorial_format") or "").strip()[:80]
    pitch_angle_hint = str(verdict.get("pitch_angle_hint") or "").strip()[:220]
    confidence = max(0.0, min(1.0, float(verdict.get("confidence", 0.5) or 0.5)))
    style_tags = [str(item).strip().lower()[:40] for item in (verdict.get("style_tags") or []) if str(item).strip()][:6]
    campaign_fit_tags = [
        str(item).strip().lower()[:40] for item in (verdict.get("campaign_fit_tags") or []) if str(item).strip()
    ][:6]

    row.status = "active"
    row.is_real_station = True
    row.has_real_editorial_surface = bool(editorial_summary_short or editorial_format or style_tags)
    row.accepts_music_submissions = bool(
        features.get("submission_channel_total") or features.get("active_music_form_count") or features.get("submission_channel_email_count")
    )
    row.accepts_new_artists = bool(features.get("newcomer_signal"))
    row.automation_readiness = max(0.0, min(1.0, float(features.get("active_music_form_count", 0) > 0)))
    row.risk_score = max(0.0, min(1.0, 1.0 - confidence))
    row.notes = editorial_summary_short or None
    row.evidence_json = json.dumps(
        {
            "provider": provider,
            "editorial_summary_short": editorial_summary_short,
            "editorial_format": editorial_format,
            "style_tags": style_tags,
            "campaign_fit_tags": campaign_fit_tags,
            "pitch_angle_hint": pitch_angle_hint,
            "confidence": confidence,
        },
        ensure_ascii=False,
    )
    session.add(row)
    session.flush()
    return row


def run_station_editorial_enrichment(
    session: Session,
    limit: int = 25,
    station_id: int | None = None,
    provider: str | None = None,
    only_unassessed: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    chosen_provider = (provider or settings.editorial_enrichment_provider or "gemini").strip().lower()
    if chosen_provider not in {"gemini", "openai"}:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": f"unsupported_provider={chosen_provider}"}
    if chosen_provider == "gemini" and not settings.gemini_api_key:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "GEMINI_API_KEY missing"}
    if chosen_provider == "openai" and not settings.openai_api_key:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "OPENAI_API_KEY missing"}

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "editorial_enrichment_budget.json",
        max_llm_calls_per_day=settings.editorial_enrichment_max_calls_per_day,
        max_daily_usd=settings.editorial_enrichment_max_daily_usd,
        input_price_per_1m=settings.editorial_enrichment_gemini_input_price_per_1m,
        output_price_per_1m=settings.editorial_enrichment_gemini_output_price_per_1m,
    )

    q = select(Station).where(Station.status == StationStatus.VERIFIED, Station.website_url.is_not(None))
    if station_id is not None:
        q = q.where(Station.id == station_id)
    if only_unassessed:
        assessed_ids = select(StationSubmissionAssessment.station_id).where(
            StationSubmissionAssessment.assessment_kind == settings.editorial_enrichment_assessment_kind
        )
        q = q.where(Station.id.not_in(assessed_ids))
    q = q.order_by(Station.confidence_score.desc(), Station.id.asc())
    stations = session.scalars(q.limit(max(1, limit))).all()
    if not stations:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "no_candidates"}

    processed = 0
    saved = 0
    errors = 0
    skipped_budget = 0
    samples: list[dict[str, Any]] = []

    for station in stations:
        features = _build_station_features(session=session, station=station, include_deep_pass=False)
        prompt = _build_prompt(features)
        estimated_usd = max(0.00001, _estimate_call_usd(prompt, provider=chosen_provider))
        if not budget.can_call_llm_today() or not budget.can_spend(estimated_usd):
            skipped_budget += 1
            continue
        processed += 1
        try:
            verdict = _call_editorial(provider=chosen_provider, prompt=prompt)
            if apply:
                _persist_editorial_assessment(
                    session=session,
                    station=station,
                    features=features,
                    verdict=verdict,
                    provider=chosen_provider,
                )
                session.commit()
                budget.register_call(estimated_usd)
                saved += 1
            if len(samples) < 10:
                samples.append(
                    {
                        "station_id": int(station.id),
                        "name": station.canonical_name,
                        "editorial_format": str(verdict.get("editorial_format") or ""),
                        "summary": str(verdict.get("editorial_summary_short") or "")[:120],
                    }
                )
        except Exception as exc:
            errors += 1
            session.rollback()
            if len(samples) < 10:
                samples.append(
                    {
                        "station_id": int(station.id),
                        "name": station.canonical_name,
                        "error": str(exc)[:180],
                    }
                )

    return {
        "provider": chosen_provider,
        "assessment_kind": settings.editorial_enrichment_assessment_kind,
        "processed": processed,
        "saved": saved,
        "errors": errors,
        "skipped_budget": skipped_budget,
        "samples": samples,
        "apply": apply,
        "only_unassessed": only_unassessed,
    }

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text

from radio_db.db import SessionLocal, init_db
from radio_db.models.entities import Station, StationSubmissionAssessment
from radio_db.services.route_discovery import (
    ASSESSMENT_KIND,
    STRONG_ROUTE_TYPES,
    _guard_verdict_against_discovery,
    _is_generic_contact_email,
)


def _load_json(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _fallback_route_from_existing(station: Station, reason: str) -> dict[str, Any]:
    email = _clean_text(station.best_submission_route_email).lower()
    url = _clean_text(station.best_submission_route_url)
    if email:
        route_type = "contact_email_only" if _is_generic_contact_email(email) else "submission_page_signal"
        return {
            "route_type": route_type,
            "url": url,
            "email": email,
            "confidence": min(float(station.best_submission_route_confidence or 0.45), 0.5),
            "reason": reason,
        }
    if url:
        return {
            "route_type": "submission_page_signal",
            "url": url,
            "email": "",
            "confidence": min(float(station.best_submission_route_confidence or 0.45), 0.5),
            "reason": reason,
        }
    return {"route_type": "none", "confidence": 0.0, "reason": reason}


def _apply_route(station: Station, route: dict[str, Any], reason: str) -> None:
    route_type = _clean_text(route.get("route_type"))
    if route_type == "none":
        station.best_submission_route_type = None
        station.best_submission_route_url = None
        station.best_submission_route_email = None
        station.best_submission_route_confidence = None
    else:
        station.best_submission_route_type = route_type
        station.best_submission_route_url = _clean_text(route.get("url"))[:1024] or None
        station.best_submission_route_email = _clean_text(route.get("email")).lower()[:320] or None
        station.best_submission_route_confidence = float(route.get("confidence") or 0.45)
    station.best_submission_route_reason = reason[:500]
    station.best_submission_route_updated_at = datetime.utcnow()


def _reclassify_phase2_assessment(station: Station, assessment: StationSubmissionAssessment) -> tuple[bool, str, str]:
    payload = _load_json(assessment.evidence_json)
    discovery = payload.get("discovery") if isinstance(payload.get("discovery"), dict) else {}
    candidates = [c for c in discovery.get("route_candidates") or [] if isinstance(c, dict)]
    old_route_type = _clean_text(station.best_submission_route_type)
    if old_route_type not in STRONG_ROUTE_TYPES:
        return False, old_route_type, old_route_type
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    if not verdict:
        best = payload.get("best_submission_route") if isinstance(payload.get("best_submission_route"), dict) else {}
        verdict = {
            "decision": payload.get("decision") or "review",
            "quality_score": payload.get("quality_score") or 50,
            "confidence": payload.get("confidence") or station.best_submission_route_confidence or 0.5,
            "outreach_bucket": payload.get("outreach_bucket") or "needs_review",
            "best_route": best,
            "secondary_routes": payload.get("secondary_submission_routes") or [],
        }
    guarded = _guard_verdict_against_discovery(verdict, discovery, candidates)
    new_route = guarded.get("best_route") if isinstance(guarded.get("best_route"), dict) else {}
    new_type = _clean_text(new_route.get("route_type"))
    if new_type == old_route_type:
        return False, old_route_type, new_type
    reason = _clean_text(guarded.get("manual_review_reason")) or "Downgraded by stricter Phase-2 route guardrails."
    _apply_route(station, new_route, reason)
    payload["reclassified_at"] = datetime.utcnow().isoformat() + "Z"
    payload["reclassification_reason"] = reason
    payload["submission_path_quality"] = new_type
    payload["best_submission_route"] = new_route
    payload["outreach_bucket"] = guarded.get("outreach_bucket")
    payload["decision"] = guarded.get("decision")
    assessment.evidence_json = json.dumps(payload, ensure_ascii=False)
    assessment.notes = reason[:500]
    assessment.accepts_music_submissions = new_type in {"direct_music_form", "gated_music_form", "explicit_submission_email"}
    assessment.risk_score = max(float(assessment.risk_score or 0.0), 0.55)
    return True, old_route_type, new_type


def _reclassify_legacy_obvious(station: Station) -> tuple[bool, str, str]:
    old_type = _clean_text(station.best_submission_route_type)
    if old_type not in STRONG_ROUTE_TYPES:
        return False, old_type, old_type
    reason = _clean_text(station.best_submission_route_reason).lower()
    downgrade_reason = ""
    if "auto_discovered_from_page" in reason:
        downgrade_reason = "Downgraded: auto-discovered email is not counted as verified without current page evidence."
    elif old_type in {"direct_music_form", "gated_music_form"}:
        url = _clean_text(station.best_submission_route_url).lower().rstrip("/")
        website = _clean_text(station.website_url).lower().rstrip("/")
        if url and website and url in {website, website + "/"}:
            downgrade_reason = "Downgraded: homepage/contact form signal is not counted as a verified music submission form."
    if not downgrade_reason:
        return False, old_type, old_type
    route = _fallback_route_from_existing(station, downgrade_reason)
    new_type = _clean_text(route.get("route_type")) or "none"
    _apply_route(station, route, downgrade_reason)
    return True, old_type, new_type


def main() -> None:
    init_db()
    with SessionLocal() as session:
        latest_phase2 = (
            select(func.max(StationSubmissionAssessment.id))
            .where(
                StationSubmissionAssessment.station_id == Station.id,
                StationSubmissionAssessment.assessment_kind == ASSESSMENT_KIND,
            )
            .correlate(Station)
            .scalar_subquery()
        )
        rows = (
            session.execute(
                select(Station, StationSubmissionAssessment)
                .outerjoin(StationSubmissionAssessment, StationSubmissionAssessment.id == latest_phase2)
                .where(Station.best_submission_route_type.in_(tuple(STRONG_ROUTE_TYPES)))
                .order_by(Station.id)
            )
            .unique()
            .all()
        )
        before = session.execute(
            text(
                """
                SELECT coalesce(best_submission_route_type, 'none') AS route_type, count(*) AS n
                FROM stations
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings().all()
        changed: dict[str, int] = {}
        changed_rows: list[dict[str, Any]] = []
        for station, assessment in rows:
            did_change = False
            old_type = _clean_text(station.best_submission_route_type)
            new_type = old_type
            if assessment is not None:
                did_change, old_type, new_type = _reclassify_phase2_assessment(station, assessment)
            if not did_change:
                did_change, old_type, new_type = _reclassify_legacy_obvious(station)
            if did_change:
                key = f"{old_type}->{new_type or 'none'}"
                changed[key] = changed.get(key, 0) + 1
                changed_rows.append(
                    {
                        "station_id": station.id,
                        "name": station.canonical_name,
                        "from": old_type,
                        "to": new_type,
                    }
                )
        session.execute(
            text(
                """
                UPDATE phase2_route_discovery_queue q
                SET route_type = s.best_submission_route_type,
                    updated_at = now()
                FROM stations s
                WHERE q.station_id = s.id
                  AND q.status = 'done'
                """
            )
        )
        session.commit()
        after = session.execute(
            text(
                """
                SELECT coalesce(best_submission_route_type, 'none') AS route_type, count(*) AS n
                FROM stations
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings().all()
    print(
        json.dumps(
            {
                "checked_strong_routes": len(rows),
                "changed_total": len(changed_rows),
                "changed_by_transition": changed,
                "sample_changes": changed_rows[:30],
                "before": {str(row["route_type"]): int(row["n"]) for row in before},
                "after": {str(row["route_type"]): int(row["n"]) for row in after},
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

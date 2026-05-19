from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text

from radio_db.connectors.brave import search_web
from radio_db.db import SessionLocal, init_db
from radio_db.models.entities import Station
from radio_db.services.phase2_route_queue import ensure_phase2_route_queue
from radio_db.services.route_discovery import (
    _build_brave_prompts,
    _build_gemini_prompt,
    _build_station_features,
    _call_gemini_route_discovery,
    _candidate_urls_from_existing,
    _compact_text,
    _dedupe_urls,
    _emails,
    _guard_verdict_against_discovery,
    _has_any,
    _is_dedicated_submission_email,
    _is_generic_contact_email,
    _json_dumps,
    _normalize_verdict,
    _official_urls,
    _persist_route_discovery,
    _route_candidate,
    _safe_domain,
    _verify_url,
    ASSESSMENT_KIND,
    DIRECT_SUBMISSION_HINTS,
    MUSIC_DIRECTOR_HINTS,
)


def _claim_next_search(session, worker_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            WITH candidate AS (
                SELECT id
                FROM phase2_route_discovery_queue
                WHERE status = 'pending'
                  AND source_pool IN ('phase2_contact_only_rescan', 'phase2_needs_review_high_value')
                  AND NOT EXISTS (
                    SELECT 1 FROM station_submission_assessments a
                    WHERE a.station_id = phase2_route_discovery_queue.station_id
                      AND a.assessment_kind = 'route_discovery_v1'
                  )
                ORDER BY
                    CASE source_pool
                        WHEN 'phase2_contact_only_rescan' THEN 0
                        WHEN 'phase2_needs_review_high_value' THEN 1
                        ELSE 2
                    END,
                    priority_score DESC,
                    id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE phase2_route_discovery_queue q
            SET status = 'running',
                claim_token = :claim_token,
                claimed_at = now(),
                attempt_count = attempt_count + 1,
                updated_at = now()
            FROM candidate
            WHERE q.id = candidate.id
            RETURNING q.id, q.station_id, q.source_pool
            """
        ),
        {"claim_token": worker_id},
    ).mappings().first()
    session.commit()
    return dict(row) if row else None


def _search_result_text(row: dict[str, Any]) -> str:
    extra = row.get("extra_snippets") if isinstance(row.get("extra_snippets"), list) else []
    return _compact_text(
        " ".join(
            [
                str(row.get("title") or ""),
                str(row.get("url") or ""),
                str(row.get("description") or ""),
                " ".join(str(item) for item in extra),
            ]
        )
    )


def _run_station_with_brave_search(session, station_id: int, *, max_search_calls: int, max_pages: int) -> dict[str, Any]:
    station = session.get(Station, station_id)
    if station is None:
        return {"station_id": station_id, "status": "error", "error": "station_not_found"}
    old_features = _build_station_features(session=session, station=station, include_deep_pass=False)
    station_domain = _safe_domain(station.website_url)
    search_results: list[dict[str, Any]] = []
    answer_urls: list[str] = []
    answer_emails: list[str] = []
    prompts = _build_brave_prompts(station)[: max(0, max_search_calls)]
    for label, query in prompts:
        rows = search_web(query=query, count=8, country=station.country_code or None)
        content = "\n".join(_search_result_text(row) for row in rows)
        result = {
            "ok": bool(rows),
            "query_type": label,
            "provider": "brave_search",
            "content": content,
            "usage": {"results": len(rows)},
        }
        search_results.append(result)
        for row in rows:
            text = _search_result_text(row)
            answer_urls.extend(_official_urls(f"{row.get('url') or ''}\n{text}", station_domain))
            answer_emails.extend(_emails(text))
    existing_urls = _candidate_urls_from_existing(station, old_features)
    urls = _dedupe_urls(answer_urls + existing_urls, station, max_pages)
    verifications = [_verify_url(url, station) for url in urls]
    candidates: list[dict[str, Any]] = []
    for verification in verifications:
        candidates.extend([c for c in verification.get("candidates", []) if isinstance(c, dict)])
    answer_text = _compact_text(" ".join(str(item.get("content") or "") for item in search_results))
    has_answer_submission_context = _has_any(answer_text, DIRECT_SUBMISSION_HINTS) or _has_any(
        answer_text, MUSIC_DIRECTOR_HINTS
    )
    for email in answer_emails:
        email_domain = email.split("@", 1)[1] if "@" in email else ""
        if _safe_domain(email_domain) and _safe_domain(station_domain) and (
            email_domain == station_domain
            or email_domain.endswith("." + station_domain)
            or station_domain.endswith("." + email_domain)
        ):
            route_type = (
                "explicit_submission_email"
                if _is_dedicated_submission_email(email)
                and has_answer_submission_context
                and not _is_generic_contact_email(email)
                else "contact_email_only"
            )
            candidates.append(
                _route_candidate(
                    route_type=route_type,
                    source="brave_search",
                    email=email,
                    evidence_snippet=answer_text[:600] or "Email extracted from Brave Search result",
                    confidence=0.54 if route_type == "explicit_submission_email" else 0.36,
                )
            )
    candidates = sorted(
        {(_json_dumps({k: c.get(k) for k in ("route_type", "url", "email")})): c for c in candidates}.values(),
        key=lambda c: (int(c.get("score") or 0), float(c.get("confidence") or 0.0)),
        reverse=True,
    )
    discovery = {
        "brave_answers": search_results,
        "candidate_urls": urls,
        "url_verifications": verifications,
        "route_candidates": candidates[:12],
        "provider_override": "brave_search",
    }
    prompt = _build_gemini_prompt(station, old_features, discovery)
    try:
        verdict_raw = _call_gemini_route_discovery(prompt)
        verdict = _guard_verdict_against_discovery(_normalize_verdict(verdict_raw, candidates), discovery, candidates)
        gemini_error = ""
    except Exception as exc:
        verdict = _normalize_verdict({}, candidates)
        gemini_error = f"{exc.__class__.__name__}:{str(exc)[:220]}"
    assessment = _persist_route_discovery(
        session,
        station,
        old_features=old_features,
        discovery=discovery,
        verdict=verdict,
        apply=True,
    )
    session.commit()
    return {
        "station_id": station.id,
        "name": station.canonical_name,
        "status": "ok",
        "assessment_id": assessment.id,
        "provider": "brave_search",
        "search_calls": sum(1 for item in search_results if item.get("ok")),
        "verified_urls": len(verifications),
        "gemini_error": gemini_error,
        "verdict": {
            "decision": verdict.get("decision"),
            "quality_score": verdict.get("quality_score"),
            "confidence": verdict.get("confidence"),
            "outreach_bucket": verdict.get("outreach_bucket"),
            "best_route": verdict.get("best_route"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-search-calls", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=8)
    args = parser.parse_args()

    init_db()
    worker_id = args.worker_id or f"phase2-search-{uuid.uuid4().hex[:10]}"
    processed = 0
    errors = 0
    route_types: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    stop_reason = ""
    with SessionLocal() as session:
        ensure_phase2_route_queue(session)
        for _ in range(max(1, args.limit)):
            claim = _claim_next_search(session, worker_id)
            if not claim:
                stop_reason = "queue_empty"
                break
            station_id = int(claim["station_id"])
            try:
                result = _run_station_with_brave_search(
                    session,
                    station_id,
                    max_search_calls=max(1, args.max_search_calls),
                    max_pages=max(1, args.max_pages),
                )
                verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
                best_route = verdict.get("best_route") if isinstance(verdict.get("best_route"), dict) else {}
                route_type = str(best_route.get("route_type") or "unknown")
                session.execute(
                    text(
                        """
                        UPDATE phase2_route_discovery_queue
                        SET status = 'done',
                            route_type = :route_type,
                            result_json = :result_json,
                            last_error = :last_error,
                            updated_at = now(),
                            processed_at = now()
                        WHERE id = :id AND claim_token = :claim_token
                        """
                    ),
                    {
                        "id": int(claim["id"]),
                        "claim_token": worker_id,
                        "route_type": route_type,
                        "result_json": json.dumps(result, ensure_ascii=False),
                        "last_error": str(result.get("gemini_error") or result.get("error") or "")[:1000],
                    },
                )
                session.commit()
                processed += 1
                route_types[route_type] = route_types.get(route_type, 0) + 1
                events.append({"station_id": station_id, "source_pool": claim["source_pool"], "route_type": route_type})
            except Exception as exc:
                session.rollback()
                errors += 1
                session.execute(
                    text(
                        """
                        UPDATE phase2_route_discovery_queue
                        SET status = 'pending',
                            last_error = :error,
                            claim_token = NULL,
                            claimed_at = NULL,
                            updated_at = now()
                        WHERE id = :id AND claim_token = :claim_token
                        """
                    ),
                    {
                        "id": int(claim["id"]),
                        "claim_token": worker_id,
                        "error": f"{exc.__class__.__name__}:{str(exc)[:900]}",
                    },
                )
                session.commit()
                events.append({"station_id": station_id, "source_pool": claim["source_pool"], "error": str(exc)[:200]})
    print(
        json.dumps(
            {
                "worker_id": worker_id,
                "provider": "brave_search",
                "processed": processed,
                "errors": errors,
                "stop_reason": stop_reason,
                "route_types": route_types,
                "events": events[-25:],
                "finished_at": datetime.utcnow().isoformat() + "Z",
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

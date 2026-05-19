from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from radio_db.db import SessionLocal
from radio_db.services.route_discovery import (
    _brave_answer_remaining,
    route_discovery_candidates,
    run_route_discovery_for_station,
)


MAX_BRAVE_CALLS = 2
MAX_PAGES = 8
CORE_LIMIT = 494
ENRICHMENT_LIMIT = 474


summary: dict[str, object] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "core_target": CORE_LIMIT,
    "enrichment_target": ENRICHMENT_LIMIT,
    "processed": 0,
    "core_processed": 0,
    "enrichment_processed": 0,
    "errors": 0,
    "route_types": {},
    "results": [],
}


def emit(event: str, payload: dict[str, object] | None = None) -> None:
    row: dict[str, object] = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    if payload:
        row.update(payload)
    print(json.dumps(row, ensure_ascii=False), flush=True)


def add_result(pool: str, result: dict[str, object]) -> None:
    summary["processed"] = int(summary["processed"]) + 1
    if pool == "phase2_core":
        summary["core_processed"] = int(summary["core_processed"]) + 1
    else:
        summary["enrichment_processed"] = int(summary["enrichment_processed"]) + 1
    if result.get("status") != "ok":
        summary["errors"] = int(summary["errors"]) + 1

    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    best_route = verdict.get("best_route") if isinstance(verdict.get("best_route"), dict) else {}
    route_type = str(best_route.get("route_type") or "unknown")
    route_types = summary["route_types"]
    assert isinstance(route_types, dict)
    route_types[route_type] = int(route_types.get(route_type, 0)) + 1

    compact = {
        "pool": pool,
        "station_id": result.get("station_id"),
        "name": result.get("name"),
        "status": result.get("status"),
        "brave_calls": result.get("brave_calls"),
        "verified_urls": result.get("verified_urls"),
        "route_type": route_type,
        "confidence": best_route.get("confidence"),
        "error": result.get("error") or result.get("gemini_error") or "",
    }
    results = summary["results"]
    assert isinstance(results, list)
    results.append(compact)
    emit("station_done", compact | {"brave_answer_remaining": _brave_answer_remaining()})


def error_result(station_id: int, name: str | None, exc: Exception) -> dict[str, object]:
    return {
        "station_id": station_id,
        "name": name,
        "status": "error",
        "error": f"{exc.__class__.__name__}:{str(exc)[:250]}",
        "brave_calls": 0,
        "verified_urls": 0,
        "verdict": {"best_route": {"route_type": "error"}},
    }


def main() -> None:
    emit("phase2_paid_start", {"brave_answer_remaining": _brave_answer_remaining()})
    with SessionLocal() as session:
        core = route_discovery_candidates(session, limit=CORE_LIMIT)
        emit("core_candidates_loaded", {"count": len(core)})
        for row in core[:CORE_LIMIT]:
            station_id = int(row["station_id"])
            try:
                result = run_route_discovery_for_station(
                    session,
                    station_id,
                    max_brave_calls=MAX_BRAVE_CALLS,
                    max_pages=MAX_PAGES,
                    apply=True,
                )
            except Exception as exc:
                session.rollback()
                result = error_result(station_id, str(row.get("name") or ""), exc)
            add_result("phase2_core", result)

        enrichment_rows = session.execute(
            text(
                """
                select q.main_station_id, max(q.priority_score) as priority_score
                from candidate_rescan_queue q
                join stations s on s.id = q.main_station_id
                where q.source_pool = 'enrichment_reassessment'
                  and q.status = 'pending'
                  and q.main_station_id is not null
                  and s.website_url is not null
                  and s.status in ('CANDIDATE','VERIFIED')
                  and not exists (
                    select 1 from station_submission_assessments a
                    where a.station_id = q.main_station_id
                      and a.assessment_kind = 'route_discovery_v1'
                  )
                group by q.main_station_id
                order by max(q.priority_score) desc nulls last, q.main_station_id asc
                limit :limit
                """
            ),
            {"limit": ENRICHMENT_LIMIT},
        ).all()
        enrichment_ids = [int(row[0]) for row in enrichment_rows]
        emit("enrichment_candidates_loaded", {"count": len(enrichment_ids)})
        for station_id in enrichment_ids:
            try:
                result = run_route_discovery_for_station(
                    session,
                    station_id,
                    max_brave_calls=MAX_BRAVE_CALLS,
                    max_pages=MAX_PAGES,
                    apply=True,
                )
            except Exception as exc:
                session.rollback()
                result = error_result(station_id, None, exc)
            add_result("phase2_enrichment_reassessment", result)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["brave_answer_remaining"] = _brave_answer_remaining()
    emit("phase2_paid_complete", summary)


if __name__ == "__main__":
    main()

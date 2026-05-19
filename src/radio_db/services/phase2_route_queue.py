from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from radio_db.services.route_discovery import (
    _brave_answer_remaining,
    route_discovery_candidates,
    run_route_discovery_for_station,
)


QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS phase2_route_discovery_queue (
    id SERIAL PRIMARY KEY,
    source_pool VARCHAR(64) NOT NULL,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    claim_token VARCHAR(64),
    claimed_at TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    last_error TEXT,
    route_type VARCHAR(64),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    processed_at TIMESTAMP,
    UNIQUE (source_pool, station_id)
);
CREATE INDEX IF NOT EXISTS ix_phase2_route_queue_status_priority
    ON phase2_route_discovery_queue (status, priority_score DESC, id ASC);
CREATE INDEX IF NOT EXISTS ix_phase2_route_queue_station
    ON phase2_route_discovery_queue (station_id);
"""


def ensure_phase2_route_queue(session: Session) -> None:
    session.execute(text(QUEUE_TABLE_SQL))
    session.commit()


def seed_phase2_route_queue(
    session: Session,
    *,
    include_core: bool = True,
    include_enrichment: bool = True,
    core_limit: int = 494,
    enrichment_limit: int = 474,
) -> dict[str, Any]:
    ensure_phase2_route_queue(session)
    inserted: dict[str, int] = {"phase2_core": 0, "phase2_enrichment_reassessment": 0}
    if include_core:
        core = route_discovery_candidates(session, limit=core_limit)
        for idx, row in enumerate(core[:core_limit]):
            priority_score = (
                float(row.get("quality_score") or 0)
                + 10.0 * float(row.get("confidence_score") or 0)
                + max(0, core_limit - idx) / 10_000.0
            )
            result = session.execute(
                text(
                    """
                    INSERT INTO phase2_route_discovery_queue (source_pool, station_id, priority_score, status)
                    VALUES ('phase2_core', :station_id, :priority_score, 'pending')
                    ON CONFLICT (source_pool, station_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"station_id": int(row["station_id"]), "priority_score": priority_score},
            ).first()
            if result:
                inserted["phase2_core"] += 1

    if include_enrichment:
        rows = session.execute(
            text(
                """
                SELECT q.main_station_id, max(q.priority_score) AS priority_score
                FROM candidate_rescan_queue q
                JOIN stations s ON s.id = q.main_station_id
                WHERE q.source_pool = 'enrichment_reassessment'
                  AND q.status = 'pending'
                  AND q.main_station_id IS NOT NULL
                  AND s.website_url IS NOT NULL
                  AND s.status IN ('CANDIDATE','VERIFIED')
                  AND NOT EXISTS (
                    SELECT 1 FROM station_submission_assessments a
                    WHERE a.station_id = q.main_station_id
                      AND a.assessment_kind = 'route_discovery_v1'
                  )
                GROUP BY q.main_station_id
                ORDER BY max(q.priority_score) DESC NULLS LAST, q.main_station_id ASC
                LIMIT :limit
                """
            ),
            {"limit": enrichment_limit},
        ).all()
        for row in rows:
            result = session.execute(
                text(
                    """
                    INSERT INTO phase2_route_discovery_queue (source_pool, station_id, priority_score, status)
                    VALUES ('phase2_enrichment_reassessment', :station_id, :priority_score, 'pending')
                    ON CONFLICT (source_pool, station_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"station_id": int(row[0]), "priority_score": float(row[1] or 0)},
            ).first()
            if result:
                inserted["phase2_enrichment_reassessment"] += 1
    session.commit()
    return phase2_route_queue_status(session) | {"inserted": inserted}


def phase2_route_queue_status(session: Session) -> dict[str, Any]:
    ensure_phase2_route_queue(session)
    rows = session.execute(
        text(
            """
            SELECT source_pool, status, count(*) AS n
            FROM phase2_route_discovery_queue
            GROUP BY source_pool, status
            ORDER BY source_pool, status
            """
        )
    ).mappings()
    by_pool: dict[str, dict[str, int]] = {}
    for row in rows:
        by_pool.setdefault(str(row["source_pool"]), {})[str(row["status"])] = int(row["n"])
    route_rows = session.execute(
        text(
            """
            SELECT coalesce(route_type, 'unknown') AS route_type, count(*) AS n
            FROM phase2_route_discovery_queue
            WHERE status = 'done'
            GROUP BY 1
            ORDER BY n DESC
            """
        )
    ).mappings()
    return {
        "by_pool": by_pool,
        "done_route_types": {str(row["route_type"]): int(row["n"]) for row in route_rows},
        "brave_answer_remaining": _brave_answer_remaining(),
    }


def _claim_next(session: Session, *, worker_id: str, stale_after_minutes: int = 90) -> dict[str, Any] | None:
    stale_before = datetime.utcnow() - timedelta(minutes=max(5, stale_after_minutes))
    row = session.execute(
        text(
            """
            WITH candidate AS (
                SELECT id
                FROM phase2_route_discovery_queue
                WHERE (
                    status = 'pending'
                    OR (status = 'running' AND claimed_at < :stale_before AND attempt_count < max_attempts)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM station_submission_assessments a
                    WHERE a.station_id = phase2_route_discovery_queue.station_id
                      AND a.assessment_kind = 'route_discovery_v1'
                )
                ORDER BY
                    CASE source_pool
                        WHEN 'phase2_core' THEN 0
                        WHEN 'phase2_enrichment_reassessment' THEN 1
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
            RETURNING q.id, q.station_id, q.source_pool, q.attempt_count
            """
        ),
        {"claim_token": worker_id, "stale_before": stale_before},
    ).mappings().first()
    session.commit()
    return dict(row) if row else None


def _mark_skipped_already_done(session: Session) -> int:
    result = session.execute(
        text(
            """
            UPDATE phase2_route_discovery_queue q
            SET status = 'skipped',
                route_type = 'already_scanned',
                last_error = 'route_discovery_v1 already exists',
                updated_at = now(),
                processed_at = now()
            WHERE status IN ('pending', 'running')
              AND EXISTS (
                SELECT 1 FROM station_submission_assessments a
                WHERE a.station_id = q.station_id
                  AND a.assessment_kind = 'route_discovery_v1'
              )
            """
        )
    )
    session.commit()
    return int(result.rowcount or 0)


def run_phase2_route_worker(
    session: Session,
    *,
    worker_id: str | None = None,
    limit: int = 100,
    max_brave_calls: int = 2,
    max_pages: int = 8,
    stale_after_minutes: int = 90,
) -> dict[str, Any]:
    ensure_phase2_route_queue(session)
    worker = worker_id or f"phase2-{uuid.uuid4().hex[:12]}"
    processed = 0
    errors = 0
    skipped = _mark_skipped_already_done(session)
    route_types: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    stop_reason = ""

    for _ in range(max(1, limit)):
        if max_brave_calls > 0 and _brave_answer_remaining() <= 0:
            stop_reason = "brave_answer_quota_exhausted"
            break
        claim = _claim_next(session, worker_id=worker, stale_after_minutes=stale_after_minutes)
        if not claim:
            stop_reason = "queue_empty"
            break
        station_id = int(claim["station_id"])
        try:
            station_brave_calls = min(max(0, max_brave_calls), max(0, _brave_answer_remaining()))
            result = run_route_discovery_for_station(
                session,
                station_id,
                max_brave_calls=station_brave_calls,
                max_pages=max_pages,
                apply=True,
            )
            if result.get("status") == "error" and str(result.get("error") or "").startswith("brave_answer_"):
                session.rollback()
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
                        "claim_token": worker,
                        "error": str(result.get("error") or "")[:1000],
                    },
                )
                session.commit()
                stop_reason = str(result.get("error") or "brave_answer_unavailable")
                events.append({"station_id": station_id, "source_pool": claim["source_pool"], "error": stop_reason})
                break
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
                    "claim_token": worker,
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
            status = "error" if int(claim.get("attempt_count") or 0) >= 2 else "pending"
            session.execute(
                text(
                    """
                    UPDATE phase2_route_discovery_queue
                    SET status = :status,
                        last_error = :error,
                        updated_at = now()
                    WHERE id = :id AND claim_token = :claim_token
                    """
                ),
                {
                    "id": int(claim["id"]),
                    "claim_token": worker,
                    "status": status,
                    "error": f"{exc.__class__.__name__}:{str(exc)[:900]}",
                },
            )
            session.commit()
            events.append({"station_id": station_id, "source_pool": claim["source_pool"], "error": str(exc)[:200]})

    return {
        "worker_id": worker,
        "processed": processed,
        "errors": errors,
        "skipped_already_done": skipped,
        "stop_reason": stop_reason,
        "route_types": route_types,
        "brave_answer_remaining": _brave_answer_remaining(),
        "events": events[-25:],
    }

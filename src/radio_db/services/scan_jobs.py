from __future__ import annotations

import json
import socket
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from radio_db.models.entities import ScanEvent, ScanJob, ScanRun
from radio_db.services.enrichment import run_free_enrichment_scan, run_paid_enrichment_scan
from radio_db.services.pipeline import run_country_discovery_cycle, stats

SUPPORTED_JOB_TYPES = {
    "stats_snapshot",
    "country_discovery",
    "enrichment_free_scan",
    "enrichment_paid_scan",
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _bounded_int(payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(payload: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _optional_station_id(payload: dict[str, Any]) -> int | None:
    raw_value = payload.get("station_id")
    if raw_value in (None, ""):
        return None
    return max(1, int(raw_value))


def enqueue_scan_job(
    session: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    requested_by: str = "system",
) -> ScanJob:
    if job_type not in SUPPORTED_JOB_TYPES:
        supported = ", ".join(sorted(SUPPORTED_JOB_TYPES))
        raise ValueError(f"Unsupported scan job type: {job_type}. Supported: {supported}")
    job = ScanJob(
        job_type=job_type,
        status="queued",
        priority=priority,
        payload_json=_json_dumps(payload or {}),
        requested_by=requested_by,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def record_scan_event(
    session: Session,
    event_type: str,
    message: str,
    *,
    job_id: int | None = None,
    run_id: int | None = None,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        ScanEvent(
            job_id=job_id,
            run_id=run_id,
            event_type=event_type,
            level=level,
            message=message,
            payload_json=_json_dumps(payload or {}),
        )
    )
    session.commit()


def claim_next_job(session: Session, worker_id: str, stale_after_minutes: int = 60) -> ScanJob | None:
    stale_before = datetime.utcnow() - timedelta(minutes=max(5, stale_after_minutes))
    row = session.execute(
        text(
            """
            SELECT id
            FROM scan_jobs
            WHERE
                status IN ('queued', 'retry')
                OR (
                    status = 'running'
                    AND heartbeat_at IS NOT NULL
                    AND heartbeat_at < :stale_before
                )
            ORDER BY priority ASC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ),
        {"stale_before": stale_before},
    ).first()
    if row is None:
        session.rollback()
        return None

    job = session.get(ScanJob, int(row.id))
    if job is None:
        session.rollback()
        return None

    now = datetime.utcnow()
    job.status = "running"
    job.claimed_by = worker_id
    job.claimed_at = now
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.error = None
    session.commit()
    session.refresh(job)
    return job


def _execute_job_type(session: Session, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if job_type == "stats_snapshot":
        return stats(session)

    if job_type == "country_discovery":
        countries_raw = payload.get("countries")
        countries = None
        if isinstance(countries_raw, str):
            countries = [country.strip().upper() for country in countries_raw.split(",") if country.strip()]
        elif isinstance(countries_raw, list):
            countries = [str(country).strip().upper() for country in countries_raw if str(country).strip()]
        return run_country_discovery_cycle(
            session=session,
            max_queries=_bounded_int(payload, "max_queries", 2, 1, 20),
            country=(str(payload.get("country")).strip().upper() or None) if payload.get("country") else None,
            countries=countries,
            include_linkup=bool(payload.get("include_linkup", False)),
            min_confidence=_bounded_float(payload, "min_confidence", 0.35, 0.0, 1.0),
        )

    if job_type == "enrichment_free_scan":
        return run_free_enrichment_scan(
            session=session,
            limit=_bounded_int(payload, "limit", 20, 1, 200),
            max_urls_per_station=_bounded_int(payload, "max_urls_per_station", 8, 1, 30),
            station_id=_optional_station_id(payload),
        )

    if job_type == "enrichment_paid_scan":
        return run_paid_enrichment_scan(
            session=session,
            limit=_bounded_int(payload, "limit", 5, 1, 50),
            station_id=_optional_station_id(payload),
        )

    raise ValueError(f"Unsupported scan job type: {job_type}")


def execute_scan_job(session: Session, job: ScanJob, worker_id: str) -> dict[str, Any]:
    run = ScanRun(job_id=job.id, worker_id=worker_id, status="running")
    session.add(run)
    session.commit()
    session.refresh(run)
    record_scan_event(session, "job_started", f"Started {job.job_type}", job_id=job.id, run_id=run.id)

    try:
        payload = _json_loads(job.payload_json, {})
        if not isinstance(payload, dict):
            payload = {}
        result = _execute_job_type(session, job.job_type, payload)
    except Exception as exc:
        now = datetime.utcnow()
        message = str(exc)
        job.status = "failed"
        job.error = message
        job.finished_at = now
        job.heartbeat_at = now
        run.status = "failed"
        run.error = message
        run.finished_at = now
        run.heartbeat_at = now
        session.commit()
        record_scan_event(session, "job_failed", message, job_id=job.id, run_id=run.id, level="error")
        raise

    now = datetime.utcnow()
    job.status = "succeeded"
    job.finished_at = now
    job.heartbeat_at = now
    job.error = None
    run.status = "succeeded"
    run.result_json = _json_dumps(result)
    run.finished_at = now
    run.heartbeat_at = now
    session.commit()
    record_scan_event(session, "job_succeeded", f"Finished {job.job_type}", job_id=job.id, run_id=run.id, payload=result)
    return result


def run_scan_worker(
    session_factory: Callable[[], Session],
    *,
    once: bool = False,
    sleep_seconds: int = 10,
    worker_id: str | None = None,
) -> dict[str, Any]:
    worker = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    processed = 0
    failed = 0

    while True:
        with session_factory() as session:
            job = claim_next_job(session, worker)
            if job is not None:
                try:
                    execute_scan_job(session, job, worker)
                    processed += 1
                except Exception:
                    failed += 1
            elif once:
                break

        if once:
            break
        if job is None:
            time.sleep(max(1, sleep_seconds))

    return {"worker_id": worker, "processed": processed, "failed": failed}


def scan_job_overview(session: Session) -> dict[str, Any]:
    rows = session.execute(select(ScanJob.status, func.count()).group_by(ScanJob.status)).all()
    latest = session.scalars(select(ScanJob).order_by(ScanJob.created_at.desc()).limit(10)).all()
    return {
        "by_status": {status: count for status, count in rows},
        "latest": [
            {
                "id": job.id,
                "job_type": job.job_type,
                "status": job.status,
                "priority": job.priority,
                "requested_by": job.requested_by,
                "claimed_by": job.claimed_by,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "error": job.error,
            }
            for job in latest
        ],
    }

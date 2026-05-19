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
from radio_db.services.contact_center import (
    create_dry_run_send,
    create_execute_send,
    create_persisted_contact_draft,
)
from radio_db.services.enrichment import run_free_enrichment_scan, run_paid_enrichment_scan
from radio_db.services.forms import run_country_form_cycle, scan_submission_forms
from radio_db.services.forms_agent import run_main_scan_cycle, start_execute_submission_run, start_manual_scan_run
from radio_db.services.outreach_campaign import campaign_monitor, generate_outreach_email
from radio_db.services.pipeline import run_country_discovery_cycle, stats

SUPPORTED_JOB_TYPES = {
    "stats_snapshot",
    "country_discovery",
    "enrichment_free_scan",
    "enrichment_paid_scan",
    "submission_form_scan",
    "submission_country_form_cycle",
    "submission_agent_manual_scan",
    "submission_agent_main_cycle",
    "submission_form_execute",
    "campaign_monitor",
    "outreach_create_draft",
    "outreach_generate_email",
    "outreach_dry_run_send",
    "outreach_execute_send",
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


def _required_int(payload: dict[str, Any], key: str) -> int:
    raw_value = payload.get(key)
    if raw_value in (None, ""):
        raise ValueError(f"{key}_required")
    return max(1, int(raw_value))


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = str(payload.get(key) or "").strip()
    return value or None


def _country_list(payload: dict[str, Any]) -> list[str] | None:
    countries_raw = payload.get("countries")
    if isinstance(countries_raw, str):
        countries = [country.strip().upper() for country in countries_raw.split(",") if country.strip()]
    elif isinstance(countries_raw, list):
        countries = [str(country).strip().upper() for country in countries_raw if str(country).strip()]
    else:
        countries = []
    return countries or None


def _payload_from_object(value: object) -> dict[str, Any]:
    data = getattr(value, "__dict__", {})
    return {key: item for key, item in data.items() if not key.startswith("_")}


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
        return run_country_discovery_cycle(
            session=session,
            max_queries=_bounded_int(payload, "max_queries", 2, 1, 20),
            country=(str(payload.get("country")).strip().upper() or None) if payload.get("country") else None,
            countries=_country_list(payload),
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

    if job_type == "submission_form_scan":
        return scan_submission_forms(
            session=session,
            station_limit=_bounded_int(payload, "station_limit", 5, 1, 50),
            country=_optional_str(payload, "country"),
            station_id=_optional_station_id(payload),
            mode=_optional_str(payload, "mode") or "read",
            max_forms_per_station=_bounded_int(payload, "max_forms_per_station", 2, 1, 10),
        )

    if job_type == "submission_country_form_cycle":
        return run_country_form_cycle(
            session=session,
            station_limit=_bounded_int(payload, "station_limit", 5, 1, 50),
            mode=_optional_str(payload, "mode") or "read",
            max_forms_per_station=_bounded_int(payload, "max_forms_per_station", 2, 1, 10),
            countries=_country_list(payload),
        )

    if job_type == "submission_agent_manual_scan":
        run = start_manual_scan_run(
            session=session,
            station_id=_required_int(payload, "station_id"),
            mode=_optional_str(payload, "mode") or "scan",
            target_url=_optional_str(payload, "target_url"),
            max_pages=_bounded_int(payload, "max_pages", 1, 1, 10),
            force_rescan=bool(payload.get("force_rescan", False)),
        )
        return {
            "agent_run_id": run.id,
            "station_id": run.station_id,
            "status": run.status,
            "current_state": run.current_state,
            "target_url": run.target_url,
            "blocked_reason": run.blocked_reason,
        }

    if job_type == "submission_agent_main_cycle":
        return run_main_scan_cycle(
            session=session,
            station_limit=_bounded_int(payload, "station_limit", 10, 1, 100),
            max_pages=_bounded_int(payload, "max_pages", 2, 1, 10),
            min_confidence=_bounded_float(payload, "min_confidence", 0.0, 0.0, 1.0),
            use_checkpoint=bool(payload.get("use_checkpoint", True)),
            reset_checkpoint=bool(payload.get("reset_checkpoint", False)),
        )

    if job_type == "submission_form_execute":
        run = start_execute_submission_run(
            session=session,
            station_id=_required_int(payload, "station_id"),
            form_id=payload.get("form_id"),
            target_url=_optional_str(payload, "target_url"),
            mode=_optional_str(payload, "mode") or "execute",
            max_retries=_bounded_int(payload, "max_retries", 1, 0, 3),
        )
        return {
            "agent_run_id": run.id,
            "station_id": run.station_id,
            "form_id": run.form_id,
            "status": run.status,
            "current_state": run.current_state,
            "target_url": run.target_url,
            "blocked_reason": run.blocked_reason,
        }

    if job_type == "campaign_monitor":
        result = campaign_monitor(session, _required_int(payload, "campaign_id"))
        if result is None:
            raise ValueError("campaign_not_found")
        return result

    if job_type == "outreach_create_draft":
        draft = create_persisted_contact_draft(
            session=session,
            station_id=_required_int(payload, "station_id"),
            template_id=payload.get("template_id"),
            subject=_optional_str(payload, "subject"),
            body=_optional_str(payload, "body"),
            campaign_id=payload.get("campaign_id"),
        )
        if draft is None:
            raise ValueError("station_not_found")
        return _payload_from_object(draft)

    if job_type == "outreach_generate_email":
        return generate_outreach_email(
            session=session,
            campaign_id=_required_int(payload, "campaign_id"),
            station_id=_required_int(payload, "station_id"),
        )

    if job_type == "outreach_dry_run_send":
        send = create_dry_run_send(
            session=session,
            draft_id=_required_int(payload, "draft_id"),
            target_value=_optional_str(payload, "target_value"),
            channel=_optional_str(payload, "channel"),
        )
        if send is None:
            raise ValueError("draft_not_found")
        return _payload_from_object(send)

    if job_type == "outreach_execute_send":
        send = create_execute_send(
            session=session,
            draft_id=_required_int(payload, "draft_id"),
            target_value=_optional_str(payload, "target_value"),
            channel=_optional_str(payload, "channel"),
        )
        if send is None:
            raise ValueError("draft_not_found")
        return _payload_from_object(send)

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

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.brave_keys import (
    brave_answer_keys,
    brave_search_keys,
    effective_daily_limit,
    effective_monthly_limit,
)
from radio_db.models.entities import (
    ContactRole,
    CrawlFrontier,
    Evidence,
    Station,
    StationPerson,
    StationProfileSnapshot,
    StationStatus,
    SubmissionChannel,
)
from radio_db.services.pipeline import (
    boost_top_major_station_ids,
    get_scan_focus_countries,
    load_brave_boost_state,
    load_country_discovery_intelligence,
    load_high_priority_state,
    load_scan_focus_state,
    market_focus_countries,
    save_scan_focus_state,
)


_ARCHIVED_STATUS = getattr(StationStatus, "ARCHIVED", None)


def _non_archived_status_filter():
    excluded = [StationStatus.REJECTED]
    if _ARCHIVED_STATUS is not None:
        excluded.append(_ARCHIVED_STATUS)
    return Station.status.notin_(excluded)


def _archived_status_condition():
    if _ARCHIVED_STATUS is None:
        return False
    return Station.status == _ARCHIVED_STATUS


def _priority_tier_filter():
    priority_column = getattr(Station, "priority_tier", None)
    if priority_column is None:
        return true()
    return priority_column > 0


def _run_command(cmd: list[str], timeout: int = 3) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def _latest_rejected_queue_db(queue_dir: Path) -> Path | None:
    candidates = sorted(queue_dir.glob("radio-rejected-*.db"))
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return candidates[0]


def _sqlite_scalar(db_path: Path, query: str) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(query).fetchone()
    if not row:
        return 0
    try:
        return int(row[0] or 0)
    except Exception:
        return 0


def _sqlite_rows(db_path: Path, query: str) -> list[sqlite3.Row]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query).fetchall()


def _rejected_scan_worker_state() -> dict[str, Any]:
    raw = _run_command(
        [
            "bash",
            "-lc",
            "ps -eo pid,user,etimes,cmd --sort=-etimes | rg 'radio-db scan-rejected-cycle' | rg -v 'rg ' | head -n 1",
        ],
        timeout=2,
    )
    if not raw or raw.startswith("unavailable:"):
        return {
            "is_running": False,
            "pid": None,
            "user": "",
            "elapsed_seconds": 0,
            "command": "",
        }
    parts = raw.split(None, 3)
    if len(parts) < 4:
        return {
            "is_running": True,
            "pid": None,
            "user": "",
            "elapsed_seconds": 0,
            "command": raw.strip(),
        }
    return {
        "is_running": True,
        "pid": int(parts[0]) if parts[0].isdigit() else None,
        "user": parts[1],
        "elapsed_seconds": int(parts[2]) if parts[2].isdigit() else 0,
        "command": parts[3].strip(),
    }


def _build_rejected_scan_snapshot(state_dir: Path) -> dict[str, Any]:
    queue_dir = Path("queue_dbs")
    queue_db = _latest_rejected_queue_db(queue_dir)
    checkpoint = _load_json(state_dir / "rejected_scan_checkpoint.json")
    worker = _rejected_scan_worker_state()
    if queue_db is None or not queue_db.exists():
        return {
            "queue_db_path": None,
            "is_running": bool(worker.get("is_running")),
            "worker": worker,
            "checkpoint_last_station_id": int(checkpoint.get("last_station_id", 0) or 0),
            "checkpoint_updated_at": str(checkpoint.get("updated_at") or "-"),
            "error": "queue_db_missing",
        }

    queue_total = _sqlite_scalar(queue_db, "select count(*) from stations;")
    rejected_total = _sqlite_scalar(queue_db, "select count(*) from stations where status='REJECTED';")
    rejected_with_website = _sqlite_scalar(
        queue_db,
        "select count(*) from stations where status='REJECTED' and website_url is not null and trim(website_url)<>'';",
    )
    scanned_station_count = _sqlite_scalar(queue_db, "select count(distinct station_id) from submission_agent_runs;")
    total_runs = _sqlite_scalar(queue_db, "select count(*) from submission_agent_runs;")
    awaiting_review_runs = _sqlite_scalar(
        queue_db,
        "select count(*) from submission_agent_runs where status='awaiting_review';",
    )
    blocked_runs = _sqlite_scalar(
        queue_db,
        "select count(*) from submission_agent_runs where status='blocked';",
    )
    recent_two_hour_row = _sqlite_rows(
        queue_db,
        """
        select
          count(*) as runs_last_2h,
          sum(coalesce(json_extract(summary_json,'$.forms_saved'),0)) as forms_last_2h,
          sum(coalesce(json_extract(summary_json,'$.emails_saved'),0)) as emails_last_2h
        from submission_agent_runs
        where started_at >= datetime('now','-2 hours');
        """,
    )
    recent_two_hour = dict(recent_two_hour_row[0]) if recent_two_hour_row else {}
    recent_status_rows = _sqlite_rows(
        queue_db,
        """
        select status, count(*) as total
        from submission_agent_runs
        where started_at >= datetime('now','-2 hours')
        group by status
        order by total desc;
        """,
    )
    latest_runs = [
        {
            "station_id": int(row["station_id"] or 0),
            "station_name": str(row["canonical_name"] or ""),
            "website_url": str(row["website_url"] or ""),
            "status": str(row["status"] or ""),
            "blocked_reason": str(row["blocked_reason"] or ""),
            "forms_saved": int(row["forms_saved"] or 0),
            "emails_saved": int(row["emails_saved"] or 0),
            "finished_at": str(row["finished_at"] or ""),
        }
        for row in _sqlite_rows(
            queue_db,
            """
            select
              s.id,
              s.canonical_name,
              s.website_url,
              r.station_id,
              r.status,
              r.blocked_reason,
              json_extract(r.summary_json,'$.forms_saved') as forms_saved,
              json_extract(r.summary_json,'$.emails_saved') as emails_saved,
              r.finished_at
            from submission_agent_runs r
            join stations s on s.id=r.station_id
            order by r.id desc
            limit 8;
            """,
        )
    ]
    top_issue_types = [
        {"issue_type": str(row["issue_type"] or ""), "total": int(row["total"] or 0)}
        for row in _sqlite_rows(
            queue_db,
            """
            select issue_type, count(*) as total
            from submission_agent_issues
            group by issue_type
            order by total desc
            limit 8;
            """,
        )
    ]
    form_status_counts = [
        {
            "form_type": str(row["form_type"] or ""),
            "status": str(row["status"] or ""),
            "total": int(row["total"] or 0),
        }
        for row in _sqlite_rows(
            queue_db,
            """
            select form_type, status, count(*) as total
            from forms
            group by form_type, status
            order by total desc
            limit 8;
            """,
        )
    ]
    forms_total_row = _sqlite_rows(
        queue_db,
        """
        select
          count(*) as forms_total,
          sum(case when status='ACTIVE' then 1 else 0 end) as forms_active,
          sum(case when status='CAPTCHA_PRESENT' then 1 else 0 end) as forms_captcha,
          sum(case when status='LOGIN_REQUIRED' then 1 else 0 end) as forms_login
        from forms;
        """,
    )
    forms_totals = dict(forms_total_row[0]) if forms_total_row else {}
    submissions_row = _sqlite_rows(
        queue_db,
        """
        select
          count(*) as submissions_total,
          sum(case when accepts_newcomers=1 then 1 else 0 end) as newcomer_routes
        from submission_channels;
        """,
    )
    submissions_totals = dict(submissions_row[0]) if submissions_row else {}
    progress_ratio = (scanned_station_count / rejected_with_website) if rejected_with_website else 0.0
    return {
        "queue_db_path": str(queue_db.resolve()),
        "queue_db_size_mb": round(queue_db.stat().st_size / (1024 * 1024), 2),
        "is_running": bool(worker.get("is_running")),
        "worker": worker,
        "checkpoint_last_station_id": int(checkpoint.get("last_station_id", 0) or 0),
        "checkpoint_updated_at": str(checkpoint.get("updated_at") or "-"),
        "queue_total": int(queue_total),
        "queue_rejected_total": int(rejected_total),
        "queue_rejected_with_website": int(rejected_with_website),
        "scanned_station_count": int(scanned_station_count),
        "progress_ratio": float(progress_ratio),
        "total_runs": int(total_runs),
        "awaiting_review_runs": int(awaiting_review_runs),
        "blocked_runs": int(blocked_runs),
        "recent_2h_runs": int(recent_two_hour.get("runs_last_2h", 0) or 0),
        "recent_2h_forms": int(recent_two_hour.get("forms_last_2h", 0) or 0),
        "recent_2h_emails": int(recent_two_hour.get("emails_last_2h", 0) or 0),
        "recent_2h_status_counts": [
            {"status": str(row["status"] or ""), "total": int(row["total"] or 0)}
            for row in recent_status_rows
        ],
        "forms_total": int(forms_totals.get("forms_total", 0) or 0),
        "forms_active": int(forms_totals.get("forms_active", 0) or 0),
        "forms_captcha": int(forms_totals.get("forms_captcha", 0) or 0),
        "forms_login": int(forms_totals.get("forms_login", 0) or 0),
        "submissions_total": int(submissions_totals.get("submissions_total", 0) or 0),
        "newcomer_routes": int(submissions_totals.get("newcomer_routes", 0) or 0),
        "people_entities": _sqlite_scalar(queue_db, "select count(*) from station_people;"),
        "evidence_rows": _sqlite_scalar(queue_db, "select count(*) from evidence;"),
        "llm_quality_rows": _sqlite_scalar(
            queue_db,
            "select count(*) from station_submission_assessments where assessment_kind='llm_quality_v1';",
        ),
        "llm_provider": settings.station_quality_provider,
        "llm_model": settings.station_quality_gemini_model if settings.station_quality_provider == "gemini" else settings.station_quality_openai_model,
        "browser_worker_enabled": bool(settings.browser_worker_enabled),
        "browser_headless": bool(settings.browser_headless),
        "batch_station_limit": 45,
        "max_pages": 3,
        "latest_runs": latest_runs,
        "top_issue_types": top_issue_types,
        "form_status_counts": form_status_counts,
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_api_call_history(limit: int, hours: int) -> list[dict[str, Any]]:
    path = Path(".radio_db_state") / "api_call_history.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        ts_raw = str(row.get("ts_utc") or "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            else:
                ts = ts.astimezone(UTC)
        except Exception:
            ts = None
        if ts is not None and ts < cutoff:
            break
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def set_scan_focus(enabled: bool, market_focus: str, updated_by: str = "api") -> dict[str, Any]:
    safe_market = (market_focus or "international").strip().lower()
    if safe_market not in {"international", "dach", "anglo", "eu_core", "top_major"}:
        safe_market = "international"
    return save_scan_focus_state(enabled=enabled, market_focus=safe_market, updated_by=updated_by)


def build_data_control_overview(session: Session, history_limit: int = 30, history_hours: int = 72) -> dict[str, Any]:
    state_dir = Path(".radio_db_state")
    budget = _load_json(state_dir / "cost_guard.json")
    usage = _load_json(state_dir / "api_usage.json")
    openai_reconcile = _load_json(state_dir / "openai_cost_reconcile.json")
    verify_checkpoint = _load_json(state_dir / "verify_checkpoint.json")
    boost_state = load_brave_boost_state()
    high_priority_state = load_high_priority_state()
    scan_focus_state = load_scan_focus_state()
    scan_focus_countries = get_scan_focus_countries()
    scan_focus_enabled = bool(scan_focus_state.get("enabled", False))
    scan_focus_market = str(scan_focus_state.get("market_focus", "international"))

    focus_filters = [
        _non_archived_status_filter(),
        Station.website_url.is_not(None),
        func.length(func.trim(func.coalesce(Station.website_url, ""))) > 0,
        Station.confidence_score >= settings.station_enrich_min_station_confidence,
    ]
    if scan_focus_enabled and scan_focus_countries:
        focus_filters.append(func.upper(Station.country_code).in_(sorted(scan_focus_countries)))

    focus_eligible_total = session.scalar(select(func.count(Station.id)).where(*focus_filters)) or 0
    focus_processed_ever = session.scalar(
        select(func.count(func.distinct(Evidence.station_id)))
        .select_from(Evidence)
        .join(Station, Station.id == Evidence.station_id)
        .where(
            Evidence.source_id.in_(("station_enrich_search", "station_enrich_touch")),
            *focus_filters,
        )
    ) or 0
    focus_pending = max(0, focus_eligible_total - focus_processed_ever)
    focus_progress_ratio = (focus_processed_ever / focus_eligible_total) if focus_eligible_total else 1.0

    boost_unique_seen_by_market: dict[str, list[int]] = boost_state.get("unique_seen_by_market") or {}

    def _boost_market_progress(market: str) -> tuple[int, int, float]:
        countries = market_focus_countries(market)
        eligible_filters = [
            _non_archived_status_filter(),
            Station.website_url.is_not(None),
            func.length(func.trim(func.coalesce(Station.website_url, ""))) > 0,
            Station.confidence_score >= settings.brave_boost_min_station_confidence,
        ]
        if market == "top_major":
            top_major_ids = boost_top_major_station_ids(
                session=session,
                min_station_confidence=settings.brave_boost_min_station_confidence,
            )
            if not top_major_ids:
                return 0, 0, 1.0
            eligible_filters.append(Station.id.in_(sorted(top_major_ids)))
        if countries:
            eligible_filters.append(func.upper(Station.country_code).in_(sorted(countries)))
        eligible = session.scalar(select(func.count(Station.id)).where(*eligible_filters)) or 0

        seen_raw = boost_unique_seen_by_market.get(market, []) or []
        seen_ids = sorted({int(station_id) for station_id in seen_raw if str(station_id).isdigit()})
        if not seen_ids:
            unique_seen = session.scalar(
                select(func.count(func.distinct(Evidence.station_id)))
                .select_from(Evidence)
                .join(Station, Station.id == Evidence.station_id)
                .where(
                    Evidence.source_id == "brave_boost",
                    *eligible_filters,
                )
            ) or 0
            ratio = (unique_seen / eligible) if eligible else 1.0
            return int(eligible), int(unique_seen), float(ratio)
        seen_filters = list(eligible_filters)
        seen_filters.append(Station.id.in_(seen_ids))
        unique_seen = session.scalar(select(func.count(Station.id)).where(*seen_filters)) or 0
        ratio = (unique_seen / eligible) if eligible else 1.0
        return int(eligible), int(unique_seen), float(ratio)

    last_boost_market = str(boost_state.get("last_market_focus") or "international").strip().lower()
    if last_boost_market not in {"international", "dach", "anglo", "eu_core", "top_major"}:
        last_boost_market = "international"
    dach_eligible, dach_unique, dach_ratio = _boost_market_progress("dach")
    last_eligible, last_unique, last_ratio = _boost_market_progress(last_boost_market)

    def _key_count(csv_value: str | None, single_value: str | None) -> int:
        keys: list[str] = []
        if csv_value:
            keys.extend(key.strip() for key in csv_value.split(",") if key.strip())
        if single_value:
            keys.append(single_value.strip())
        return max(1, len({key for key in keys if key}))

    tavily_key_count = _key_count(settings.tavily_api_keys, settings.tavily_api_key)
    linkup_key_count = _key_count(settings.linkup_api_keys, settings.linkup_api_key)
    effective_max_tavily_day = settings.max_tavily_calls_per_day * tavily_key_count
    effective_max_tavily_month = settings.max_tavily_calls_per_month * tavily_key_count
    effective_max_linkup_day = settings.max_linkup_calls_per_day * linkup_key_count
    effective_max_linkup_month = settings.max_linkup_calls_per_month * linkup_key_count

    stations_total = session.scalar(select(func.count(Station.id))) or 0
    stations_rejected = session.scalar(select(func.count(Station.id)).where(Station.status == StationStatus.REJECTED)) or 0
    stations_archived = session.scalar(select(func.count(Station.id)).where(_archived_status_condition())) or 0
    stations_active = max(0, stations_total - stations_rejected - stations_archived)
    stations_verified = session.scalar(select(func.count(Station.id)).where(Station.status == StationStatus.VERIFIED)) or 0
    visible_high_priority = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            Station.confidence_score >= 0.6,
            _priority_tier_filter(),
        )
    ) or 0
    manual_confirmed_count = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            _priority_tier_filter(),
            Station.manual_confirmed.is_(True),
        )
    ) or 0
    stations_with_submission = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id)))
        .select_from(SubmissionChannel)
        .join(Station, Station.id == SubmissionChannel.station_id)
        .where(_non_archived_status_filter())
    ) or 0
    stations_without_submission = max(0, stations_active - stations_with_submission)
    submission_coverage_active = (stations_with_submission / stations_active) if stations_active else 0.0
    verified_with_submission = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            Station.id.in_(select(SubmissionChannel.station_id)),
        )
    ) or 0
    verified_submission_coverage = (verified_with_submission / stations_verified) if stations_verified else 0.0
    stations_with_newcomer_signal = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id)))
        .select_from(SubmissionChannel)
        .join(Station, Station.id == SubmissionChannel.station_id)
        .where(
            _non_archived_status_filter(),
            (SubmissionChannel.accepts_newcomers.is_(True))
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%new artist%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%unsigned%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%emerging%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%newcomer%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%demo%")
        )
    ) or 0
    stations_with_decision_maker = session.scalar(
        select(func.count(func.distinct(StationPerson.station_id))).where(
            StationPerson.role.in_([ContactRole.MUSIC_DIRECTOR, ContactRole.PROGRAM_DIRECTOR, ContactRole.PRODUCER])
        )
    ) or 0
    pitch_ready_stations = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            Station.confidence_score >= settings.station_enrich_min_station_confidence,
            Station.id.in_(select(SubmissionChannel.station_id)),
            Station.id.in_(
                select(SubmissionChannel.station_id).where(
                    (SubmissionChannel.accepts_newcomers.is_(True))
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%new artist%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%unsigned%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%emerging%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%newcomer%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%demo%")
                )
            ),
            Station.id.in_(
                select(StationPerson.station_id).where(
                    StationPerson.role.in_([ContactRole.MUSIC_DIRECTOR, ContactRole.PROGRAM_DIRECTOR, ContactRole.PRODUCER])
                )
            ),
        )
    ) or 0
    pitch_ready_ratio = (pitch_ready_stations / stations_active) if stations_active else 0.0
    verified_ratio_active = (stations_verified / stations_active) if stations_active else 0.0
    candidate_stations = session.scalar(select(func.count(Station.id)).where(Station.status == StationStatus.CANDIDATE)) or 0
    tested_candidate_stations = session.scalar(
        select(func.count(func.distinct(Evidence.station_id)))
        .select_from(Evidence)
        .join(Station, Station.id == Evidence.station_id)
        .where(
            Evidence.source_id == "quality_verify",
            Station.status == StationStatus.CANDIDATE,
        )
    ) or 0
    untested_candidate_stations = max(0, candidate_stations - tested_candidate_stations)
    verify_progress_ratio = (tested_candidate_stations / candidate_stations) if candidate_stations else 1.0
    active_without_website = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            or_(Station.website_url.is_(None), func.length(func.trim(func.coalesce(Station.website_url, ""))) == 0),
        )
    ) or 0
    cooldown_cutoff = datetime.utcnow() - timedelta(hours=max(1, settings.station_enrich_cooldown_hours))
    touched_recent_station_count = session.scalar(
        select(func.count(func.distinct(Evidence.station_id))).where(
            Evidence.source_id == "station_enrich_touch",
            Evidence.created_at >= cooldown_cutoff,
        )
    ) or 0
    eligible_active_with_website = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            Station.website_url.is_not(None),
            Station.confidence_score >= settings.station_enrich_min_station_confidence,
        )
    ) or 0
    pending_after_cooldown = max(0, eligible_active_with_website - touched_recent_station_count)

    monitor = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "catalog": {
            "stations_total": int(stations_total),
            "stations_active": int(stations_active),
            "stations_verified": int(stations_verified),
            "stations_rejected": int(stations_rejected),
            "stations_archived": int(stations_archived),
            "verified_ratio_active": float(verified_ratio_active),
            "visible_high_priority": int(visible_high_priority),
            "manual_confirmed_count": int(manual_confirmed_count),
        },
        "coverage": {
            "submission_channels": int(session.scalar(select(func.count(SubmissionChannel.id))) or 0),
            "stations_with_submission": int(stations_with_submission),
            "stations_without_submission": int(stations_without_submission),
            "submission_coverage_active": float(submission_coverage_active),
            "verified_with_submission": int(verified_with_submission),
            "verified_submission_coverage": float(verified_submission_coverage),
            "stations_with_newcomer_signal": int(stations_with_newcomer_signal),
            "stations_with_decision_maker": int(stations_with_decision_maker),
            "pitch_ready_stations": int(pitch_ready_stations),
            "pitch_ready_ratio": float(pitch_ready_ratio),
        },
        "verification": {
            "candidate_stations": int(candidate_stations),
            "tested_candidate_stations": int(tested_candidate_stations),
            "untested_candidate_stations": int(untested_candidate_stations),
            "verify_progress_ratio": float(verify_progress_ratio),
            "verify_checkpoint_last_station_id": int(verify_checkpoint.get("last_station_id", 0) or 0),
            "verify_checkpoint_completed": bool(verify_checkpoint.get("completed", False)),
            "verify_checkpoint_updated_at": str(verify_checkpoint.get("updated_at", "-")),
        },
        "enrichment": {
            "active_without_website": int(active_without_website),
            "station_enrich_touched_recent": int(touched_recent_station_count),
            "station_enrich_pending_after_cooldown": int(pending_after_cooldown),
            "station_enrich_cooldown_hours": int(settings.station_enrich_cooldown_hours),
            "people_entities": int(session.scalar(select(func.count(StationPerson.id))) or 0),
            "profile_snapshots": int(session.scalar(select(func.count(StationProfileSnapshot.id))) or 0),
            "evidence_today": int(
                session.scalar(
                    select(func.count(Evidence.id)).where(
                        Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                    )
                )
                or 0
            ),
            "internal_urls_today": int(
                session.scalar(
                    select(func.count(Evidence.id)).where(
                        Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                        Evidence.source_id == "station_enrich_search",
                        Evidence.raw_title == "Internal station source",
                    )
                )
                or 0
            ),
            "internal_stations_today": int(
                session.scalar(
                    select(func.count(func.distinct(Evidence.station_id))).where(
                        Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                        Evidence.source_id == "station_enrich_search",
                        Evidence.raw_title == "Internal station source",
                    )
                )
                or 0
            ),
            "frontier_due": int(
                session.scalar(
                    select(func.count(CrawlFrontier.id)).where(
                        or_(CrawlFrontier.next_run_at.is_(None), CrawlFrontier.next_run_at <= datetime.utcnow())
                    )
                )
                or 0
            ),
        },
        "quotas": {
            "llm": {
                "usd_spent_today_estimate": float(budget.get("usd_spent_estimate", 0.0) or 0.0),
                "max_daily_usd": float(settings.max_daily_usd),
                "llm_calls_today": int(budget.get("llm_calls", 0) or 0),
                "max_llm_calls_per_day": int(settings.max_llm_calls_per_day),
                "provider_mode": settings.llm_provider_mode,
            },
            "openai_reconcile": {
                "ok": bool(openai_reconcile.get("ok", False)),
                "updated_at": str(openai_reconcile.get("updated_at", "-")),
                "api_daily_total_usd": float(openai_reconcile.get("api_daily_total_usd", 0.0) or 0.0),
                "sync_mode": str(openai_reconcile.get("sync_mode", "-")),
                "error": str(openai_reconcile.get("error", "") or ""),
            },
            "providers": {
                "brave": {
                    "month_calls": int(usage.get("brave_calls", 0) or 0),
                    "max_month_calls": effective_monthly_limit(
                        settings.max_brave_calls_per_month,
                        len(brave_search_keys()),
                    ),
                },
                "brave_answer": {
                    "day_calls": int(usage.get("brave_answer_calls_day", 0) or 0),
                    "month_calls": int(usage.get("brave_answer_calls_month", 0) or 0),
                    "max_day_calls": effective_daily_limit(
                        settings.max_brave_answer_calls_per_day,
                        len(brave_answer_keys()),
                    ),
                    "max_month_calls": effective_monthly_limit(
                        settings.max_brave_answer_calls_per_month,
                        len(brave_answer_keys()),
                    ),
                },
                "google_cse": {
                    "day_calls": int(usage.get("google_cse_calls_day", 0) or 0),
                    "month_calls": int(usage.get("google_cse_calls_month", 0) or 0),
                    "max_day_calls": int(settings.max_google_cse_calls_per_day),
                    "max_month_calls": int(settings.max_google_cse_calls_per_month),
                },
                "tavily": {
                    "day_calls": int(usage.get("tavily_calls_day", 0) or 0),
                    "month_calls": int(usage.get("tavily_calls_month", 0) or 0),
                    "max_day_calls": int(effective_max_tavily_day),
                    "max_month_calls": int(effective_max_tavily_month),
                },
                "grok": {
                    "day_calls": int(usage.get("grok_calls_day", 0) or 0),
                    "month_calls": int(usage.get("grok_calls_month", 0) or 0),
                    "usd_day": float(usage.get("grok_usd_day", 0.0) or 0.0),
                    "usd_month": float(usage.get("grok_usd_month", 0.0) or 0.0),
                    "max_day_calls": int(settings.max_grok_calls_per_day),
                    "max_month_calls": int(settings.max_grok_calls_per_month),
                    "max_month_usd": float(settings.max_grok_usd_per_month),
                },
                "linkup": {
                    "day_calls": int(usage.get("linkup_calls_day", 0) or 0),
                    "month_calls": int(usage.get("linkup_calls_month", 0) or 0),
                    "max_day_calls": int(effective_max_linkup_day),
                    "max_month_calls": int(effective_max_linkup_month),
                },
                "duckduckgo": {
                    "day_calls": int(usage.get("duckduckgo_calls_day", 0) or 0),
                    "month_calls": int(usage.get("duckduckgo_calls_month", 0) or 0),
                    "max_day_calls": int(settings.max_duckduckgo_calls_per_day),
                    "max_month_calls": int(settings.max_duckduckgo_calls_per_month),
                },
            },
        },
        "scan_focus": {
            "enabled": scan_focus_enabled,
            "market": scan_focus_market,
            "updated_at": str(scan_focus_state.get("updated_at") or "-"),
            "updated_by": str(scan_focus_state.get("updated_by") or "-"),
            "country_count": 0 if scan_focus_countries is None else len(scan_focus_countries),
            "eligible_total": int(focus_eligible_total),
            "processed_ever": int(focus_processed_ever),
            "pending": int(focus_pending),
            "progress_ratio": float(focus_progress_ratio),
        },
        "boost": {
            "is_running": bool(boost_state.get("is_running", False)),
            "last_started_at": str(boost_state.get("last_started_at") or "-"),
            "last_finished_at": str(boost_state.get("last_finished_at") or "-"),
            "last_market_focus": str(boost_state.get("last_market_focus") or "international"),
            "last_budget_usd": float(boost_state.get("last_budget_usd", 0.0) or 0.0),
            "last_spent_usd": float(boost_state.get("last_spent_usd", 0.0) or 0.0),
            "last_calls": int(boost_state.get("last_calls", 0) or 0),
            "last_stations_processed": int(boost_state.get("last_stations_processed", 0) or 0),
            "last_results": int(boost_state.get("last_results", 0) or 0),
            "last_submissions_added": int(boost_state.get("last_submissions_added", 0) or 0),
            "last_contacts_added": int(boost_state.get("last_contacts_added", 0) or 0),
            "month": str(boost_state.get("month") or datetime.utcnow().strftime("%Y-%m")),
            "month_spent_usd": float(boost_state.get("month_spent_usd", 0.0) or 0.0),
            "month_calls": int(boost_state.get("month_calls", 0) or 0),
            "last_error": str(boost_state.get("last_error") or ""),
            "dach_eligible": int(dach_eligible),
            "dach_unique_seen": int(dach_unique),
            "dach_progress_ratio": float(dach_ratio),
            "last_market_eligible": int(last_eligible),
            "last_market_unique_seen": int(last_unique),
            "last_market_progress_ratio": float(last_ratio),
            "default_budget_usd": float(settings.brave_boost_default_budget_usd),
            "effective_usd_per_call": float(settings.brave_boost_usd_per_call),
            "no_repeat_days": int(settings.brave_boost_no_repeat_days),
        },
        "high_priority": {
            "is_running": bool(high_priority_state.get("is_running", False)),
            "last_started_at": str(high_priority_state.get("last_started_at") or "-"),
            "last_finished_at": str(high_priority_state.get("last_finished_at") or "-"),
            "last_stations": int(high_priority_state.get("last_stations", 0) or 0),
            "last_urls": int(high_priority_state.get("last_urls", 0) or 0),
            "last_results": int(high_priority_state.get("last_results", 0) or 0),
            "last_submissions_added": int(high_priority_state.get("last_submissions_added", 0) or 0),
            "last_contacts_added": int(high_priority_state.get("last_contacts_added", 0) or 0),
            "last_llm_calls": int(high_priority_state.get("last_llm_calls", 0) or 0),
            "last_targets": list(high_priority_state.get("last_targets", []) or []),
            "last_error": str(high_priority_state.get("last_error") or ""),
            "default_station_limit": int(settings.high_priority_deep_dive_station_limit),
        },
        "services": {
            "radio_db.service": _run_command(["systemctl", "is-active", "radio-db.service"]),
            "radio_db.timer": _run_command(["systemctl", "is-active", "radio-db.timer"]),
            "radio_db.timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db.timer"]),
            "radio_db_internal.service": _run_command(["systemctl", "is-active", "radio-db-internal.service"]),
            "radio_db_internal.timer": _run_command(["systemctl", "is-active", "radio-db-internal.timer"]),
            "radio_db_internal.timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-internal.timer"]),
            "radio_db_people.service": _run_command(["systemctl", "is-active", "radio-db-people.service"]),
            "radio_db_people.timer": _run_command(["systemctl", "is-active", "radio-db-people.timer"]),
            "radio_db_people.timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-people.timer"]),
            "radio_db_verify.service": _run_command(["systemctl", "is-active", "radio-db-verify.service"]),
            "radio_db_verify.timer": _run_command(["systemctl", "is-active", "radio-db-verify.timer"]),
            "radio_db_verify.timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-verify.timer"]),
            "radio_db_dashboard.service": _run_command(["systemctl", "is-active", "radio-db-dashboard.service"]),
        },
        "scan_operations": {
            "rejected_scan": _build_rejected_scan_snapshot(state_dir),
        },
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "monitor": monitor,
        "country_discovery": load_country_discovery_intelligence(limit=min(max(history_limit, 5), 50)),
        "api_history": read_api_call_history(limit=history_limit, hours=history_hours),
    }

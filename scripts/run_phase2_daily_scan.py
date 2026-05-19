from __future__ import annotations

import argparse
import calendar
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from radio_db.connectors.brave import search_web
from radio_db.db import SessionLocal, init_db
from radio_db.models.entities import Station, StationStatus
from radio_db.services.api_call_history import API_CALL_HISTORY_PATH
from radio_db.services.budget import ApiUsageGuard
from radio_db.services.dedupe import find_duplicate_station, station_fingerprint
from radio_db.services.normalize import normalize_text
from radio_db.services.route_discovery import (
    ASSESSMENT_KIND,
    DIRECT_SUBMISSION_HINTS,
    MUSIC_DIRECTOR_HINTS,
    _build_brave_prompts,
    _build_gemini_prompt,
    _build_station_features,
    _brave_answer,
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
)


PLAN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS phase2_daily_scan_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(80) NOT NULL,
    worker_id VARCHAR(80) NOT NULL,
    queue_name VARCHAR(32) NOT NULL,
    queue_id INTEGER NOT NULL,
    station_id INTEGER,
    source_station_id INTEGER,
    provider VARCHAR(32) NOT NULL DEFAULT 'brave_search',
    status VARCHAR(32) NOT NULL,
    route_type VARCHAR(64),
    search_calls INTEGER NOT NULL DEFAULT 0,
    answer_calls INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TIMESTAMP NOT NULL DEFAULT now(),
    finished_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_phase2_daily_scan_runs_run_id ON phase2_daily_scan_runs(run_id);
CREATE INDEX IF NOT EXISTS ix_phase2_daily_scan_runs_queue ON phase2_daily_scan_runs(queue_name, queue_id);
"""


def _ensure_tables(session) -> None:
    session.execute(text(PLAN_TABLE_SQL))
    session.execute(text("ALTER TABLE phase2_daily_scan_runs ADD COLUMN IF NOT EXISTS answer_calls INTEGER NOT NULL DEFAULT 0"))
    session.commit()


def _api_history_count_month(provider: str) -> int:
    if not API_CALL_HISTORY_PATH.exists():
        return 0
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    count = 0
    for line in API_CALL_HISTORY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            payload = json.loads(line)
            ts = datetime.fromisoformat(str(payload.get("ts_utc") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.strftime("%Y-%m") != month or payload.get("provider") != provider:
            continue
        if str(payload.get("status") or "") in {"success", "error"}:
            count += 1
    return count


def _api_history_count_today(provider: str) -> int:
    if not API_CALL_HISTORY_PATH.exists():
        return 0
    today = datetime.now(timezone.utc).date()
    count = 0
    for line in API_CALL_HISTORY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            payload = json.loads(line)
            ts = datetime.fromisoformat(str(payload.get("ts_utc") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.date() != today or payload.get("provider") != provider:
            continue
        if str(payload.get("status") or "") in {"success", "error"}:
            count += 1
    return count


def _days_remaining_in_month() -> int:
    today = datetime.now(timezone.utc).date()
    return calendar.monthrange(today.year, today.month)[1] - today.day + 1


def _effective_search_daily_limit(monthly_limit: int, daily_target: int) -> int:
    month_calls = _brave_search_calls_month()
    remaining_month_calls = max(0, int(monthly_limit) - month_calls)
    fair_share = (remaining_month_calls + _days_remaining_in_month() - 1) // _days_remaining_in_month()
    if daily_target <= 0:
        return 0
    return max(0, min(int(daily_target), fair_share))


def _brave_search_calls_month() -> int:
    usage_path = Path(".radio_db_state/api_usage.json")
    guard = ApiUsageGuard(usage_path, max_brave_calls_per_month=999_999)
    return int(guard.state.brave_calls)


def _brave_answer_calls_today() -> int:
    usage_path = Path(".radio_db_state/api_usage.json")
    guard = ApiUsageGuard(usage_path, max_brave_calls_per_month=999_999)
    return int(guard.state.brave_answer_calls_day)


def _release_stale_claims(session, *, older_than_minutes: int) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=max(5, older_than_minutes))
    total = 0
    for table_name in ("phase2_scan_plan", "phase2_external_backlog"):
        result = session.execute(
            text(
                f"""
                UPDATE {table_name}
                SET status = 'pending',
                    claim_token = NULL,
                    claimed_at = NULL,
                    updated_at = now()
                WHERE status = 'running'
                  AND claimed_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        )
        total += int(result.rowcount or 0)
    session.commit()
    return total


def _claim_internal(session, worker_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            WITH candidate AS (
                SELECT id
                FROM phase2_scan_plan
                WHERE status = 'pending'
                ORDER BY priority_score DESC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE phase2_scan_plan p
            SET status = 'running',
                claim_token = :worker_id,
                claimed_at = now(),
                updated_at = now()
            FROM candidate
            WHERE p.id = candidate.id
            RETURNING p.*
            """
        ),
        {"worker_id": worker_id},
    ).mappings().first()
    session.commit()
    return dict(row) if row else None


def _claim_external(session, worker_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            WITH candidate AS (
                SELECT id
                FROM phase2_external_backlog
                WHERE status = 'pending'
                ORDER BY priority_score DESC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE phase2_external_backlog p
            SET status = 'running',
                claim_token = :worker_id,
                claimed_at = now(),
                updated_at = now()
            FROM candidate
            WHERE p.id = candidate.id
            RETURNING p.*
            """
        ),
        {"worker_id": worker_id},
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


def _search_station(station_like: Any, *, max_search_calls: int) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    station_domain = _safe_domain(station_like.website_url)
    search_results: list[dict[str, Any]] = []
    answer_urls: list[str] = []
    answer_emails: list[str] = []
    for label, query in _build_brave_prompts(station_like)[: max(1, max_search_calls)]:
        rows = search_web(query=query, count=8, country=getattr(station_like, "country_code", "") or None)
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
            row_text = _search_result_text(row)
            answer_urls.extend(_official_urls(f"{row.get('url') or ''}\n{row_text}", station_domain))
            answer_emails.extend(_emails(row_text))
    return search_results, answer_urls, answer_emails


def _has_answer_worthy_signal(
    station_like: Any,
    queue_category: str,
    search_results: list[dict[str, Any]],
    answer_urls: list[str],
    answer_emails: list[str],
) -> bool:
    high_value_categories = {
        "revalidate_existing_strong_route",
        "verify_submission_signal",
        "promoted_rejected_high",
        "needs_review_high",
        "external_needs_review",
        "external_llm_quality_music",
    }
    if queue_category not in high_value_categories:
        return False
    text_blob = _compact_text(" ".join(str(item.get("content") or "") for item in search_results))
    has_route_hint = _has_any(text_blob, DIRECT_SUBMISSION_HINTS) or _has_any(text_blob, MUSIC_DIRECTOR_HINTS)
    if not has_route_hint:
        return False
    station_domain = _safe_domain(getattr(station_like, "website_url", "") or "")
    has_official_url = bool(answer_urls)
    has_dedicated_email = any(
        _is_dedicated_submission_email(email) and not _is_generic_contact_email(email)
        for email in answer_emails
        if station_domain and station_domain in email.split("@", 1)[-1]
    )
    return has_official_url or has_dedicated_email or queue_category == "revalidate_existing_strong_route"


def _maybe_add_brave_answer(
    station_like: Any,
    *,
    queue_category: str,
    search_results: list[dict[str, Any]],
    answer_urls: list[str],
    answer_emails: list[str],
    enabled: bool,
    max_answer_calls_day: int,
) -> str:
    if not enabled or max_answer_calls_day <= 0:
        return ""
    if _brave_answer_calls_today() >= max_answer_calls_day:
        return ""
    if not _has_answer_worthy_signal(station_like, queue_category, search_results, answer_urls, answer_emails):
        return ""
    station_domain = _safe_domain(getattr(station_like, "website_url", "") or "")
    prompt = (
        f'For radio station "{getattr(station_like, "canonical_name", "")}" on official domain {station_domain}, '
        "find only official music submission, airplay, promo, or music/program director contact routes. "
        "Return concise official URLs, emails, names/roles, and say NO_OFFICIAL_ROUTE if none. "
        "Do not include directories or unrelated stations."
    )
    try:
        answer = _brave_answer(prompt)
    except Exception as exc:
        message = f"{exc.__class__.__name__}:{str(exc)[:240]}"
        search_results.append(
            {
                "ok": False,
                "query_type": "answer_escalation",
                "provider": "brave_answer",
                "content": "",
                "error": message,
            }
        )
        if "402" in message or "429" in message or "Payment" in message or "Too Many" in message:
            return "brave_answer_unavailable_or_rate_limited"
        return ""
    content = _compact_text(answer.get("answer") or answer.get("content") or "")
    search_results.append(
        {
            "ok": bool(content),
            "query_type": "answer_escalation",
            "provider": "brave_answer",
            "content": content,
            "usage": answer.get("usage") or {},
        }
    )
    answer_urls.extend(_official_urls(content, station_domain))
    answer_emails.extend(_emails(content))
    return ""


def _successful_search_calls(search_results: list[dict[str, Any]]) -> int:
    return sum(1 for item in search_results if item.get("ok") and item.get("provider") == "brave_search")


def _successful_answer_calls(search_results: list[dict[str, Any]]) -> int:
    return sum(1 for item in search_results if item.get("ok") and item.get("provider") == "brave_answer")


def _run_search_route_discovery(
    session,
    station: Station,
    *,
    old_features: dict[str, Any] | None = None,
    search_results: list[dict[str, Any]],
    answer_urls: list[str],
    answer_emails: list[str],
    max_pages: int,
) -> dict[str, Any]:
    old_features = old_features if old_features is not None else _build_station_features(
        session=session, station=station, include_deep_pass=False
    )
    station_domain = _safe_domain(station.website_url)
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
        if _safe_domain(email_domain) and station_domain and (
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
        "provider_override": "brave_search_daily",
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
        "provider": "brave_search_daily",
        "search_calls": _successful_search_calls(search_results),
        "answer_calls": _successful_answer_calls(search_results),
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


def _mark_done(session, table_name: str, row_id: int, result: dict[str, Any], *, station_id: int | None = None) -> None:
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    best_route = verdict.get("best_route") if isinstance(verdict.get("best_route"), dict) else {}
    route_type = str(best_route.get("route_type") or "unknown")
    station_clause = ", station_id = :station_id" if table_name == "phase2_external_backlog" else ""
    session.execute(
        text(
            f"""
            UPDATE {table_name}
            SET status = 'done',
                claim_token = NULL,
                claimed_at = NULL,
                processed_at = now(),
                updated_at = now()
                {station_clause}
            WHERE id = :id
            """
        ),
        {"id": row_id, "station_id": station_id},
    )
    session.execute(
        text(
            """
            INSERT INTO phase2_daily_scan_runs (
                run_id, worker_id, queue_name, queue_id, station_id, source_station_id,
                provider, status, route_type, search_calls, answer_calls, result_json, error, finished_at
            ) VALUES (
                :run_id, :worker_id, :queue_name, :queue_id, :station_id, :source_station_id,
                'brave_search', 'done', :route_type, :search_calls, :answer_calls, :result_json, :error, now()
            )
            """
        ),
        {
            "run_id": result["_run_id"],
            "worker_id": result["_worker_id"],
            "queue_name": result["_queue_name"],
            "queue_id": row_id,
            "station_id": station_id or result.get("station_id"),
            "source_station_id": result.get("_source_station_id"),
            "route_type": route_type,
            "search_calls": int(result.get("search_calls") or 0),
            "answer_calls": int(result.get("answer_calls") or 0),
            "result_json": json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, ensure_ascii=False),
            "error": str(result.get("gemini_error") or result.get("error") or "")[:1000],
        },
    )
    session.commit()


def _mark_status(session, table_name: str, row: dict[str, Any], status: str, error: str, result: dict[str, Any]) -> None:
    session.execute(
        text(
            f"""
            UPDATE {table_name}
            SET status = CAST(:status AS VARCHAR),
                claim_token = CASE
                    WHEN CAST(:status AS VARCHAR) IN ('pending', 'no_signal', 'error') THEN NULL
                    ELSE claim_token
                END,
                claimed_at = CASE
                    WHEN CAST(:status AS VARCHAR) IN ('pending', 'no_signal', 'error') THEN NULL
                    ELSE claimed_at
                END,
                updated_at = now(),
                processed_at = CASE WHEN CAST(:status AS VARCHAR) IN ('no_signal', 'error') THEN now() ELSE processed_at END
            WHERE id = :id
            """
        ),
        {"id": int(row["id"]), "status": status},
    )
    session.execute(
        text(
            """
            INSERT INTO phase2_daily_scan_runs (
                run_id, worker_id, queue_name, queue_id, source_station_id,
                provider, status, search_calls, answer_calls, result_json, error, finished_at
            ) VALUES (
                :run_id, :worker_id, :queue_name, :queue_id, :source_station_id,
                'brave_search', :status, :search_calls, :answer_calls, :result_json, :error, now()
            )
            """
        ),
        {
            "run_id": result["_run_id"],
            "worker_id": result["_worker_id"],
            "queue_name": result["_queue_name"],
            "queue_id": int(row["id"]),
            "source_station_id": result.get("_source_station_id"),
            "status": status,
            "search_calls": int(result.get("search_calls") or 0),
            "answer_calls": int(result.get("answer_calls") or 0),
            "result_json": json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, ensure_ascii=False),
            "error": error[:1000],
        },
    )
    session.commit()


def _create_or_match_external_station(session, row: dict[str, Any]) -> Station:
    duplicate = find_duplicate_station(
        session,
        str(row["canonical_name"]),
        str(row["website_url"] or ""),
        str(row["country_code"] or ""),
        min_name_similarity=92,
    )
    if duplicate:
        return duplicate
    station = Station(
        canonical_name=str(row["canonical_name"])[:255],
        normalized_name=normalize_text(str(row["canonical_name"])),
        country_code=str(row["country_code"] or "").upper()[:8],
        language="",
        website_url=str(row["website_url"] or "")[:2048] or None,
        stream_url=str(row["stream_url"] or "")[:2048] or None,
        city=None,
        status=StationStatus.CANDIDATE,
        confidence_score=float(row.get("old_confidence") or 0.35),
        fingerprint=station_fingerprint(str(row["canonical_name"]), str(row["website_url"] or ""), str(row["country_code"] or "")),
    )
    session.add(station)
    session.flush()
    return station


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--limit-stations", type=int, default=25)
    parser.add_argument("--max-brave-search-calls-month", type=int, default=2000)
    parser.add_argument("--max-brave-search-calls-day", type=int, default=60)
    parser.add_argument("--max-brave-answer-calls-day", type=int, default=20)
    parser.add_argument("--enable-answer-escalation", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-search-calls-per-station", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--stale-after-minutes", type=int, default=90)
    args = parser.parse_args()

    init_db()
    worker_id = args.worker_id or f"phase2-daily-{uuid.uuid4().hex[:10]}"
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{worker_id}"
    processed = 0
    errors = 0
    no_signal = 0
    stop_reason = ""
    events: list[dict[str, Any]] = []
    search_month_limit = max(0, int(args.max_brave_search_calls_month))
    search_day_limit = _effective_search_daily_limit(search_month_limit, int(args.max_brave_search_calls_day))

    with SessionLocal() as session:
        _ensure_tables(session)
        released = _release_stale_claims(session, older_than_minutes=args.stale_after_minutes)
        for _ in range(max(1, args.limit_stations)):
            if _brave_search_calls_month() >= max(0, search_month_limit):
                stop_reason = "brave_search_monthly_quota_reached"
                break
            if _api_history_count_today("brave") >= max(0, search_day_limit):
                stop_reason = "brave_search_daily_target_reached"
                break
            row = _claim_internal(session, worker_id)
            queue_name = "phase2_scan_plan"
            table_name = "phase2_scan_plan"
            if row is None:
                row = _claim_external(session, worker_id)
                queue_name = "phase2_external_backlog"
                table_name = "phase2_external_backlog"
            if row is None:
                stop_reason = "queue_empty"
                break

            try:
                old_features = None
                if queue_name == "phase2_scan_plan":
                    station = session.get(Station, int(row["station_id"]))
                    if station is None:
                        result = {
                            "_run_id": run_id,
                            "_worker_id": worker_id,
                            "_queue_name": queue_name,
                            "search_calls": 0,
                            "answer_calls": 0,
                        }
                        _mark_status(session, table_name, row, "error", "station_not_found", result)
                        errors += 1
                        continue
                    station_like = station
                else:
                    station_like = SimpleNamespace(
                        canonical_name=str(row["canonical_name"]),
                        website_url=str(row["website_url"] or ""),
                        country_code=str(row["country_code"] or ""),
                    )

                search_results, answer_urls, answer_emails = _search_station(
                    station_like, max_search_calls=args.max_search_calls_per_station
                )
                answer_stop = _maybe_add_brave_answer(
                    station_like,
                    queue_category=str(row.get("queue_category") or ""),
                    search_results=search_results,
                    answer_urls=answer_urls,
                    answer_emails=answer_emails,
                    enabled=bool(args.enable_answer_escalation),
                    max_answer_calls_day=args.max_brave_answer_calls_day,
                )
                if answer_stop:
                    result = {
                        "_run_id": run_id,
                        "_worker_id": worker_id,
                        "_queue_name": queue_name,
                        "_source_station_id": int(row.get("source_station_id") or 0) or None,
                        "station_id": getattr(station_like, "id", None),
                        "search_calls": _successful_search_calls(search_results),
                        "answer_calls": _successful_answer_calls(search_results),
                        "search_results": search_results,
                    }
                    _mark_status(session, table_name, row, "pending", answer_stop, result)
                    stop_reason = answer_stop
                    break

                if _successful_search_calls(search_results) <= 0:
                    result = {
                        "_run_id": run_id,
                        "_worker_id": worker_id,
                        "_queue_name": queue_name,
                        "_source_station_id": int(row.get("source_station_id") or 0) or None,
                        "station_id": getattr(station_like, "id", None),
                        "search_calls": 0,
                        "answer_calls": _successful_answer_calls(search_results),
                        "search_results": search_results,
                    }
                    _mark_status(session, table_name, row, "no_signal", "no successful Brave Search results", result)
                    no_signal += 1
                    events.append({"queue": queue_name, "id": int(row["id"]), "status": "no_signal"})
                    continue

                if queue_name == "phase2_external_backlog":
                    station = _create_or_match_external_station(session, row)
                    old_features = {
                        "source": "phase2_external_backlog",
                        "old_scan_outcome": row.get("old_scan_outcome"),
                        "old_quality_score": row.get("old_quality_score"),
                        "old_confidence": row.get("old_llm_confidence"),
                    }
                    session.commit()

                result = _run_search_route_discovery(
                    session,
                    station,
                    old_features=old_features,
                    search_results=search_results,
                    answer_urls=answer_urls,
                    answer_emails=answer_emails,
                    max_pages=args.max_pages,
                )
                result["_run_id"] = run_id
                result["_worker_id"] = worker_id
                result["_queue_name"] = queue_name
                result["_source_station_id"] = int(row.get("source_station_id") or 0) or None
                _mark_done(session, table_name, int(row["id"]), result, station_id=station.id)
                processed += 1
                best = ((result.get("verdict") or {}).get("best_route") or {})
                events.append(
                    {
                        "queue": queue_name,
                        "id": int(row["id"]),
                        "station_id": station.id,
                        "route_type": best.get("route_type"),
                        "search_calls": result.get("search_calls"),
                        "answer_calls": result.get("answer_calls"),
                    }
                )
            except Exception as exc:
                session.rollback()
                result = {
                    "_run_id": run_id,
                    "_worker_id": worker_id,
                    "_queue_name": queue_name,
                    "_source_station_id": int(row.get("source_station_id") or 0) or None,
                    "search_calls": 0,
                    "answer_calls": 0,
                }
                _mark_status(session, table_name, row, "pending", f"{exc.__class__.__name__}:{str(exc)[:900]}", result)
                errors += 1
                events.append({"queue": queue_name, "id": int(row["id"]), "error": str(exc)[:160]})

    print(
        json.dumps(
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "processed": processed,
                "no_signal": no_signal,
                "errors": errors,
                "released_stale_claims": released if "released" in locals() else 0,
                "stop_reason": stop_reason,
                "brave_search_calls_month": _brave_search_calls_month(),
                "brave_search_history_calls_month": _api_history_count_month("brave"),
                "brave_search_calls_today": _api_history_count_today("brave"),
                "brave_search_month_limit": search_month_limit,
                "brave_search_day_limit": search_day_limit,
                "brave_search_days_remaining_in_month": _days_remaining_in_month(),
                "brave_answer_calls_today": _brave_answer_calls_today(),
                "events": events[-25:],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

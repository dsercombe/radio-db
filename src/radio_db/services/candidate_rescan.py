from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from radio_db.config import settings
from radio_db.models.entities import (
    CandidateRescanQueue,
    Station,
    StationStatus,
    StationSubmissionAssessment,
)
from radio_db.services.forms_agent import (
    _candidate_urls_for_station,
    _extract_rejected_scan_payload,
    _persist_extracted_scan_run,
    _rank_candidate_scan_urls,
    is_supported_station_target_url,
    start_manual_scan_run,
    station_matches_entrypoint_only,
    station_matches_excluded_focus,
    station_matches_excluded_meta,
)
from radio_db.services.station_quality import run_station_quality_verification


DEFAULT_QUEUE_DB_PATH = Path("queue_dbs") / "radio-rejected-45319.db"
CURRENT_RESCAN_SCHEMA_VERSION = 1
PROMOTABLE_RESCAN_STATUSES = {"verified", "verified_contact_only", "candidate_contact_only"}
CANDIDATE_RESCAN_WRITE_LOCK = threading.RLock()
NETWORK_DOMAIN_HINTS = {
    "bbc.co.uk",
    "bellmedia.ca",
    "francebleu.fr",
    "iheart.com",
    "radiofrance.fr",
    "radio-canada.ca",
    "radio-canada.ca",
    "radioplayer.ca",
    "rsi.ch",
    "siriusxm.com",
}
ENTRYPOINT_TITLE_HINTS = (
    "airplay guide",
    "award submission",
    "call for entries",
    "call for scores",
    "contact us",
    "how to submit",
    "independent radio exchange",
    "music submission",
    "music submissions",
    "send music to stations",
    "sending music to stations",
    "submission guidelines",
    "submissions",
    "submit music",
    "submitting music",
)
NON_STATION_ENTRYPOINT_DOMAIN_HINTS = (
    "distro",
    "distribution",
    "earshot-distro",
    "emisora.org",
    "radio-usa.net",
    "soundcloud.com",
    "topradio.me",
    "musicaustria.at",
)
NON_STATION_ENTRYPOINT_PATH_HINTS = (
    "award-submission",
    "call-for-entries",
    "call-for-scores",
    "independent-radio-exchange",
    "music-submission",
    "music-submissions",
    "sending-music-to-stations",
    "submission-guidelines",
    "submit-music",
)
PLATFORM_EMAIL_DOMAINS = {
    "soundcloud.com",
    "spotify.com",
    "youtube.com",
    "google.com",
    "gmail.com",
}
GENERIC_PLATFORM_EMAILS = {
    "music@soundcloud.com",
    "support@soundcloud.com",
    "info@soundcloud.com",
}


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _queue_db_path(path: str | None = None) -> Path:
    raw = Path(path or DEFAULT_QUEUE_DB_PATH)
    return raw if raw.is_absolute() else Path.cwd() / raw


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _name_tokens(value: str | None) -> set[str]:
    stop = {
        "fm",
        "am",
        "radio",
        "online",
        "stream",
        "aac",
        "mp3",
        "kbps",
        "live",
        "the",
        "station",
    }
    return {
        token
        for token in re.split(r"[^a-z0-9]+", (value or "").lower())
        if len(token) >= 2 and token not in stop and not token.isdigit()
    }


def _name_similarity(left: str | None, right: str | None) -> float:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _domain_matches_network_hint(domain: str) -> bool:
    return any(domain == hint or domain.endswith(f".{hint}") for hint in NETWORK_DOMAIN_HINTS)


def _domain_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _route_matches_station_domain(station: Station, url: str = "", email: str = "") -> bool:
    station_domain = _domain(station.website_url)
    route_domain = _domain(url)
    if route_domain and station_domain and _domain_matches(route_domain, station_domain):
        return True
    if email and "@" in email and station_domain:
        email_domain = email.lower().split("@", 1)[1]
        return _domain_matches(email_domain, station_domain)
    return False


def _has_confirmed_best_route(station: Station, assessment_payload: dict[str, Any], path_quality: str) -> bool:
    features = assessment_payload.get("features") if isinstance(assessment_payload.get("features"), dict) else {}
    best = features.get("best_submission_route") if isinstance(features.get("best_submission_route"), dict) else {}
    if not best and isinstance(assessment_payload.get("best_submission_route"), dict):
        best = assessment_payload["best_submission_route"]
    route_type = str(best.get("route_type") or path_quality or "").strip()
    url = str(best.get("url") or "").strip()
    email = str(best.get("email") or "").strip().lower()
    reason = str(best.get("reason") or "").strip().lower()
    if route_type not in {"direct_music_form", "gated_music_form", "explicit_submission_email"}:
        return False
    route_domain = _domain(url)
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    if email in GENERIC_PLATFORM_EMAILS:
        return False
    if route_domain and any(hint in route_domain for hint in NON_STATION_ENTRYPOINT_DOMAIN_HINTS):
        return False
    if email_domain in PLATFORM_EMAIL_DOMAINS and not _route_matches_station_domain(station, url=url, email=email):
        return False
    if route_type == "explicit_submission_email" and not _route_matches_station_domain(station, url=url, email=email):
        if not any(token in reason for token in ("music submission", "submit music", "airplay", "new music", "send music")):
            return False
    if url and not _route_matches_station_domain(station, url=url, email=email):
        return False
    return True


def _looks_like_submission_entrypoint_title(name: str | None) -> bool:
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    return any(
        lowered == hint
        or lowered.startswith(f"{hint} ")
        or lowered.startswith(f"{hint} -")
        or lowered.startswith(f"{hint} —")
        for hint in ENTRYPOINT_TITLE_HINTS
    )


def _looks_like_non_station_entrypoint(*, station: Station, assessment_payload: dict[str, Any]) -> bool:
    parsed_url = urlparse(station.website_url or "")
    domain = (parsed_url.netloc or "").lower().replace("www.", "")
    path = (parsed_url.path or "").lower()
    name = station.canonical_name or station.normalized_name or ""
    features = assessment_payload.get("features") if isinstance(assessment_payload.get("features"), dict) else {}
    topical_labels = [str(x).lower() for x in (features.get("topical_exclusion_labels") or [])]
    reason_codes = [
        str(x).lower()
        for x in (assessment_payload.get("reason_codes") or [])
        if isinstance(assessment_payload.get("reason_codes"), list)
    ]
    if "submission_entrypoint" in topical_labels or "format_rejected:submission_entrypoint" in reason_codes:
        return True
    if any(hint in domain for hint in NON_STATION_ENTRYPOINT_DOMAIN_HINTS):
        return True
    if any(hint in path for hint in NON_STATION_ENTRYPOINT_PATH_HINTS):
        return True
    return _looks_like_submission_entrypoint_title(name)


def _duplicate_match_kind(station_domain: str, left_name: str | None, right_name: str | None) -> str:
    left_tokens = _name_tokens(left_name)
    right_tokens = _name_tokens(right_name)
    if not left_tokens or not right_tokens:
        return "duplicate"
    shared = left_tokens & right_tokens
    left_unique = left_tokens - right_tokens
    right_unique = right_tokens - left_tokens
    similarity = _name_similarity(left_name, right_name)
    if similarity >= 0.95:
        return "duplicate"
    if _domain_matches_network_hint(station_domain) and len(shared) >= 1 and left_unique and right_unique:
        return "network_sibling"
    if len(shared) >= 2 and left_unique and right_unique and similarity < 0.8:
        return "network_sibling"
    return "duplicate"


def _find_promotion_duplicate(session: Session, *, station: Station, queue_id: int) -> dict[str, Any] | None:
    station_domain = _domain(station.website_url)
    if not station_domain:
        return None
    station_name = station.canonical_name or station.normalized_name or ""
    station_tokens = _name_tokens(station_name)
    if not station_tokens:
        return None

    main_rows = session.execute(
        text(
            """
            SELECT id, canonical_name, website_url, status, confidence_score
            FROM stations
            WHERE id != :station_id
              AND website_url IS NOT NULL
              AND status IN ('VERIFIED', 'CANDIDATE')
              AND replace(lower(COALESCE(website_url, '')), 'www.', '') LIKE :domain_like
            LIMIT 50
            """
        ),
        {"station_id": int(station.id), "domain_like": f"%{station_domain}%"},
    ).mappings().all()
    for row in main_rows:
        similarity = _name_similarity(station_name, str(row["canonical_name"] or ""))
        if similarity >= 0.5:
            match_kind = _duplicate_match_kind(station_domain, station_name, str(row["canonical_name"] or ""))
            return {
                "source": "main_db",
                "match_kind": match_kind,
                "station_id": int(row["id"]),
                "name": str(row["canonical_name"] or ""),
                "status": str(row["status"] or ""),
                "website_url": str(row["website_url"] or ""),
                "similarity": round(similarity, 3),
            }

    queue_rows = session.execute(
        text(
            """
            SELECT q.id, q.main_station_id, s.canonical_name, s.website_url, q.result_status, q.path_quality
            FROM candidate_rescan_queue q
            JOIN stations s ON s.id = q.main_station_id
            WHERE q.id != :queue_id
              AND q.status = 'done'
              AND q.result_status IN ('verified', 'verified_contact_only', 'candidate_contact_only')
              AND s.website_url IS NOT NULL
              AND replace(lower(COALESCE(s.website_url, '')), 'www.', '') LIKE :domain_like
            ORDER BY q.updated_at DESC
            LIMIT 50
            """
        ),
        {"queue_id": int(queue_id), "domain_like": f"%{station_domain}%"},
    ).mappings().all()
    for row in queue_rows:
        similarity = _name_similarity(station_name, str(row["canonical_name"] or ""))
        if similarity >= 0.5:
            match_kind = _duplicate_match_kind(station_domain, station_name, str(row["canonical_name"] or ""))
            return {
                "source": "candidate_rescan_queue",
                "match_kind": match_kind,
                "queue_id": int(row["id"]),
                "station_id": int(row["main_station_id"]),
                "name": str(row["canonical_name"] or ""),
                "result_status": str(row["result_status"] or ""),
                "path_quality": str(row["path_quality"] or ""),
                "website_url": str(row["website_url"] or ""),
                "similarity": round(similarity, 3),
            }
    return None


def _attach_queue_db(session: Session, queue_db_path: str | None) -> str:
    path = _queue_db_path(queue_db_path)
    if not path.exists():
        raise FileNotFoundError(f"queue_db_not_found:{path}")
    alias = "rescue_queue_db"
    escaped = str(path).replace("'", "''")
    try:
        session.execute(text(f"DETACH DATABASE {alias}"))
    except Exception:
        session.rollback()
    session.execute(text(f"ATTACH DATABASE '{escaped}' AS {alias}"))
    return alias


def _detach_queue_db(session: Session, alias: str) -> None:
    try:
        session.execute(text(f"DETACH DATABASE {alias}"))
    except Exception:
        session.rollback()


def seed_enrichment_reassessment_queue(
    session: Session,
    *,
    min_new_evidence: int = 5,
    reset_existing: bool = False,
) -> dict[str, Any]:
    source_pool = "enrichment_reassessment"
    if reset_existing:
        session.execute(
            text("DELETE FROM candidate_rescan_queue WHERE source_pool = :pool"),
            {"pool": source_pool},
        )
        session.commit()

    before = int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue")) or 0)
    session.execute(
        text(
            """
            WITH latest_quality AS (
                SELECT station_id, MAX(updated_at) AS assessed_at
                FROM station_submission_assessments
                WHERE assessment_kind = :assessment_kind
                GROUP BY station_id
            ),
            enrichment_delta AS (
                SELECT
                    s.id AS station_id,
                    COUNT(e.id) AS new_evidence_count,
                    MAX(e.created_at) AS newest_evidence_at,
                    MAX(lq.assessed_at) AS assessed_at
                FROM stations s
                JOIN evidence e ON e.station_id = s.id
                LEFT JOIN latest_quality lq ON lq.station_id = s.id
                WHERE s.status IN ('CANDIDATE', 'VERIFIED')
                  AND s.website_url IS NOT NULL
                  AND e.source_id IN ('station_enrich_search', 'station_enrich_touch')
                  AND datetime(e.created_at) > datetime(COALESCE(lq.assessed_at, '1970-01-01 00:00:00'))
                GROUP BY s.id
                HAVING COUNT(e.id) >= :min_new_evidence
            )
            INSERT OR IGNORE INTO candidate_rescan_queue (
                source_pool, main_station_id, queue_station_id, queue_fingerprint,
                old_queue_status, old_main_status, old_scan_outcome, old_decision,
                old_quality_score, old_confidence, priority_score, status, attempt_count,
                evidence_json, created_at, updated_at
            )
            SELECT
                :pool,
                s.id,
                NULL,
                s.fingerprint,
                NULL,
                s.status,
                'enrichment_delta',
                NULL,
                NULL,
                NULL,
                45.0 + MIN(40.0, ed.new_evidence_count * 4.0) + (10.0 * COALESCE(s.confidence_score, 0.0)),
                'pending',
                0,
                json_object(
                    'seeded_from', 'main.enrichment_delta',
                    'new_evidence_count', ed.new_evidence_count,
                    'newest_evidence_at', ed.newest_evidence_at,
                    'last_quality_assessed_at', ed.assessed_at,
                    'min_new_evidence', :min_new_evidence,
                    'schema_version', :schema_version
                ),
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM enrichment_delta ed
            JOIN stations s ON s.id = ed.station_id
            """
        ),
        {
            "assessment_kind": settings.station_quality_assessment_kind,
            "min_new_evidence": max(1, int(min_new_evidence)),
            "pool": source_pool,
            "schema_version": CURRENT_RESCAN_SCHEMA_VERSION,
        },
    )
    session.commit()
    after = int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue")) or 0)
    return {
        "pool": source_pool,
        "inserted": max(0, after - before),
        "min_new_evidence": max(1, int(min_new_evidence)),
        "totals": candidate_rescan_stats(session),
    }


def seed_candidate_rescan_queue(
    session: Session,
    *,
    queue_db_path: str | None = None,
    pool: str = "all",
    min_review_score: float = 70.0,
    reset_existing: bool = False,
) -> dict[str, Any]:
    alias = _attach_queue_db(session, queue_db_path)
    try:
        if reset_existing:
            if pool == "all":
                session.execute(text("DELETE FROM candidate_rescan_queue"))
            else:
                session.execute(
                    text("DELETE FROM candidate_rescan_queue WHERE source_pool = :pool"),
                    {"pool": pool},
                )
            session.commit()

        pools = []
        if pool in {"all", "promoted_rejected"}:
            pools.append("promoted_rejected")
        if pool in {"all", "needs_review_high_score"}:
            pools.append("needs_review_high_score")
        if pool in {"all", "needs_review_all"}:
            pools.append("needs_review_all")
        if pool in {"all", "llm_review_all"}:
            pools.append("llm_review_all")
        if pool in {"all", "queue_candidates_unscanned"}:
            pools.append("queue_candidates_unscanned")
        if pool in {"all", "active_main_rescan"}:
            pools.append("active_main_rescan")
        if not pools:
            return {"inserted": 0, "pool": pool, "error": "unknown_pool"}

        inserted_by_pool: dict[str, int] = {}
        for source_pool in pools:
            before = int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue")) or 0)
            if source_pool == "promoted_rejected":
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'promoted_rejected',
                            m.id,
                            q.id,
                            q.fingerprint,
                            q.status,
                            m.status,
                            q.scan_outcome,
                            json_extract(a.evidence_json, '$.decision'),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.confidence') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL)
                              + (10.0 * CAST(json_extract(a.evidence_json, '$.confidence') AS REAL)),
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'queue.reviewed_verified.promote',
                                'queue_station_name', q.canonical_name,
                                'queue_country_code', q.country_code,
                                'queue_website_url', q.website_url,
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN {alias}.station_submission_assessments a
                          ON a.station_id = q.id
                         AND a.assessment_kind = 'llm_quality_v1'
                        JOIN stations m ON m.fingerprint = q.fingerprint
                        WHERE q.status = 'VERIFIED'
                          AND q.scan_outcome = 'reviewed_verified'
                          AND m.status = 'REJECTED'
                          AND json_extract(a.evidence_json, '$.decision') = 'promote'
                        """
                    ),
                    {"schema_version": CURRENT_RESCAN_SCHEMA_VERSION},
                )
            elif source_pool == "needs_review_high_score":
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'needs_review_high_score',
                            m.id,
                            q.id,
                            q.fingerprint,
                            q.status,
                            m.status,
                            q.scan_outcome,
                            json_extract(a.evidence_json, '$.decision'),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.confidence') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL)
                              + (8.0 * CAST(json_extract(a.evidence_json, '$.confidence') AS REAL)),
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'queue.needs_review.high_score',
                                'queue_station_name', q.canonical_name,
                                'queue_country_code', q.country_code,
                                'queue_website_url', q.website_url,
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN {alias}.station_submission_assessments a
                          ON a.station_id = q.id
                         AND a.assessment_kind = 'llm_quality_v1'
                        JOIN stations m ON m.fingerprint = q.fingerprint
                        WHERE q.scan_outcome = 'needs_review'
                          AND json_extract(a.evidence_json, '$.decision') = 'review'
                          AND CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL) >= :min_review_score
                        """
                    ),
                    {
                        "min_review_score": float(min_review_score),
                        "schema_version": CURRENT_RESCAN_SCHEMA_VERSION,
                    },
                )
            elif source_pool == "needs_review_all":
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO stations (
                            canonical_name, normalized_name, country_code, language, website_url,
                            stream_url, city, status, confidence_score, fingerprint, priority_tier,
                            created_at, updated_at
                        )
                        SELECT
                            q.canonical_name,
                            q.normalized_name,
                            q.country_code,
                            q.language,
                            q.website_url,
                            q.stream_url,
                            q.city,
                            'ARCHIVED',
                            MAX(0.35, MIN(0.62, COALESCE(q.confidence_score, 0.35))),
                            q.fingerprint,
                            0,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        WHERE q.scan_outcome = 'needs_review'
                          AND q.website_url IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM stations m WHERE m.fingerprint = q.fingerprint
                          )
                        """
                    )
                )
                session.commit()
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'needs_review_all',
                            m.id,
                            q.id,
                            q.fingerprint,
                            q.status,
                            m.status,
                            q.scan_outcome,
                            json_extract(a.evidence_json, '$.decision'),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.confidence') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL)
                              + (5.0 * CAST(json_extract(a.evidence_json, '$.confidence') AS REAL))
                              + CASE
                                  WHEN json_extract(a.evidence_json, '$.decision') = 'promote' THEN 15.0
                                  WHEN json_extract(a.evidence_json, '$.decision') = 'review' THEN 5.0
                                  ELSE 0.0
                                END,
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'queue.needs_review.all',
                                'queue_station_name', q.canonical_name,
                                'queue_country_code', q.country_code,
                                'queue_website_url', q.website_url,
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'created_missing_main_placeholder',
                                    CASE
                                        WHEN m.created_at = m.updated_at AND m.status = 'ARCHIVED' THEN 1
                                        ELSE 0
                                    END,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN {alias}.station_submission_assessments a
                          ON a.station_id = q.id
                         AND a.assessment_kind = 'llm_quality_v1'
                        JOIN stations m ON m.fingerprint = q.fingerprint
                        WHERE q.scan_outcome = 'needs_review'
                        """
                    ),
                    {"schema_version": CURRENT_RESCAN_SCHEMA_VERSION},
                )
            elif source_pool == "llm_review_all":
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO stations (
                            canonical_name, normalized_name, country_code, language, website_url,
                            stream_url, city, status, confidence_score, fingerprint, priority_tier,
                            created_at, updated_at
                        )
                        SELECT
                            q.canonical_name,
                            q.normalized_name,
                            q.country_code,
                            q.language,
                            q.website_url,
                            q.stream_url,
                            q.city,
                            'ARCHIVED',
                            MAX(0.30, MIN(0.60, COALESCE(q.confidence_score, 0.35))),
                            q.fingerprint,
                            0,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN {alias}.station_submission_assessments a
                          ON a.station_id = q.id
                         AND a.assessment_kind = 'llm_quality_v1'
                        WHERE json_extract(a.evidence_json, '$.decision') = 'review'
                          AND q.website_url IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM stations m WHERE m.fingerprint = q.fingerprint
                          )
                        """
                    )
                )
                session.commit()
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'llm_review_all',
                            m.id,
                            q.id,
                            q.fingerprint,
                            q.status,
                            m.status,
                            q.scan_outcome,
                            json_extract(a.evidence_json, '$.decision'),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.confidence') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL)
                              + (5.0 * CAST(json_extract(a.evidence_json, '$.confidence') AS REAL))
                              + CASE
                                  WHEN q.scan_outcome = 'needs_review' THEN 8.0
                                  WHEN q.scan_outcome = 'reviewed_rejected' THEN 2.0
                                  ELSE 0.0
                                END,
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'queue.llm.review_all',
                                'queue_station_name', q.canonical_name,
                                'queue_country_code', q.country_code,
                                'queue_website_url', q.website_url,
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN {alias}.station_submission_assessments a
                          ON a.station_id = q.id
                         AND a.assessment_kind = 'llm_quality_v1'
                        JOIN stations m ON m.fingerprint = q.fingerprint
                        WHERE json_extract(a.evidence_json, '$.decision') = 'review'
                          AND q.website_url IS NOT NULL
                        """
                    ),
                    {"schema_version": CURRENT_RESCAN_SCHEMA_VERSION},
                )
            elif source_pool == "queue_candidates_unscanned":
                session.execute(
                    text(
                        f"""
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'queue_candidates_unscanned',
                            m.id,
                            q.id,
                            q.fingerprint,
                            q.status,
                            m.status,
                            q.scan_outcome,
                            NULL,
                            NULL,
                            NULL,
                            50.0 + (10.0 * COALESCE(q.confidence_score, 0.0)),
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'queue.candidate.unscanned',
                                'queue_station_name', q.canonical_name,
                                'queue_country_code', q.country_code,
                                'queue_website_url', q.website_url,
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM {alias}.stations q
                        JOIN stations m ON m.fingerprint = q.fingerprint
                        WHERE q.status = 'CANDIDATE'
                          AND q.website_url IS NOT NULL
                          AND q.scan_last_scanned_at IS NULL
                        """
                    ),
                    {"schema_version": CURRENT_RESCAN_SCHEMA_VERSION},
                )
            elif source_pool == "active_main_rescan":
                session.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO candidate_rescan_queue (
                            source_pool, main_station_id, queue_station_id, queue_fingerprint,
                            old_queue_status, old_main_status, old_scan_outcome, old_decision,
                            old_quality_score, old_confidence, priority_score, status, attempt_count,
                            evidence_json, created_at, updated_at
                        )
                        SELECT
                            'active_main_rescan',
                            m.id,
                            NULL,
                            m.fingerprint,
                            NULL,
                            m.status,
                            'active_main_rescan',
                            json_extract(a.evidence_json, '$.decision'),
                            CAST(json_extract(a.evidence_json, '$.quality_score') AS REAL),
                            CAST(json_extract(a.evidence_json, '$.confidence') AS REAL),
                            CASE
                                WHEN json_extract(a.evidence_json, '$.submission_path_quality') IS NULL
                                  OR json_extract(a.evidence_json, '$.submission_path_quality') = ''
                                    THEN 120.0
                                WHEN m.status = 'VERIFIED' THEN 95.0
                                ELSE 80.0
                            END
                            + (20.0 * COALESCE(m.confidence_score, 0.0))
                            + CASE WHEN m.status = 'VERIFIED' THEN 15.0 ELSE 0.0 END,
                            'pending',
                            0,
                            json_object(
                                'seeded_from', 'main.active_rescan',
                                'main_station_name', m.canonical_name,
                                'main_country_code', m.country_code,
                                'main_website_url', m.website_url,
                                'previous_submission_path_quality', json_extract(a.evidence_json, '$.submission_path_quality'),
                                'previous_accepts_music_submissions', a.accepts_music_submissions,
                                'schema_version', :schema_version
                            ),
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        FROM stations m
                        LEFT JOIN station_submission_assessments a
                          ON a.id = (
                              SELECT max(aa.id)
                              FROM station_submission_assessments aa
                              WHERE aa.station_id = m.id
                                AND aa.assessment_kind = 'llm_quality_v1'
                          )
                        WHERE m.status IN ('CANDIDATE', 'VERIFIED')
                          AND m.website_url IS NOT NULL
                          AND trim(m.website_url) != ''
                        """
                    ),
                    {"schema_version": CURRENT_RESCAN_SCHEMA_VERSION},
                )
            session.commit()
            after = int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue")) or 0)
            inserted_by_pool[source_pool] = max(0, after - before)

        return {
            "queue_db_path": str(_queue_db_path(queue_db_path)),
            "pool": pool,
            "inserted": sum(inserted_by_pool.values()),
            "inserted_by_pool": inserted_by_pool,
            "totals": candidate_rescan_stats(session),
        }
    finally:
        _detach_queue_db(session, alias)


def candidate_rescan_stats(session: Session) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT source_pool, status, COALESCE(result_status, '') AS result_status, COUNT(*) AS n
            FROM candidate_rescan_queue
            GROUP BY source_pool, status, COALESCE(result_status, '')
            ORDER BY source_pool, status, result_status
            """
        )
    ).mappings().all()
    path_rows = session.execute(
        text(
            """
            SELECT COALESCE(path_quality, '') AS path_quality, COALESCE(final_decision, '') AS final_decision, COUNT(*) AS n
            FROM candidate_rescan_queue
            GROUP BY COALESCE(path_quality, ''), COALESCE(final_decision, '')
            ORDER BY n DESC
            """
        )
    ).mappings().all()
    return {
        "total": int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue")) or 0),
        "by_pool_status": [dict(row) for row in rows],
        "by_path_decision": [dict(row) for row in path_rows],
    }


def list_candidate_rescan_queue(
    session: Session,
    *,
    status: str = "pending",
    pool: str = "",
    limit: int = 50,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    where = ["q.status = :status"] if status else ["1=1"]
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if status:
        params["status"] = status
    if pool:
        where.append("q.source_pool = :pool")
        params["pool"] = pool
    if not include_archived:
        where.append("s.status != 'ARCHIVED'")
    rows = session.execute(
        text(
            f"""
            SELECT
                q.id, q.source_pool, q.status, q.main_station_id, q.queue_station_id,
                s.status AS main_status, s.canonical_name, s.country_code, s.website_url,
                q.old_decision, q.old_quality_score, q.old_confidence, q.priority_score,
                q.final_decision, q.path_quality, q.result_status, q.notes
            FROM candidate_rescan_queue q
            JOIN stations s ON s.id = q.main_station_id
            WHERE {' AND '.join(where)}
            ORDER BY q.priority_score DESC, q.old_quality_score DESC, q.id ASC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def _latest_assessment(session: Session, station_id: int) -> StationSubmissionAssessment | None:
    return session.scalar(
        select(StationSubmissionAssessment)
        .where(
            StationSubmissionAssessment.station_id == station_id,
            StationSubmissionAssessment.assessment_kind == settings.station_quality_assessment_kind,
        )
        .order_by(StationSubmissionAssessment.updated_at.desc(), StationSubmissionAssessment.id.desc())
    )


def _path_quality_from_assessment(assessment: StationSubmissionAssessment | None) -> tuple[str, dict[str, Any]]:
    if assessment is None:
        return "unknown", {}
    payload = _json_loads(assessment.evidence_json, {})
    features = payload.get("features") if isinstance(payload, dict) else {}
    if not isinstance(features, dict):
        features = {}
    path_quality = str(features.get("submission_path_quality") or "").strip()
    return path_quality or "unknown", payload if isinstance(payload, dict) else {}


def _rescan_result_status(
    *,
    station: Station,
    assessment: StationSubmissionAssessment | None,
    promote_contact_only: bool,
) -> tuple[str, str]:
    path_quality, payload = _path_quality_from_assessment(assessment)
    decision = str(payload.get("decision") or "").strip().lower()
    quality_score = float(payload.get("quality_score") or 0.0)
    confidence = float(payload.get("confidence") or 0.0)
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    is_real = bool(verdict.get("is_real_station") or (assessment and assessment.is_real_station))
    is_aggregator = bool(features.get("is_aggregator_domain"))
    topical_exclusion_labels = [str(x) for x in (features.get("topical_exclusion_labels") or [])]
    reason_codes = [str(x).lower() for x in payload.get("reason_codes", [])] if isinstance(payload.get("reason_codes"), list) else []
    has_format_reject = bool(topical_exclusion_labels) or any(code.startswith("format_rejected:") for code in reason_codes)

    if decision == "reject" or not is_real:
        return "rejected", path_quality
    if path_quality in {"direct_music_form", "gated_music_form", "explicit_submission_email"} and decision == "promote":
        if is_aggregator or has_format_reject:
            return "needs_manual_review", path_quality
        if not _has_confirmed_best_route(station, payload, path_quality):
            return "needs_manual_review", path_quality
        return "verified", path_quality
    if (
        path_quality == "contact_email_only"
        and is_real
        and not is_aggregator
        and not has_format_reject
        and (quality_score >= 40 or confidence >= 0.6)
    ):
        return ("verified_contact_only" if promote_contact_only else "candidate_contact_only"), path_quality
    if decision == "review":
        return "needs_manual_review", path_quality
    if decision == "promote":
        return "needs_manual_review", path_quality
    return "candidate_review", path_quality


def run_candidate_rescan_batch(
    session: Session,
    *,
    limit: int = 10,
    pool: str = "",
    max_pages: int = 6,
    provider: str | None = None,
    apply_status: bool = False,
    promote_contact_only: bool = False,
    dry_run: bool = True,
    include_archived: bool = False,
) -> dict[str, Any]:
    rows = list_candidate_rescan_queue(
        session=session,
        status="pending",
        pool=pool,
        limit=limit,
        include_archived=include_archived,
    )
    if dry_run:
        return {
            "dry_run": True,
            "selected": len(rows),
            "rows": rows[: min(len(rows), 25)],
            "stats": candidate_rescan_stats(session),
        }

    claim_token = uuid.uuid4().hex
    processed = 0
    scans_created = 0
    assessments_saved = 0
    errors = 0
    result_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    for row in rows:
        queue_row = session.get(CandidateRescanQueue, int(row["id"]))
        if queue_row is None or queue_row.status != "pending":
            continue
        station = session.get(Station, int(queue_row.main_station_id))
        if station is None:
            queue_row.status = "error"
            queue_row.result_status = "station_missing"
            queue_row.notes = "Main station missing"
            queue_row.updated_at = datetime.utcnow()
            session.commit()
            errors += 1
            continue
        queue_row.status = "running"
        queue_row.claim_token = claim_token
        queue_row.claimed_at = datetime.utcnow()
        queue_row.attempt_count = int(queue_row.attempt_count or 0) + 1
        session.commit()

        source_pool = str(row.get("source_pool") or queue_row.source_pool or "")
        original_status = station.status
        original_confidence = float(station.confidence_score or 0.0)
        try:
            if station.status in {StationStatus.REJECTED, StationStatus.ARCHIVED}:
                station.status = StationStatus.CANDIDATE
                station.confidence_score = max(float(station.confidence_score or 0.0), 0.45)
                session.add(station)
                session.commit()

            run = start_manual_scan_run(
                session=session,
                station_id=int(station.id),
                max_pages=max(1, int(max_pages)),
                force_rescan=True,
            )
            scans_created += 1

            llm_result = run_station_quality_verification(
                session=session,
                limit=1,
                station_id=int(station.id),
                provider=provider or settings.station_quality_provider,
                only_unassessed=False,
                apply=True,
            )
            assessments_saved += int(llm_result.get("saved", 0) or 0)
            assessment = _latest_assessment(session=session, station_id=int(station.id))
            result_status, path_quality = _rescan_result_status(
                station=station,
                assessment=assessment,
                promote_contact_only=promote_contact_only,
            )
            path_quality, assessment_payload = _path_quality_from_assessment(assessment)
            final_decision = str(assessment_payload.get("decision") or "").strip().lower() or None
            if run.status == "blocked":
                result_status = "needs_manual_review"
                final_decision = "review"
            if result_status in PROMOTABLE_RESCAN_STATUSES and _looks_like_non_station_entrypoint(
                station=station,
                assessment_payload=assessment_payload,
            ):
                result_status = "needs_manual_review"
                final_decision = "review"
            duplicate_match = None
            if result_status in PROMOTABLE_RESCAN_STATUSES:
                duplicate_match = _find_promotion_duplicate(
                    session=session,
                    station=station,
                    queue_id=int(queue_row.id),
                )
                if duplicate_match is not None:
                    result_status = (
                        "network_sibling_review"
                        if duplicate_match.get("match_kind") == "network_sibling"
                        else "duplicate_review"
                    )
                    final_decision = "review"
            if source_pool == "active_main_rescan" and result_status == "rejected":
                result_status = "needs_manual_review"
                final_decision = "review"

            if apply_status:
                if result_status == "verified":
                    station.status = StationStatus.VERIFIED
                    station.priority_tier = max(1, int(station.priority_tier or 0))
                    station.confidence_score = max(float(station.confidence_score or 0.0), 0.8)
                elif result_status == "verified_contact_only":
                    station.status = StationStatus.VERIFIED
                    station.priority_tier = max(1, int(station.priority_tier or 0))
                    station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.68), 0.58)
                elif result_status == "candidate_contact_only":
                    station.status = StationStatus.CANDIDATE
                    station.priority_tier = max(2, int(station.priority_tier or 0))
                    station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.68), 0.55)
                elif result_status in {"needs_manual_review", "candidate_review", "duplicate_review", "network_sibling_review"}:
                    station.status = original_status
                    if source_pool == "active_main_rescan":
                        station.confidence_score = original_confidence
                    else:
                        station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.62), 0.35)
                elif result_status == "rejected":
                    station.status = StationStatus.REJECTED
                    station.confidence_score = min(float(station.confidence_score or 0.0), 0.25)
                session.add(station)
            else:
                station.status = original_status
                session.add(station)

            queue_row.status = "done"
            queue_row.claim_token = None
            queue_row.claimed_at = None
            queue_row.last_run_id = int(run.id)
            queue_row.last_assessment_id = int(assessment.id) if assessment is not None else None
            queue_row.final_decision = final_decision
            queue_row.path_quality = path_quality
            queue_row.result_status = result_status
            queue_row.notes = (
                f"rescan_status={result_status}; path_quality={path_quality}; "
                f"station_status={station.status.value if isinstance(station.status, StationStatus) else station.status}"
                + (
                    f"; duplicate={duplicate_match.get('match_kind')}:{duplicate_match.get('source')}:"
                    f"{duplicate_match.get('station_id')}"
                    if duplicate_match
                    else ""
                )
            )[:500]
            queue_row.evidence_json = json.dumps(
                {
                    **(_json_loads(queue_row.evidence_json, {}) if queue_row.evidence_json else {}),
                    "last_rescan": {
                        "run_id": int(run.id),
                        "run_status": run.status,
                        "run_blocked_reason": run.blocked_reason,
                        "llm_result": llm_result,
                        "assessment_payload": assessment_payload,
                        "duplicate_match": duplicate_match,
                        "apply_status": apply_status,
                        "promote_contact_only": promote_contact_only,
                        "rescan_at": datetime.utcnow().isoformat() + "Z",
                    },
                    "schema_version": CURRENT_RESCAN_SCHEMA_VERSION,
                },
                ensure_ascii=False,
            )
            session.add(queue_row)
            session.commit()

            processed += 1
            result_counts[result_status] = result_counts.get(result_status, 0) + 1
            if len(samples) < 20:
                samples.append(
                    {
                        "queue_id": queue_row.id,
                        "station_id": station.id,
                        "name": station.canonical_name,
                        "run_status": run.status,
                        "final_decision": final_decision,
                        "path_quality": path_quality,
                        "result_status": result_status,
                    }
                )
        except Exception as exc:
            session.rollback()
            errors += 1
            station = session.get(Station, int(row["main_station_id"]))
            if station is not None and not apply_status:
                station.status = original_status
                session.add(station)
            queue_row = session.get(CandidateRescanQueue, int(row["id"]))
            if queue_row is not None:
                queue_row.status = "error"
                queue_row.claim_token = None
                queue_row.claimed_at = None
                queue_row.result_status = "error"
                queue_row.notes = f"{type(exc).__name__}: {str(exc)[:400]}"
                queue_row.updated_at = datetime.utcnow()
                session.add(queue_row)
            session.commit()

    return {
        "dry_run": False,
        "selected": len(rows),
        "processed": processed,
        "scans_created": scans_created,
        "assessments_saved": assessments_saved,
        "errors": errors,
        "result_counts": result_counts,
        "samples": samples,
        "stats": candidate_rescan_stats(session),
    }


def _claim_candidate_rescan_row(
    session: Session,
    *,
    pool: str,
    include_archived: bool,
    claim_token: str,
) -> dict[str, Any] | None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        archived_filter = "" if include_archived else "AND s.status != 'ARCHIVED'"
        pool_filter = "AND q.source_pool = :pool" if pool else ""
        params: dict[str, Any] = {"claim_token": claim_token}
        if pool:
            params["pool"] = pool
        row = session.execute(
            text(
                f"""
                WITH picked AS (
                    SELECT q.id
                    FROM candidate_rescan_queue q
                    JOIN stations s ON s.id = q.main_station_id
                    WHERE q.status = 'pending'
                      {pool_filter}
                      {archived_filter}
                    ORDER BY q.priority_score DESC, q.old_quality_score DESC, q.id ASC
                    FOR UPDATE OF q SKIP LOCKED
                    LIMIT 1
                )
                UPDATE candidate_rescan_queue q
                SET status = 'running',
                    claim_token = :claim_token,
                    claimed_at = now(),
                    attempt_count = COALESCE(q.attempt_count, 0) + 1,
                    updated_at = now()
                FROM picked
                WHERE q.id = picked.id
                RETURNING q.id, q.source_pool, q.main_station_id
                """
            ),
            params,
        ).mappings().first()
        if row is None:
            session.commit()
            return None
        session.commit()
        return {
            "id": int(row["id"]),
            "source_pool": str(row["source_pool"] or ""),
            "main_station_id": int(row["main_station_id"]),
        }

    rows = list_candidate_rescan_queue(
        session=session,
        status="pending",
        pool=pool,
        limit=1,
        include_archived=include_archived,
    )
    if not rows:
        return None
    row = rows[0]
    queue_row = session.get(CandidateRescanQueue, int(row["id"]))
    if queue_row is None or queue_row.status != "pending":
        return None
    station = session.get(Station, int(queue_row.main_station_id))
    if station is None:
        queue_row.status = "error"
        queue_row.result_status = "station_missing"
        queue_row.notes = "Main station missing"
        queue_row.updated_at = datetime.utcnow()
        session.add(queue_row)
        session.commit()
        return None
    queue_row.status = "running"
    queue_row.claim_token = claim_token
    queue_row.claimed_at = datetime.utcnow()
    queue_row.attempt_count = int(queue_row.attempt_count or 0) + 1
    session.add(queue_row)
    session.commit()
    return {
        "id": int(queue_row.id),
        "source_pool": str(queue_row.source_pool or ""),
        "main_station_id": int(queue_row.main_station_id),
    }


def _process_claimed_candidate_rescan_row(
    session: Session,
    *,
    queue_id: int,
    claim_token: str,
    max_pages: int,
    provider: str | None,
    apply_status: bool,
    promote_contact_only: bool,
) -> dict[str, Any]:
    queue_row = session.get(CandidateRescanQueue, int(queue_id))
    if queue_row is None or queue_row.status != "running" or queue_row.claim_token != claim_token:
        return {"processed": 0, "scans_created": 0, "assessments_saved": 0, "errors": 0, "skipped": 1}
    source_pool = str(queue_row.source_pool or "")
    station = session.get(Station, int(queue_row.main_station_id))
    if station is None:
        queue_row.status = "error"
        queue_row.claim_token = None
        queue_row.claimed_at = None
        queue_row.result_status = "station_missing"
        queue_row.notes = "Main station missing"
        queue_row.updated_at = datetime.utcnow()
        session.add(queue_row)
        session.commit()
        return {"processed": 0, "scans_created": 0, "assessments_saved": 0, "errors": 1, "skipped": 0}

    original_status = station.status
    original_confidence = float(station.confidence_score or 0.0)
    scans_created = 0
    assessments_saved = 0
    try:
        use_direct_scan = (
            not is_supported_station_target_url(station.website_url)
            or station_matches_excluded_focus(station)
            or station_matches_excluded_meta(station)
            or station_matches_entrypoint_only(station)
        )
        candidate_urls: list[tuple[str, list[str]]] = []
        extraction: dict[str, Any] | None = None
        if not use_direct_scan:
            candidate_urls = _candidate_urls_for_station(
                station,
                max_urls=max(1, int(max_pages) * 3, settings.browser_deep_max_urls_per_station),
            )
            candidate_urls = _rank_candidate_scan_urls(candidate_urls)
            selected_urls = candidate_urls[: max(1, int(max_pages))]
            extraction = _extract_rejected_scan_payload(
                {
                    "station_id": int(station.id),
                    "station_name": station.canonical_name,
                    "website_url": station.website_url,
                    "candidate_urls": selected_urls,
                    "max_pages": max(1, int(max_pages)),
                }
            )

        with CANDIDATE_RESCAN_WRITE_LOCK:
            station = session.get(Station, int(queue_row.main_station_id))
            if station is None:
                raise RuntimeError("station_missing_after_scan")
            if station.status in {StationStatus.REJECTED, StationStatus.ARCHIVED}:
                station.status = StationStatus.CANDIDATE
                station.confidence_score = max(float(station.confidence_score or 0.0), 0.45)
                session.add(station)
                session.commit()

            if use_direct_scan:
                run = start_manual_scan_run(
                    session=session,
                    station_id=int(station.id),
                    max_pages=max(1, int(max_pages)),
                    force_rescan=True,
                )
            else:
                run = _persist_extracted_scan_run(
                    session=session,
                    station_id=int(station.id),
                    candidate_urls=candidate_urls[: max(1, int(max_pages))],
                    pages=(extraction or {}).get("pages") or [],
                    max_pages=max(1, int(max_pages)),
                    force_rescan=True,
                )
            scans_created += 1

            llm_result = run_station_quality_verification(
                session=session,
                limit=1,
                station_id=int(station.id),
                provider=provider or settings.station_quality_provider,
                only_unassessed=False,
                apply=True,
            )
            assessments_saved += int(llm_result.get("saved", 0) or 0)
            assessment = _latest_assessment(session=session, station_id=int(station.id))
            result_status, path_quality = _rescan_result_status(
                station=station,
                assessment=assessment,
                promote_contact_only=promote_contact_only,
            )
            path_quality, assessment_payload = _path_quality_from_assessment(assessment)
            final_decision = str(assessment_payload.get("decision") or "").strip().lower() or None
            if run.status == "blocked":
                result_status = "needs_manual_review"
                final_decision = "review"
            if result_status in PROMOTABLE_RESCAN_STATUSES and _looks_like_non_station_entrypoint(
                station=station,
                assessment_payload=assessment_payload,
            ):
                result_status = "needs_manual_review"
                final_decision = "review"
            duplicate_match = None
            if result_status in PROMOTABLE_RESCAN_STATUSES:
                duplicate_match = _find_promotion_duplicate(
                    session=session,
                    station=station,
                    queue_id=int(queue_row.id),
                )
                if duplicate_match is not None:
                    result_status = (
                        "network_sibling_review"
                        if duplicate_match.get("match_kind") == "network_sibling"
                        else "duplicate_review"
                    )
                    final_decision = "review"
            if source_pool == "active_main_rescan" and result_status == "rejected":
                result_status = "needs_manual_review"
                final_decision = "review"

            if apply_status:
                if result_status == "verified":
                    station.status = StationStatus.VERIFIED
                    station.priority_tier = max(1, int(station.priority_tier or 0))
                    station.confidence_score = max(float(station.confidence_score or 0.0), 0.8)
                elif result_status == "verified_contact_only":
                    station.status = StationStatus.VERIFIED
                    station.priority_tier = max(1, int(station.priority_tier or 0))
                    station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.68), 0.58)
                elif result_status == "candidate_contact_only":
                    station.status = StationStatus.CANDIDATE
                    station.priority_tier = max(2, int(station.priority_tier or 0))
                    station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.68), 0.55)
                elif result_status in {"needs_manual_review", "candidate_review", "duplicate_review", "network_sibling_review"}:
                    station.status = original_status
                    if source_pool == "active_main_rescan":
                        station.confidence_score = original_confidence
                    else:
                        station.confidence_score = max(min(float(station.confidence_score or 0.0), 0.62), 0.35)
                elif result_status == "rejected":
                    station.status = StationStatus.REJECTED
                    station.confidence_score = min(float(station.confidence_score or 0.0), 0.25)
                session.add(station)
            else:
                station.status = original_status
                station.confidence_score = original_confidence
                session.add(station)

            queue_row.status = "done"
            queue_row.claim_token = None
            queue_row.claimed_at = None
            queue_row.last_run_id = int(run.id)
            queue_row.last_assessment_id = int(assessment.id) if assessment is not None else None
            queue_row.final_decision = final_decision
            queue_row.path_quality = path_quality
            queue_row.result_status = result_status
            queue_row.notes = (
                f"rescan_status={result_status}; path_quality={path_quality}; "
                f"station_status={station.status.value if isinstance(station.status, StationStatus) else station.status}"
                + (
                    f"; duplicate={duplicate_match.get('match_kind')}:{duplicate_match.get('source')}:"
                    f"{duplicate_match.get('station_id')}"
                    if duplicate_match
                    else ""
                )
            )[:500]
            queue_row.evidence_json = json.dumps(
                {
                    **(_json_loads(queue_row.evidence_json, {}) if queue_row.evidence_json else {}),
                    "last_rescan": {
                        "run_id": int(run.id),
                        "run_status": run.status,
                        "run_blocked_reason": run.blocked_reason,
                        "llm_result": llm_result,
                        "assessment_payload": assessment_payload,
                        "duplicate_match": duplicate_match,
                        "apply_status": apply_status,
                        "promote_contact_only": promote_contact_only,
                        "rescan_at": datetime.utcnow().isoformat() + "Z",
                    },
                    "schema_version": CURRENT_RESCAN_SCHEMA_VERSION,
                },
                ensure_ascii=False,
            )
            session.add(queue_row)
            session.commit()
        return {
            "processed": 1,
            "scans_created": scans_created,
            "assessments_saved": assessments_saved,
            "errors": 0,
            "skipped": 0,
            "result_status": result_status,
            "sample": {
                "queue_id": queue_row.id,
                "station_id": station.id,
                "name": station.canonical_name,
                "run_status": run.status,
                "final_decision": final_decision,
                "path_quality": path_quality,
                "result_status": result_status,
                "worker_claim_token": claim_token,
            },
        }
    except Exception as exc:
        session.rollback()
        with CANDIDATE_RESCAN_WRITE_LOCK:
            station = session.get(Station, int(queue_row.main_station_id))
            if station is not None:
                station.status = original_status
                station.confidence_score = original_confidence
                session.add(station)
            queue_row = session.get(CandidateRescanQueue, int(queue_id))
            if queue_row is not None:
                queue_row.status = "error"
                queue_row.claim_token = None
                queue_row.claimed_at = None
                queue_row.result_status = "error"
                queue_row.notes = f"{type(exc).__name__}: {str(exc)[:400]}"
                queue_row.updated_at = datetime.utcnow()
                session.add(queue_row)
            session.commit()
        return {
            "processed": 0,
            "scans_created": scans_created,
            "assessments_saved": assessments_saved,
            "errors": 1,
            "skipped": 0,
            "result_status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
        }


def run_candidate_rescan_continuous(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 12,
    pool: str = "",
    max_pages: int = 6,
    workers: int = 3,
    provider: str | None = None,
    apply_status: bool = False,
    promote_contact_only: bool = False,
    include_archived: bool = False,
) -> dict[str, Any]:
    max_workers = max(1, int(workers or 1))
    target_limit = max(1, int(limit or 1))
    max_pages = max(1, int(max_pages or 1))
    state = {"claimed": 0}

    def _worker(worker_index: int) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "worker": worker_index,
            "claimed": 0,
            "processed": 0,
            "scans_created": 0,
            "assessments_saved": 0,
            "errors": 0,
            "skipped": 0,
            "result_counts": {},
            "samples": [],
        }
        while True:
            claim_token = f"candidate-worker-{worker_index}-{uuid.uuid4().hex}"
            queue_id: int | None = None
            with CANDIDATE_RESCAN_WRITE_LOCK:
                if state["claimed"] >= target_limit:
                    break
                try:
                    with session_factory() as session:
                        row = _claim_candidate_rescan_row(
                            session=session,
                            pool=pool,
                            include_archived=include_archived,
                            claim_token=claim_token,
                        )
                        if row is None:
                            break
                        state["claimed"] += 1
                        stats["claimed"] += 1
                        queue_id = int(row["id"])
                except Exception as exc:
                    stats["errors"] += 1
                    counts = stats["result_counts"]
                    counts["claim_error"] = int(counts.get("claim_error", 0) or 0) + 1
                    if len(stats["samples"]) < 10:
                        stats["samples"].append(
                            {
                                "worker": worker_index,
                                "result_status": "claim_error",
                                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                            }
                        )
                    break

            if queue_id is None:
                break
            with session_factory() as session:
                result = _process_claimed_candidate_rescan_row(
                    session=session,
                    queue_id=queue_id,
                    claim_token=claim_token,
                    max_pages=max_pages,
                    provider=provider,
                    apply_status=apply_status,
                    promote_contact_only=promote_contact_only,
                )
            stats["processed"] += int(result.get("processed", 0) or 0)
            stats["scans_created"] += int(result.get("scans_created", 0) or 0)
            stats["assessments_saved"] += int(result.get("assessments_saved", 0) or 0)
            stats["errors"] += int(result.get("errors", 0) or 0)
            stats["skipped"] += int(result.get("skipped", 0) or 0)
            result_status = str(result.get("result_status") or "")
            if result_status:
                counts = stats["result_counts"]
                counts[result_status] = int(counts.get(result_status, 0) or 0) + 1
            if result.get("sample") and len(stats["samples"]) < 10:
                stats["samples"].append(result["sample"])
        return stats

    worker_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, index) for index in range(1, max_workers + 1)]
        for future in as_completed(futures):
            worker_results.append(future.result())

    result_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for item in worker_results:
        for key, value in (item.get("result_counts") or {}).items():
            result_counts[str(key)] = result_counts.get(str(key), 0) + int(value or 0)
        for sample in item.get("samples") or []:
            if len(samples) < 20:
                samples.append(sample)
    with session_factory() as session:
        stats = candidate_rescan_stats(session=session)
    return {
        "mode": "candidate_rescan_continuous",
        "workers": max_workers,
        "target_limit": target_limit,
        "claimed": sum(int(item.get("claimed", 0) or 0) for item in worker_results),
        "processed": sum(int(item.get("processed", 0) or 0) for item in worker_results),
        "scans_created": sum(int(item.get("scans_created", 0) or 0) for item in worker_results),
        "assessments_saved": sum(int(item.get("assessments_saved", 0) or 0) for item in worker_results),
        "errors": sum(int(item.get("errors", 0) or 0) for item in worker_results),
        "skipped": sum(int(item.get("skipped", 0) or 0) for item in worker_results),
        "result_counts": result_counts,
        "samples": samples,
        "worker_results": worker_results,
        "stats": stats,
    }

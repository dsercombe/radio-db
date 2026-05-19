from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.connectors.http import get_text
from radio_db.models.entities import Evidence, SourceType, Station, StationProgram, StationStatus, SubmissionChannel


NON_STATION_DOMAIN_HINTS = (
    "onlineradiobox",
    "radio.net",
    "mytuner-radio",
    "internet-radio",
    "radio.fr",
    "radio-espana.es",
    "radio-en-ligne.fr",
    "apps.apple.com",
    "play.google.com",
    "youtube.com",
    "facebook.com",
    "reddit.com",
    "groover.co",
    "one-submit.com",
    "musicgateway.com",
    "livescore.",
    "wpcomstaging.com",
)

NON_STATION_PATH_HINTS = (
    "/genre/",
    "/country/",
    "/language/",
    "/topic/",
    "/category/",
)

RADIO_SIGNAL_HINTS = (
    "radio station",
    "listen live",
    "now playing",
    "on air",
    "web radio",
    "playlist",
    "program",
    "schedule",
    "stream",
)

PLATFORM_PROXY_DOMAIN_HINTS = (
    "zeno.fm",
    "qtfm.cn",
    "qingting.fm",
    "ximalaya.com",
    "vk.com",
    "stream.zeno.fm",
    "lhttp.qtfm.cn",
    "lhttp-hw.qtfm.cn",
)


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _path(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).path or "").lower()


def _is_url_like_name(name: str | None) -> bool:
    if not name:
        return True
    lowered = name.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://") or lowered.startswith("www.")


def _is_non_station_domain(url: str | None) -> bool:
    d = _domain(url)
    return bool(d) and any(h in d for h in NON_STATION_DOMAIN_HINTS)


def _is_platform_proxy_domain(url: str | None) -> bool:
    d = _domain(url)
    return bool(d) and any(h in d for h in PLATFORM_PROXY_DOMAIN_HINTS)


def _is_tv_like_domain(url: str | None) -> bool:
    d = _domain(url)
    if not d:
        return False
    return (
        d.endswith(".tv")
        or ".tv." in d
        or d.startswith("tv")
        or "tvradio" in d
        or d.startswith("rtv")
        or ".rtv" in d
    )


def _has_non_station_path(url: str | None) -> bool:
    p = _path(url)
    return bool(p) and any(h in p for h in NON_STATION_PATH_HINTS)


def _looks_like_stream(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(h in lowered for h in (".mp3", ".aac", ".m3u", ".pls", "stream", "icecast", "shoutcast"))


@dataclass
class VerifyResult:
    station_id: int
    score: float
    verdict: str
    reason: str


def verify_real_stations(
    session: Session,
    *,
    limit: int = 2000,
    station_id: int | None = None,
    only_candidates: bool = True,
    fetch_homepages: bool = True,
    max_page_fetches: int = 300,
    apply: bool = False,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
    checkpoint_path: Path | None = None,
) -> dict:
    checkpoint_file = checkpoint_path or (Path(".radio_db_state") / "verify_checkpoint.json")
    checkpoint = {"last_station_id": 0, "updated_at": None, "completed": False}
    if station_id is None and use_checkpoint and checkpoint_file.exists() and not reset_checkpoint:
        try:
            payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            checkpoint["last_station_id"] = int(payload.get("last_station_id", 0))
            checkpoint["updated_at"] = payload.get("updated_at")
            checkpoint["completed"] = bool(payload.get("completed", False))
        except Exception:
            checkpoint = {"last_station_id": 0, "updated_at": None, "completed": False}
    if reset_checkpoint:
        checkpoint = {"last_station_id": 0, "updated_at": None, "completed": False}

    stmt = select(Station)
    if station_id is not None:
        stmt = stmt.where(Station.id == station_id)
    else:
        if only_candidates:
            stmt = stmt.where(Station.status == StationStatus.CANDIDATE)
        else:
            stmt = stmt.where(Station.status != StationStatus.REJECTED)
        if use_checkpoint:
            stmt = stmt.where(Station.id > int(checkpoint.get("last_station_id", 0)))
            stmt = stmt.order_by(Station.id.asc()).limit(limit)
        else:
            stmt = stmt.order_by(Station.updated_at.desc()).limit(limit)
    stations = session.scalars(stmt).all()

    if station_id is None and use_checkpoint and not stations:
        checkpoint["completed"] = True
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_file.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
        return {
            "checked": 0,
            "keep": 0,
            "reject": 0,
            "ambiguous": 0,
            "applied_verified": 0,
            "applied_rejects": 0,
            "only_candidates": only_candidates,
            "fetch_homepages": fetch_homepages,
            "page_fetches": 0,
            "checkpoint_enabled": True,
            "checkpoint_path": str(checkpoint_file),
            "checkpoint_last_station_id": checkpoint["last_station_id"],
            "checkpoint_completed": True,
            "sample": [],
        }
    station_ids = [st.id for st in stations]
    submission_counts: dict[int, int] = {}
    program_counts: dict[int, int] = {}
    if station_ids:
        submission_counts = {
            sid: cnt
            for sid, cnt in session.execute(
                select(SubmissionChannel.station_id, func.count(SubmissionChannel.id))
                .where(SubmissionChannel.station_id.in_(station_ids))
                .group_by(SubmissionChannel.station_id)
            ).all()
        }
        program_counts = {
            sid: cnt
            for sid, cnt in session.execute(
                select(StationProgram.station_id, func.count(StationProgram.id))
                .where(StationProgram.station_id.in_(station_ids))
                .group_by(StationProgram.station_id)
            ).all()
        }

    checked = 0
    keep = 0
    reject = 0
    ambiguous = 0
    applied_verified = 0
    applied_rejects = 0
    page_fetches = 0
    sample: list[VerifyResult] = []

    for st in stations:
        checked += 1
        reasons: list[str] = []
        score = 0.0

        has_stream = _looks_like_stream(st.stream_url)
        if has_stream:
            score += 0.45
            reasons.append("has_stream")

        sub_count = submission_counts.get(st.id, 0)
        if sub_count > 0:
            score += 0.25
            reasons.append("has_submission")

        program_count = program_counts.get(st.id, 0)
        if program_count > 0:
            score += 0.10
            reasons.append("has_program")

        if (st.confidence_score or 0.0) >= 0.65:
            score += 0.10
            reasons.append("high_confidence")

        if _is_non_station_domain(st.website_url):
            score -= 0.60
            reasons.append("non_station_domain")
        if _has_non_station_path(st.website_url):
            score -= 0.20
            reasons.append("non_station_path")
        if _is_url_like_name(st.canonical_name):
            score -= 0.40
            reasons.append("url_like_name")

        if fetch_homepages and page_fetches < max_page_fetches and st.website_url and not _is_non_station_domain(st.website_url):
            try:
                text = get_text(st.website_url)[:12000].lower()
                page_fetches += 1
                if any(h in text for h in RADIO_SIGNAL_HINTS):
                    score += 0.20
                    reasons.append("homepage_radio_signals")
            except Exception:
                reasons.append("homepage_unreachable")

        verdict = "keep"
        # Hard safety gate: known aggregator/directory domains can never be auto-kept.
        if _is_non_station_domain(st.website_url):
            score = min(score, 0.44)
            reasons.append("domain_keep_blocked")
        # Platform/proxy pages without strong station facts should stay ambiguous.
        if _is_platform_proxy_domain(st.website_url) and sub_count == 0 and program_count == 0:
            score = min(score, 0.44)
            reasons.append("platform_keep_blocked")
        if _is_tv_like_domain(st.website_url) and sub_count == 0 and program_count == 0:
            score = min(score, 0.44)
            reasons.append("tv_keep_blocked")
        if score < 0.20:
            verdict = "reject"
        elif score < 0.45:
            verdict = "ambiguous"

        if verdict == "keep":
            keep += 1
            if apply:
                # Legacy verifier is evidence-only for keep decisions. Promotion now
                # belongs exclusively to candidate_rescan's Playwright + LLM gate.
                pass
        elif verdict == "reject":
            reject += 1
            if apply and st.status != StationStatus.REJECTED:
                st.status = StationStatus.REJECTED
                st.confidence_score = min(st.confidence_score or 0.0, 0.30)
                applied_rejects += 1
        else:
            ambiguous += 1

        payload = {"score": round(score, 3), "verdict": verdict, "reasons": reasons}
        session.add(
            Evidence(
                station_id=st.id,
                source_type=SourceType.WEBSITE,
                source_url=st.website_url,
                source_id="quality_verify",
                raw_title=st.canonical_name,
                raw_snippet=";".join(reasons)[:500],
                extracted_payload_json=json.dumps(payload),
                confidence=max(0.0, min(1.0, score)),
            )
        )

        if len(sample) < 30:
            sample.append(
                VerifyResult(
                    station_id=st.id,
                    score=round(score, 3),
                    verdict=verdict,
                    reason=", ".join(reasons[:5]),
                )
            )

    if apply:
        session.commit()
    else:
        session.rollback()

    if station_id is None and use_checkpoint and stations:
        checkpoint["last_station_id"] = max(st.id for st in stations)
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint["completed"] = False
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_file.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")

    return {
        "checked": checked,
        "keep": keep,
        "reject": reject,
        "ambiguous": ambiguous,
        "applied_verified": applied_verified,
        "applied_rejects": applied_rejects,
        "only_candidates": only_candidates,
        "fetch_homepages": fetch_homepages,
        "page_fetches": page_fetches,
        "checkpoint_enabled": bool(use_checkpoint and station_id is None),
        "checkpoint_path": str(checkpoint_file),
        "checkpoint_last_station_id": checkpoint.get("last_station_id", 0),
        "checkpoint_completed": bool(checkpoint.get("completed", False)),
        "sample": [
            {
                "station_id": row.station_id,
                "score": row.score,
                "verdict": row.verdict,
                "reason": row.reason,
            }
            for row in sample
        ],
    }

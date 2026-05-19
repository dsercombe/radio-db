from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.connectors.radio_browser import fetch_station_seeds, fetch_station_seeds_page
from radio_db.connectors.wikidata import fetch_radio_stations, fetch_radio_stations_page
from radio_db.llm.extractor import choose_llm_provider, extract_page_text_for_prompt, extract_station_from_text
from radio_db.models.entities import (
    ContactRole,
    CrawlFrontier,
    Evidence,
    SourceType,
    Station,
    StationAlias,
    StationContact,
    StationGenre,
    StationProfileSnapshot,
    StationPerson,
    StationProgram,
    StationStatus,
    SubmissionForm,
    SubmissionFormField,
    FormRecipe,
    SubmissionChannel,
    SubmissionMethod,
)
from radio_db.services.dedupe import find_duplicate_station, station_fingerprint
from radio_db.services.budget import CostGuard
from radio_db.services.frontier import claim_queries, refresh_priorities
from radio_db.services.normalize import normalize_country, normalize_text
from radio_db.services.people import upsert_station_person
from radio_db.services.source_search import collect_search_results, collect_station_enrichment_results
from radio_db.services.query_templates import generate_queries, generate_queries_for_country

NON_STATION_DOMAIN_HINTS = (
    "one-submit.com",
    "musicgateway.com",
    "groover.co",
    "blog.groover.co",
    "onlineradiobox.com",
    "radio.net",
    "mytuner-radio.com",
    "internet-radio.com",
    "radio.fr",
    "radio-espana.es",
    "radio-en-ligne.fr",
    "apps.apple.com",
    "play.google.com",
    "youtube.com",
    "facebook.com",
    "reddit.com",
    "submit",
    "submitmusic",
    "musicpromo",
    "playlistpush",
    "submithub",
    "ditto",
    "distribution",
)

NON_STATION_TEXT_HINTS = (
    "submit your music",
    "music distribution",
    "playlist pitching",
    "promotion service",
    "marketing service",
    "top radio stations",
    "listen to ",
    "radio stations from ",
    "app store",
    "google play",
    "download on the app store",
)

NON_STATION_URL_HINTS = (
    "/genre/",
    "/country/",
    "/language/",
    "/category/",
    "/stations",
)

RADIO_SIGNAL_HINTS = (
    "radio station",
    "fm",
    "am",
    "on air",
    "listen live",
    "web radio",
)

GENERIC_ALIAS_HINTS = {
    "radio",
    "listen",
    "podcast",
    "station",
    "music",
    "live",
}

EPISODE_TITLE_PATTERNS = (
    re.compile(r"\bepisode\b", flags=re.I),
    re.compile(r"\bep\.?\s*\d+\b", flags=re.I),
    re.compile(r"\bvol\.?\s*\d+\b", flags=re.I),
    re.compile(r"#\d{2,5}\b"),
    re.compile(r"\bseason\s*\d+\b", flags=re.I),
)

NEWCOMER_REQUIREMENT_HINTS = (
    "new artist",
    "unsigned",
    "emerging",
    "newcomer",
    "demo",
    "submit",
)

NEGATIVE_EMAIL_SUBMISSION_HINTS = (
    "do not send music",
    "don't send music",
    "do not submit music",
    "do not send demos",
    "don't send demos",
    "not for music submissions",
    "not for submissions",
    "no music submissions",
)

DECISION_ROLES = {
    ContactRole.MUSIC_DIRECTOR,
    ContactRole.PROGRAM_DIRECTOR,
    ContactRole.PRODUCER,
}


def _has_newcomer_text(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in NEWCOMER_REQUIREMENT_HINTS)


def _is_blocked_submission_channel(
    *,
    method: SubmissionMethod,
    requirements: str | None,
) -> bool:
    if method != SubmissionMethod.EMAIL:
        return False
    if not requirements:
        return False
    lowered = requirements.lower()
    return any(hint in lowered for hint in NEGATIVE_EMAIL_SUBMISSION_HINTS)


def _latest_profile_signal_flags(session: Session, station_ids: list[int]) -> dict[int, tuple[bool, bool]]:
    if not station_ids:
        return {}
    rows = session.scalars(
        select(StationProfileSnapshot)
        .where(StationProfileSnapshot.station_id.in_(station_ids))
        .order_by(StationProfileSnapshot.station_id.asc(), StationProfileSnapshot.created_at.desc())
    ).all()
    out: dict[int, tuple[bool, bool]] = {}
    for row in rows:
        if row.station_id in out:
            continue
        editorial_blob = (row.editorial_signals_json or "").lower()
        style_blob = (row.style_tags_json or "").lower()
        has_new_music_support = ("new_music_support" in editorial_blob) or ("open_submission" in editorial_blob)
        is_mainstream = "mainstream" in style_blob
        out[row.station_id] = (has_new_music_support, is_mainstream)
    return out


def _build_station_priority_pool(
    session: Session,
    station_limit: int,
    min_station_confidence: float,
) -> tuple[list[Station], dict[int, float], dict[str, int]]:
    pool_size = max(station_limit, station_limit * max(1, settings.station_enrich_pool_multiplier))
    candidates = session.scalars(
        select(Station)
        .where(
            Station.website_url.is_not(None),
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
            Station.confidence_score >= min_station_confidence,
        )
        .order_by(Station.updated_at.desc())
        .limit(pool_size)
    ).all()
    if not candidates:
        return [], {}, {"high": 0, "maintenance": 0, "exploration": 0}

    station_ids = [s.id for s in candidates]
    submissions_rows = session.scalars(
        select(SubmissionChannel).where(SubmissionChannel.station_id.in_(station_ids))
    ).all()
    people_rows = session.scalars(
        select(StationPerson).where(StationPerson.station_id.in_(station_ids))
    ).all()
    genre_counts = {
        sid: cnt
        for sid, cnt in session.execute(
            select(StationGenre.station_id, func.count(StationGenre.id))
            .where(StationGenre.station_id.in_(station_ids))
            .group_by(StationGenre.station_id)
        ).all()
    }
    last_enrich_rows = session.execute(
        select(Evidence.station_id, func.max(Evidence.created_at))
        .where(
            Evidence.station_id.in_(station_ids),
            Evidence.source_id == "station_enrich_search",
        )
        .group_by(Evidence.station_id)
    ).all()
    last_enriched_at = {sid: ts for sid, ts in last_enrich_rows}

    profile_flags = _latest_profile_signal_flags(session=session, station_ids=station_ids)

    submission_count: dict[int, int] = {}
    newcomer_submission: dict[int, bool] = {}
    for row in submissions_rows:
        submission_count[row.station_id] = submission_count.get(row.station_id, 0) + 1
        if row.accepts_newcomers or _has_newcomer_text(row.requirements):
            newcomer_submission[row.station_id] = True

    decision_people_count: dict[int, int] = {}
    for row in people_rows:
        if row.role in DECISION_ROLES:
            decision_people_count[row.station_id] = decision_people_count.get(row.station_id, 0) + 1

    now = datetime.utcnow()
    overdue_cutoff = now - timedelta(days=max(1, settings.station_enrich_maintenance_days_overdue))
    scores: dict[int, float] = {}
    queue_buckets = {"high": [], "maintenance": [], "exploration": []}

    for st in candidates:
        sid = st.id
        has_submission = submission_count.get(sid, 0) > 0
        has_decision_maker = decision_people_count.get(sid, 0) > 0
        has_genre = genre_counts.get(sid, 0) > 0
        profile_new_music, profile_mainstream = profile_flags.get(sid, (False, False))
        newcomer_signal = newcomer_submission.get(sid, False) or profile_new_music
        last_seen = last_enriched_at.get(sid)
        is_overdue = (last_seen is None) or (last_seen <= overdue_cutoff)

        score = 0.0
        if has_submission:
            score += 30
        if newcomer_signal:
            score += 25
        if has_decision_maker:
            score += 20
        if has_genre:
            score += 10
        if st.confidence_score >= 0.8:
            score += 8
        elif st.confidence_score >= 0.65:
            score += 4
        if is_overdue:
            score += 10
        if profile_mainstream and not newcomer_signal:
            score -= 12
        score = max(0.0, min(100.0, score))
        scores[sid] = score

        if score >= settings.station_pitch_ready_min_score:
            queue_buckets["high"].append(st)
        elif is_overdue and score >= 35:
            queue_buckets["maintenance"].append(st)
        else:
            queue_buckets["exploration"].append(st)

    queue_buckets["high"].sort(key=lambda s: scores.get(s.id, 0.0), reverse=True)
    queue_buckets["maintenance"].sort(
        key=lambda s: (
            last_enriched_at.get(s.id) is None,
            last_enriched_at.get(s.id) or datetime.min,
            -scores.get(s.id, 0.0),
        )
    )
    queue_buckets["exploration"].sort(key=lambda s: (s.updated_at or datetime.min, s.id))

    target_high = int(round(station_limit * settings.station_enrich_high_priority_share))
    target_maintenance = int(round(station_limit * settings.station_enrich_maintenance_share))
    target_exploration = station_limit - target_high - target_maintenance
    if target_exploration < 0:
        target_exploration = 0

    selected: list[Station] = []
    selected_ids: set[int] = set()
    queue_counts = {"high": 0, "maintenance": 0, "exploration": 0}

    def _take(bucket_name: str, target: int) -> None:
        if target <= 0:
            return
        for st in queue_buckets[bucket_name]:
            if len(selected) >= station_limit or queue_counts[bucket_name] >= target:
                break
            if st.id in selected_ids:
                continue
            selected.append(st)
            selected_ids.add(st.id)
            queue_counts[bucket_name] += 1

    _take("high", target_high)
    _take("maintenance", target_maintenance)
    _take("exploration", target_exploration)

    if len(selected) < station_limit:
        remainder = sorted(candidates, key=lambda s: scores.get(s.id, 0.0), reverse=True)
        for st in remainder:
            if len(selected) >= station_limit:
                break
            if st.id in selected_ids:
                continue
            selected.append(st)
            selected_ids.add(st.id)
            if st in queue_buckets["high"]:
                queue_counts["high"] += 1
            elif st in queue_buckets["maintenance"]:
                queue_counts["maintenance"] += 1
            else:
                queue_counts["exploration"] += 1

    return selected, scores, queue_counts


def seed_frontier(session: Session) -> int:
    queries = generate_queries()
    inserted = 0

    for item in queries:
        exists = session.scalar(select(CrawlFrontier).where(CrawlFrontier.query == item["query"]))
        if exists:
            continue
        session.add(
            CrawlFrontier(
                query=item["query"],
                locale=item["locale"],
                country=item["country"],
                template_name=item["template_name"],
                next_run_at=datetime.utcnow(),
            )
        )
        inserted += 1
    session.commit()
    refresh_priorities(session)
    return inserted


def _save_evidence(
    session: Session,
    source_type: SourceType,
    source_url: str | None,
    source_id: str | None,
    raw_title: str | None,
    raw_snippet: str | None,
    extracted_payload: dict | None,
    confidence: float,
    station_id: int | None = None,
) -> None:
    dedupe_since = datetime.utcnow() - timedelta(hours=max(1, settings.evidence_dedupe_hours))
    existing = session.scalar(
        select(Evidence.id).where(
            Evidence.station_id == station_id,
            Evidence.source_type == source_type,
            Evidence.source_url == source_url,
            Evidence.created_at >= dedupe_since,
        )
    )
    if existing:
        return
    session.add(
        Evidence(
            station_id=station_id,
            source_type=source_type,
            source_url=source_url,
            source_id=source_id,
            raw_title=raw_title,
            raw_snippet=raw_snippet,
            extracted_payload_json=json.dumps(extracted_payload) if extracted_payload else None,
            confidence=confidence,
        )
    )


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _is_known_non_station_domain(url: str | None) -> bool:
    d = _domain(url)
    if not d:
        return False
    return any(hint in d for hint in NON_STATION_DOMAIN_HINTS)


def _has_radio_signals(name: str, url: str | None, stream_url: str | None, genres: list[str], programs: list[dict], contacts: list[dict]) -> bool:
    hay = f"{name} {url or ''} {' '.join(genres)}".lower()
    if stream_url:
        return True
    if programs or contacts:
        return True
    return any(h in hay for h in RADIO_SIGNAL_HINTS)


def _is_noise_result(title: str, snippet: str, url: str) -> bool:
    hay = f"{title} {snippet} {url}".lower()
    path = (urlparse(url).path or "").lower()
    if any(hint in path for hint in NON_STATION_URL_HINTS) and ("site:" not in hay):
        return True
    if _is_known_non_station_domain(url) and not any(h in hay for h in RADIO_SIGNAL_HINTS):
        return True
    return any(h in hay for h in NON_STATION_TEXT_HINTS) and "radio station" not in hay


def _is_url_like_text(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://") or v.startswith("www.")


def _is_bad_station_identity(name: str | None, website_url: str | None) -> bool:
    lowered_name = (name or "").strip().lower()
    if not lowered_name:
        return True
    if _is_url_like_text(lowered_name):
        return True
    if lowered_name.startswith("listen to ") or lowered_name.startswith("top radio stations"):
        return True
    if _is_known_non_station_domain(website_url):
        return True
    return False


def _should_keep_alias(alias_norm: str) -> bool:
    if not alias_norm:
        return False
    tokens = [t for t in re.split(r"[\s:/|,_-]+", alias_norm.lower()) if t]
    if not tokens:
        return False
    if len(tokens) == 1 and (tokens[0] in GENERIC_ALIAS_HINTS or len(tokens[0]) < 4):
        return False
    if sum(1 for t in tokens if t in GENERIC_ALIAS_HINTS) >= len(tokens):
        return False
    return True


def _is_probable_episode_title(name: str) -> bool:
    if not name:
        return False
    cleaned = name.strip()
    if not cleaned:
        return False
    if any(p.search(cleaned) for p in EPISODE_TITLE_PATTERNS):
        return True
    # Heuristic: many digits usually indicate episode/chapter labels.
    digit_count = sum(1 for ch in cleaned if ch.isdigit())
    return digit_count >= 4 and len(cleaned) < 60


def _domain_matches_station(url: str, station_website_url: str | None) -> bool:
    result_domain = _domain(url)
    station_domain = _domain(station_website_url)
    if not result_domain or not station_domain:
        return False
    return result_domain == station_domain or result_domain.endswith(f".{station_domain}")


def _build_station_domain_index(session: Session) -> dict[str, Station]:
    rows = session.scalars(
        select(Station).where(
            Station.website_url.is_not(None),
            Station.status != StationStatus.REJECTED,
        )
    ).all()
    out: dict[str, Station] = {}
    for row in rows:
        domain = _domain(row.website_url)
        if not domain:
            continue
        existing = out.get(domain)
        if existing is None or row.confidence_score > existing.confidence_score:
            out[domain] = row
    return out


def _lookup_station_for_result(domain_index: dict[str, Station], result_url: str) -> Station | None:
    domain = _domain(result_url)
    while domain:
        hit = domain_index.get(domain)
        if hit is not None:
            return hit
        if "." not in domain:
            break
        domain = domain.split(".", 1)[1]
    return None


def _extract_sitemap_locs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    locs: list[str] = []
    for elem in root.iter():
        tag = elem.tag.lower()
        if tag.endswith("loc") and elem.text:
            locs.append(elem.text.strip())
    return [u for u in locs if u]


def _internal_url_score(url: str) -> int:
    lowered = url.lower()
    score = 0
    if any(k in lowered for k in ("/submit", "music-submission", "submit-music", "/demo")):
        score += 40
    if any(k in lowered for k in ("/contact", "/impress", "/about", "/team", "/staff")):
        score += 30
    if any(k in lowered for k in ("/program", "/schedule", "/show", "/playlist", "/on-air")):
        score += 20
    if any(k in lowered for k in ("/rss", "/feed", "/podcast")):
        score += 15
    return score


def _discover_internal_station_urls(website_url: str, max_urls: int = 8) -> list[str]:
    domain = _domain(website_url)
    if not domain:
        return []
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(url: str | None) -> None:
        if not url:
            return
        url = url.strip()
        if not url:
            return
        if not _domain_matches_station(url=url, station_website_url=website_url):
            return
        if url in seen:
            return
        seen.add(url)
        candidates.append(url)

    _add(website_url)

    homepage_html = ""
    try:
        homepage_html = get_text(website_url)
    except Exception:
        homepage_html = ""

    if settings.station_enrich_use_rss:
        for path in ("/feed", "/rss", "/rss.xml", "/feed.xml", "/podcast/feed"):
            _add(urljoin(website_url, path))
        if homepage_html:
            for href in re.findall(
                r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
                homepage_html,
                flags=re.I,
            ):
                _add(urljoin(website_url, href))

    if settings.station_enrich_use_sitemap:
        sitemap_roots = [urljoin(website_url, "/sitemap.xml")]
        for sitemap_url in sitemap_roots:
            try:
                xml_text = get_text(sitemap_url)
            except Exception:
                continue
            locs = _extract_sitemap_locs(xml_text)
            nested = [u for u in locs if "sitemap" in u.lower()][:3]
            urls = [u for u in locs if "sitemap" not in u.lower()]
            for nested_url in nested:
                try:
                    nested_xml = get_text(nested_url)
                    urls.extend(_extract_sitemap_locs(nested_xml))
                except Exception:
                    continue
            for u in sorted(urls, key=_internal_url_score, reverse=True)[: max_urls * 3]:
                if _internal_url_score(u) <= 0:
                    continue
                _add(u)

    ranked = sorted(candidates, key=_internal_url_score, reverse=True)
    return ranked[: max(1, max_urls)]


def _upsert_station(session: Session, payload: dict, source_type: SourceType, source_url: str | None) -> tuple[Station, bool]:
    name = payload.get("canonical_name") or "Unknown Station"
    country_code = normalize_country(payload.get("country_code"))
    website_url = payload.get("website_url")
    bad_identity = _is_bad_station_identity(name, website_url)
    is_probable_radio_station = _has_radio_signals(
        name=name,
        url=website_url,
        stream_url=payload.get("stream_url"),
        genres=payload.get("genres", []),
        programs=payload.get("programs", []),
        contacts=payload.get("contacts", []),
    ) and not _is_known_non_station_domain(website_url)

    duplicate = find_duplicate_station(
        session=session,
        canonical_name=name,
        website_url=website_url,
        country_code=country_code,
    )
    if not duplicate:
        duplicate = session.scalar(
            select(Station).where(
                Station.canonical_name == name,
                Station.country_code == country_code,
            )
        )
    created = False
    if duplicate:
        station = duplicate
    else:
        station = Station(
            canonical_name=name,
            normalized_name=normalize_text(name),
            country_code=country_code,
            language=payload.get("language") or "",
            city=payload.get("city"),
            website_url=website_url,
            stream_url=payload.get("stream_url"),
            # Discovery/enrichment is not a promotion gate. New rows stay staged until
            # candidate_rescan runs the Playwright + LLM quality flow and applies status.
            status=StationStatus.ARCHIVED,
            confidence_score=float(payload.get("confidence", 0.4)),
            fingerprint=station_fingerprint(name, website_url, country_code),
        )
        session.add(station)
        try:
            session.flush()
            created = True
        except IntegrityError:
            session.rollback()
            station = session.scalar(
                select(Station).where(
                    Station.canonical_name == name,
                    Station.country_code == country_code,
                )
            )
            if station is None:
                raise

    seen_aliases: set[str] = set()
    for alias in payload.get("aliases", []):
        alias_norm = normalize_text(alias)
        if not alias_norm or alias_norm in seen_aliases or not _should_keep_alias(alias_norm):
            continue
        seen_aliases.add(alias_norm)
        exists = session.scalar(
            select(StationAlias).where(StationAlias.station_id == station.id, StationAlias.alias == alias_norm)
        )
        if not exists:
            session.add(StationAlias(station_id=station.id, alias=alias_norm))

    seen_genres: set[str] = set()
    for genre in payload.get("genres", []):
        genre_norm = normalize_text(genre)
        if not genre_norm or genre_norm in seen_genres:
            continue
        seen_genres.add(genre_norm)
        exists = session.scalar(
            select(StationGenre).where(StationGenre.station_id == station.id, StationGenre.genre == genre_norm)
        )
        if not exists:
            session.add(StationGenre(station_id=station.id, genre=genre_norm))

    for program in payload.get("programs", []):
        pname = (program.get("name") or "").strip()
        if not pname or _is_probable_episode_title(pname):
            continue
        exists = session.scalar(
            select(StationProgram).where(StationProgram.station_id == station.id, StationProgram.name == pname)
        )
        if not exists:
            session.add(
                StationProgram(
                    station_id=station.id,
                    name=pname,
                    description=program.get("description"),
                    schedule=program.get("schedule"),
                )
            )

    for submission in payload.get("submissions", []):
        method_str = (submission.get("method") or "unknown").lower()
        method = SubmissionMethod(method_str) if method_str in {m.value for m in SubmissionMethod} else SubmissionMethod.UNKNOWN
        url = (submission.get("url") or "").strip() or None
        email = (submission.get("email") or "").strip() or None
        requirements = (submission.get("requirements") or "").strip() or None
        if not url and not email:
            continue
        if _is_blocked_submission_channel(method=method, requirements=requirements):
            continue
        exists = session.scalar(
            select(SubmissionChannel).where(
                SubmissionChannel.station_id == station.id,
                SubmissionChannel.method == method,
                SubmissionChannel.url == url,
                SubmissionChannel.email == email,
            )
        )
        if not exists:
            session.add(
                SubmissionChannel(
                    station_id=station.id,
                    method=method,
                    url=url,
                    email=email,
                    requirements=requirements,
                    accepts_newcomers=bool(submission.get("accepts_newcomers", False)),
                )
            )

    for contact in payload.get("contacts", []):
        role_str = (contact.get("role") or "unknown").lower()
        role = ContactRole(role_str) if role_str in {r.value for r in ContactRole} else ContactRole.UNKNOWN
        name = (contact.get("name") or "").strip() or None
        show_name = (contact.get("show_name") or "").strip() or None
        email = (contact.get("email") or "").strip() or None
        contact_url = contact.get("contact_url")

        exists = session.scalar(
            select(StationContact).where(
                StationContact.station_id == station.id,
                StationContact.name == name,
                StationContact.role == role,
                StationContact.show_name == show_name,
                StationContact.email == email,
            )
        )
        if not exists:
            session.add(
                StationContact(
                    station_id=station.id,
                    name=name,
                    role=role,
                    show_name=show_name,
                    email=email,
                    contact_url=contact_url,
                    notes=contact.get("notes"),
                    confidence=float(contact.get("confidence", 0.5)),
                )
            )
        upsert_station_person(
            session=session,
            station_id=station.id,
            contact=contact,
            station_genres=[normalize_text(g) for g in payload.get("genres", []) if normalize_text(g)],
        )

    if (not is_probable_radio_station or bad_identity) and station.status != StationStatus.REJECTED:
        station.status = StationStatus.REJECTED
        station.confidence_score = min(station.confidence_score, 0.45)

    _save_evidence(
        session=session,
        source_type=source_type,
        source_url=source_url,
        source_id=None,
        raw_title=name,
        raw_snippet=None,
        extracted_payload=payload,
        confidence=float(payload.get("confidence", 0.5)),
        station_id=station.id,
    )
    session.commit()
    return station, created


def ingest_seed_sources(session: Session, limit: int = 300) -> dict:
    created = 0
    updated = 0

    rb_items = fetch_station_seeds(limit=limit)
    for row in rb_items:
        payload = {
            "canonical_name": row.get("name") or "Unknown Station",
            "aliases": [],
            "country_code": (row.get("countrycode") or "").upper(),
            "language": row.get("language") or "",
            "city": row.get("state") or None,
            "website_url": row.get("homepage") or None,
            "stream_url": row.get("url_resolved") or row.get("url"),
            "genres": [g.strip() for g in (row.get("tags") or "").split(",") if g.strip()],
            "programs": [],
            "submissions": [],
            "confidence": 0.65,
        }
        _, was_created = _upsert_station(session, payload, SourceType.RADIO_BROWSER, payload.get("website_url"))
        created += int(was_created)
        updated += int(not was_created)

    wikidata_rows = fetch_radio_stations(limit=min(limit, 200))
    for row in wikidata_rows:
        label = row.get("stationLabel", {}).get("value", "Unknown Station")
        website = row.get("officialWebsite", {}).get("value")
        payload = {
            "canonical_name": label,
            "aliases": [],
            "country_code": "",
            "language": "",
            "city": None,
            "website_url": website,
            "stream_url": None,
            "genres": [],
            "programs": [],
            "submissions": [],
            "confidence": 0.7,
        }
        _, was_created = _upsert_station(session, payload, SourceType.WIKIDATA, website)
        created += int(was_created)
        updated += int(not was_created)

    return {"created": created, "updated": updated, "seed_rows": len(rb_items) + len(wikidata_rows)}


def _rb_row_to_payload(row: dict) -> dict:
    return {
        "canonical_name": row.get("name") or "Unknown Station",
        "aliases": [],
        "country_code": (row.get("countrycode") or "").upper(),
        "language": row.get("language") or "",
        "city": row.get("state") or None,
        "website_url": row.get("homepage") or None,
        "stream_url": row.get("url_resolved") or row.get("url"),
        "genres": [g.strip() for g in (row.get("tags") or "").split(",") if g.strip()],
        "programs": [],
        "submissions": [],
        "confidence": 0.65,
    }


def _wikidata_row_to_payload(row: dict) -> dict:
    label = row.get("stationLabel", {}).get("value", "Unknown Station")
    website = row.get("officialWebsite", {}).get("value")
    return {
        "canonical_name": label,
        "aliases": [],
        "country_code": "",
        "language": "",
        "city": None,
        "website_url": website,
        "stream_url": None,
        "genres": [],
        "programs": [],
        "submissions": [],
        "confidence": 0.7,
    }


def _ingest_rb_items(session: Session, rows: list[dict]) -> tuple[int, int]:
    created = 0
    updated = 0
    for row in rows:
        payload = _rb_row_to_payload(row)
        _, was_created = _upsert_station(session, payload, SourceType.RADIO_BROWSER, payload.get("website_url"))
        created += int(was_created)
        updated += int(not was_created)
    return created, updated


def _ingest_wikidata_items(session: Session, rows: list[dict]) -> tuple[int, int]:
    created = 0
    updated = 0
    for row in rows:
        payload = _wikidata_row_to_payload(row)
        _, was_created = _upsert_station(session, payload, SourceType.WIKIDATA, payload.get("website_url"))
        created += int(was_created)
        updated += int(not was_created)
    return created, updated


def _iso_utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _load_harvest_checkpoint(checkpoint_path: Path) -> dict:
    if checkpoint_path.exists():
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            return {
                "radio_browser_offset": int(payload.get("radio_browser_offset", 0)),
                "wikidata_offset": int(payload.get("wikidata_offset", 0)),
                "radio_browser_done": bool(payload.get("radio_browser_done", False)),
                "wikidata_done": bool(payload.get("wikidata_done", False)),
                "updated_at": payload.get("updated_at") or _iso_utc_now(),
            }
        except Exception:
            pass
    return {
        "radio_browser_offset": 0,
        "wikidata_offset": 0,
        "radio_browser_done": False,
        "wikidata_done": False,
        "updated_at": _iso_utc_now(),
    }


def _save_harvest_checkpoint(checkpoint_path: Path, state: dict) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _priority_countries() -> list[str]:
    raw = settings.priority_countries or ""
    parsed = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return parsed or ["US", "GB", "DE", "FR"]


def _country_discovery_state_path() -> Path:
    return Path(".radio_db_state") / "country_discovery_state.json"


def _load_country_discovery_state(path: Path | None = None) -> dict:
    state_path = path or _country_discovery_state_path()
    if not state_path.exists():
        return {
            "index": 0,
            "countries": _priority_countries(),
            "query_cursor": {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        countries = [str(c).upper() for c in payload.get("countries", []) if str(c).strip()]
        if not countries:
            countries = _priority_countries()
        return {
            "index": int(payload.get("index", 0)) % max(1, len(countries)),
            "countries": countries,
            "query_cursor": dict(payload.get("query_cursor", {})),
            "updated_at": str(payload.get("updated_at", datetime.now(timezone.utc).isoformat())),
        }
    except Exception:
        return {
            "index": 0,
            "countries": _priority_countries(),
            "query_cursor": {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _save_country_discovery_state(state: dict, path: Path | None = None) -> None:
    state_path = path or _country_discovery_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_country_discovery_intelligence(limit: int = 25) -> dict:
    state = _load_country_discovery_state()
    recent_runs = state.get("recent_runs", [])
    if not isinstance(recent_runs, list):
        recent_runs = []
    memory = state.get("memory", {})
    if not isinstance(memory, dict):
        memory = {}
    return {
        "state": state,
        "memory": memory,
        "recent_runs": recent_runs[-max(1, limit):],
    }


def ingest_free_sources_full(
    session: Session,
    rb_batch_size: int = 1500,
    wikidata_batch_size: int = 500,
    max_rb_pages: int | None = None,
    max_wikidata_pages: int | None = None,
    reset_checkpoint: bool = False,
    checkpoint_path: Path | None = None,
) -> dict:
    inserted_frontier = seed_frontier(session)
    checkpoint = checkpoint_path or (Path(".radio_db_state") / "free_harvest_checkpoint.json")
    state = _load_harvest_checkpoint(checkpoint)
    if reset_checkpoint:
        state = {
            "radio_browser_offset": 0,
            "wikidata_offset": 0,
            "radio_browser_done": False,
            "wikidata_done": False,
            "updated_at": _iso_utc_now(),
        }

    total_created = 0
    total_updated = 0
    total_seed_rows = 0
    rb_pages = 0
    wikidata_pages = 0

    while not state["radio_browser_done"]:
        if max_rb_pages is not None and rb_pages >= max_rb_pages:
            break
        rows = fetch_station_seeds_page(limit=rb_batch_size, offset=state["radio_browser_offset"])
        fetched = len(rows)
        if fetched == 0:
            state["radio_browser_done"] = True
            break

        created, updated = _ingest_rb_items(session, rows)
        total_created += created
        total_updated += updated
        total_seed_rows += fetched
        rb_pages += 1
        state["radio_browser_offset"] += fetched
        if fetched < rb_batch_size:
            state["radio_browser_done"] = True
        state["updated_at"] = _iso_utc_now()
        _save_harvest_checkpoint(checkpoint, state)

    while not state["wikidata_done"]:
        if max_wikidata_pages is not None and wikidata_pages >= max_wikidata_pages:
            break
        rows = fetch_radio_stations_page(limit=wikidata_batch_size, offset=state["wikidata_offset"])
        fetched = len(rows)
        if fetched == 0:
            state["wikidata_done"] = True
            break

        created, updated = _ingest_wikidata_items(session, rows)
        total_created += created
        total_updated += updated
        total_seed_rows += fetched
        wikidata_pages += 1
        state["wikidata_offset"] += fetched
        if fetched < wikidata_batch_size:
            state["wikidata_done"] = True
        state["updated_at"] = _iso_utc_now()
        _save_harvest_checkpoint(checkpoint, state)

    state["updated_at"] = _iso_utc_now()
    _save_harvest_checkpoint(checkpoint, state)
    return {
        "frontier_inserted": inserted_frontier,
        "created": total_created,
        "updated": total_updated,
        "seed_rows": total_seed_rows,
        "rb_pages": rb_pages,
        "wikidata_pages": wikidata_pages,
        "radio_browser_offset": state["radio_browser_offset"],
        "wikidata_offset": state["wikidata_offset"],
        "radio_browser_done": state["radio_browser_done"],
        "wikidata_done": state["wikidata_done"],
        "checkpoint_path": str(checkpoint),
    }


def ingest_free_sources(session: Session, limit: int = 1000) -> dict:
    inserted_frontier = seed_frontier(session)
    ingested = ingest_seed_sources(session, limit=limit)
    return {
        "frontier_inserted": inserted_frontier,
        "created": ingested["created"],
        "updated": ingested["updated"],
        "seed_rows": ingested["seed_rows"],
    }


def _is_llm_candidate(title: str, snippet: str, url: str) -> bool:
    haystack = f"{title} {snippet} {url}".lower()
    markers = [
        "submit",
        "submission",
        "send us your music",
        "new artist",
        "airplay",
        "playlist",
        "host",
        "dj",
        "presenter",
        "producer",
        "music director",
        "program director",
        "radio station",
    ]
    return any(marker in haystack for marker in markers)


def _estimate_llm_call_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    if provider == "xai":
        return (input_tokens / 1_000_000) * settings.xai_input_price_per_1m + (
            output_tokens / 1_000_000
        ) * settings.xai_output_price_per_1m
    if provider == "openai":
        return (input_tokens / 1_000_000) * settings.openai_input_price_per_1m + (
            output_tokens / 1_000_000
        ) * settings.openai_output_price_per_1m
    return (input_tokens / 1_000_000) * settings.llm_input_price_per_1m + (
        output_tokens / 1_000_000
    ) * settings.llm_output_price_per_1m


def run_discovery(session: Session, max_queries: int | None = None) -> dict:
    budget = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )
    refresh_priorities(session)
    queries = claim_queries(session, batch_size=max_queries or settings.batch_size)
    if not queries:
        return {"queries": 0, "new_stations": 0, "duplicates": 0, "results": 0}

    total_results = 0
    total_new = 0
    total_duplicates = 0
    llm_calls_run = 0
    llm_skipped_budget = 0
    llm_skipped_relevance = 0
    page_fetches = 0
    domain_llm_counts: dict[str, int] = {}
    create_new_stations = bool(settings.discovery_create_new_stations)
    domain_index = _build_station_domain_index(session) if not create_new_stations else {}
    skipped_noise = 0
    skipped_new_station = 0
    source_calls = {"brave": 0, "brave_answer": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0}
    source_type_map = {
        "brave": SourceType.BRAVE_SEARCH,
        "brave_answer": SourceType.BRAVE_SEARCH,
        "google_cse": SourceType.GOOGLE_CSE,
        "tavily": SourceType.TAVILY,
        "grok": SourceType.GROK_SEARCH,
        "linkup": SourceType.LINKUP,
        "duckduckgo": SourceType.DUCKDUCKGO,
    }

    for q in queries:
        results, calls = collect_search_results(query=q.query, country=q.country, include_linkup=False)
        for k, v in calls.items():
            source_calls[k] += v

        q.run_count += 1
        total_results += len(results)

        new_hits = 0
        duplicates = 0
        for item in results:
            title = item.get("title", "")
            snippet = item.get("description", "")
            url = item.get("url")
            if not url:
                continue
            if _is_noise_result(title=title, snippet=snippet, url=url):
                skipped_noise += 1
                continue
            matched_station = _lookup_station_for_result(domain_index, url) if not create_new_stations else None
            if not create_new_stations and matched_station is None:
                skipped_new_station += 1
                continue

            _save_evidence(
                session=session,
                source_type=source_type_map.get(str(item.get("source")), SourceType.BRAVE_SEARCH),
                source_url=url,
                source_id=str(item.get("source") or item.get("profile", {}).get("name") or "search"),
                raw_title=title,
                raw_snippet=snippet,
                extracted_payload=None,
                confidence=0.25,
            )

            page_text = None
            if page_fetches < settings.max_page_fetches_per_run:
                try:
                    html = get_text(url)
                    page_text = extract_page_text_for_prompt(html)
                    page_fetches += 1
                except Exception:
                    page_text = None

            domain = (urlparse(url).netloc or "").lower().replace("www.", "")
            llm_candidate = _is_llm_candidate(title, snippet, url)
            domain_calls = domain_llm_counts.get(domain, 0)

            estimated_input_tokens = CostGuard.estimate_input_tokens(
                f"{title}\n{snippet}\n{url}\n{(page_text or '')[:settings.llm_max_prompt_chars]}"
            )
            provider = choose_llm_provider(routing_key=url or title)
            estimated_cost = _estimate_llm_call_cost(
                provider=provider,
                input_tokens=estimated_input_tokens,
                output_tokens=settings.llm_estimated_output_tokens,
            )

            use_llm = True
            if provider == "none":
                use_llm = False
                llm_skipped_budget += 1
            if not llm_candidate:
                use_llm = False
                llm_skipped_relevance += 1
            if llm_calls_run >= settings.max_llm_calls_per_run:
                use_llm = False
                llm_skipped_budget += 1
            if domain_calls >= settings.max_llm_calls_per_domain_per_run:
                use_llm = False
                llm_skipped_budget += 1
            if not budget.can_call_llm_today() or not budget.can_spend(estimated_cost):
                use_llm = False
                llm_skipped_budget += 1

            extract = extract_station_from_text(
                title=title,
                snippet=snippet,
                url=url,
                page_text=page_text,
                use_llm=use_llm,
                provider_hint=provider,
            )
            payload = extract.model_dump(mode="json")
            if matched_station is not None:
                payload["canonical_name"] = matched_station.canonical_name
                payload["country_code"] = matched_station.country_code or payload.get("country_code") or ""
                payload["website_url"] = matched_station.website_url or payload.get("website_url") or url
                payload["stream_url"] = matched_station.stream_url or payload.get("stream_url")
                payload["language"] = payload.get("language") or matched_station.language or ""
                payload["city"] = payload.get("city") or matched_station.city
                payload["confidence"] = max(
                    float(payload.get("confidence", 0.35)),
                    float(matched_station.confidence_score or 0.35),
                )
            elif _is_bad_station_identity(payload.get("canonical_name"), payload.get("website_url") or url):
                skipped_noise += 1
                continue
            if use_llm:
                llm_calls_run += 1
                domain_llm_counts[domain] = domain_calls + 1
                budget.register_call(estimated_cost)
            station, created = _upsert_station(
                session=session,
                payload=payload,
                source_type=SourceType.WEBSITE,
                source_url=url,
            )
            new_hits += int(created)
            duplicates += int(not created)

            _save_evidence(
                session=session,
                source_type=SourceType.WEBSITE,
                source_url=url,
                source_id=str(station.id),
                raw_title=title,
                raw_snippet=snippet,
                extracted_payload=payload,
                confidence=float(payload.get("confidence", 0.35)),
                station_id=station.id,
            )
            session.commit()

        q.yield_new += new_hits
        q.yield_duplicate += duplicates
        # If query keeps producing novelty, rerun sooner.
        q.next_run_at = datetime.utcnow() + (timedelta(days=2) if new_hits > 0 else timedelta(days=10))
        total_new += new_hits
        total_duplicates += duplicates
        session.commit()

    refresh_priorities(session)
    return {
        "queries": len(queries),
        "results": total_results,
        "new_stations": total_new,
        "duplicates": total_duplicates,
        "page_fetches": page_fetches,
        "llm_calls_run": llm_calls_run,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_skipped_relevance": llm_skipped_relevance,
        "llm_calls_today": budget.state.llm_calls,
        "usd_spent_today_estimate": round(budget.state.usd_spent_estimate, 6),
        "discovery_create_new_stations": create_new_stations,
        "skipped_noise": skipped_noise,
        "skipped_new_station": skipped_new_station,
        "source_calls": source_calls,
    }


def run_country_discovery_cycle(
    session: Session,
    max_queries: int | None = None,
    country: str | None = None,
    countries: list[str] | None = None,
    include_linkup: bool | None = None,
    min_confidence: float | None = None,
) -> dict:
    budget = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )

    state = _load_country_discovery_state()
    if countries:
        cleaned = [c.strip().upper() for c in countries if c.strip()]
        if cleaned:
            state["countries"] = cleaned
            state["index"] = int(state.get("index", 0)) % len(cleaned)
    sequence = state.get("countries") or _priority_countries()
    if not sequence:
        sequence = _priority_countries()
    state["countries"] = sequence

    selected_country = (country or "").strip().upper()
    if not selected_country:
        idx = int(state.get("index", 0)) % len(sequence)
        selected_country = sequence[idx]
        state["index"] = (idx + 1) % len(sequence)
    else:
        selected_country = selected_country.upper()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    queries = generate_queries_for_country(selected_country)
    if not queries:
        _save_country_discovery_state(state)
        return {"country_processed": selected_country, "queries": 0, "results": 0, "new_stations": 0, "duplicates": 0}

    query_limit = max_queries or settings.country_discovery_max_queries_per_run
    cursor_map = state.get("query_cursor", {})
    country_cursor = int(cursor_map.get(selected_country, 0))
    ordered_queries = queries[country_cursor:] + queries[:country_cursor]
    selected_queries = ordered_queries[: max(1, query_limit)]
    cursor_map[selected_country] = (country_cursor + len(selected_queries)) % len(queries)
    state["query_cursor"] = cursor_map
    _save_country_discovery_state(state)

    total_results = 0
    total_new = 0
    total_duplicates = 0
    skipped_noise = 0
    skipped_existing = 0
    page_fetches = 0
    llm_calls_run = 0
    llm_skipped_budget = 0
    llm_skipped_relevance = 0
    source_calls = {"brave": 0, "brave_answer": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0}
    include_linkup_effective = settings.country_discovery_include_linkup if include_linkup is None else include_linkup
    confidence_floor = settings.country_discovery_min_confidence if min_confidence is None else min_confidence
    max_results_per_query = max(1, settings.country_discovery_max_results_per_query)

    source_type_map = {
        "brave": SourceType.BRAVE_SEARCH,
        "brave_answer": SourceType.BRAVE_SEARCH,
        "google_cse": SourceType.GOOGLE_CSE,
        "tavily": SourceType.TAVILY,
        "grok": SourceType.GROK_SEARCH,
        "linkup": SourceType.LINKUP,
        "duckduckgo": SourceType.DUCKDUCKGO,
    }
    known_domains = set(_build_station_domain_index(session).keys())

    for q in selected_queries:
        query_string = q["query"]
        results, calls = collect_search_results(
            query=query_string,
            country=selected_country,
            include_linkup=include_linkup_effective,
        )
        for k, v in calls.items():
            source_calls[k] += v

        results = results[:max_results_per_query]
        total_results += len(results)
        for item in results:
            title = item.get("title", "")
            snippet = item.get("description", "")
            url = (item.get("url") or "").strip()
            if not url:
                continue
            if _is_noise_result(title=title, snippet=snippet, url=url):
                skipped_noise += 1
                continue

            url_domain = _domain(url)
            if url_domain and url_domain in known_domains:
                skipped_existing += 1
                continue

            page_text = None
            if page_fetches < settings.max_page_fetches_per_run:
                try:
                    html = get_text(url)
                    page_text = extract_page_text_for_prompt(html)
                    page_fetches += 1
                except Exception:
                    page_text = None

            domain = url_domain
            llm_candidate = _is_llm_candidate(title, snippet, url)
            estimated_input_tokens = CostGuard.estimate_input_tokens(
                f"{title}\n{snippet}\n{url}\n{(page_text or '')[:settings.llm_max_prompt_chars]}"
            )
            provider = choose_llm_provider(routing_key=url or title)
            estimated_cost = _estimate_llm_call_cost(
                provider=provider,
                input_tokens=estimated_input_tokens,
                output_tokens=settings.llm_estimated_output_tokens,
            )

            use_llm = True
            if provider == "none":
                use_llm = False
                llm_skipped_budget += 1
            if not llm_candidate:
                use_llm = False
                llm_skipped_relevance += 1
            if llm_calls_run >= settings.max_llm_calls_per_run:
                use_llm = False
                llm_skipped_budget += 1
            if not budget.can_call_llm_today() or not budget.can_spend(estimated_cost):
                use_llm = False
                llm_skipped_budget += 1

            extract = extract_station_from_text(
                title=title,
                snippet=snippet,
                url=url,
                page_text=page_text,
                use_llm=use_llm,
                provider_hint=provider,
            )
            payload = extract.model_dump(mode="json")
            payload["country_code"] = normalize_country(payload.get("country_code")) or selected_country
            payload["confidence"] = max(float(payload.get("confidence", 0.35)), float(confidence_floor))
            if _is_bad_station_identity(payload.get("canonical_name"), payload.get("website_url") or url):
                skipped_noise += 1
                continue

            if use_llm:
                llm_calls_run += 1
                budget.register_call(estimated_cost)

            station, created = _upsert_station(
                session=session,
                payload=payload,
                source_type=SourceType.WEBSITE,
                source_url=url,
            )
            total_new += int(created)
            total_duplicates += int(not created)
            if station.website_url:
                known_domains.add(_domain(station.website_url))

            _save_evidence(
                session=session,
                source_type=source_type_map.get(str(item.get("source")), SourceType.WEBSITE),
                source_url=url,
                source_id="country_discovery_cycle",
                raw_title=title,
                raw_snippet=snippet,
                extracted_payload=payload,
                confidence=float(payload.get("confidence", 0.35)),
                station_id=station.id,
            )
            session.commit()

    return {
        "country_processed": selected_country,
        "next_country": state["countries"][state["index"]] if state.get("countries") else selected_country,
        "queries": len(selected_queries),
        "results": total_results,
        "new_stations": total_new,
        "duplicates": total_duplicates,
        "skipped_noise": skipped_noise,
        "skipped_existing": skipped_existing,
        "page_fetches": page_fetches,
        "llm_calls_run": llm_calls_run,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_skipped_relevance": llm_skipped_relevance,
        "llm_calls_today": budget.state.llm_calls,
        "usd_spent_today_estimate": round(budget.state.usd_spent_estimate, 6),
        "source_calls": source_calls,
    }


def enrich_priority_sources(session: Session, max_queries: int = 25) -> dict:
    return run_discovery(session=session, max_queries=max_queries)


def station_enrich_search(
    session: Session,
    station_limit: int = 200,
    queries_per_station: int = 2,
    min_confidence: float = 0.35,
    station_id: int | None = None,
) -> dict:
    budget = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )

    priority_scores: dict[int, float] = {}
    queue_mix = {"high": 0, "maintenance": 0, "exploration": 0}
    if station_id is not None:
        stations = session.scalars(select(Station).where(Station.id == station_id).limit(1)).all()
        if stations:
            priority_scores[stations[0].id] = 100.0
            queue_mix["high"] = 1
    else:
        stations, priority_scores, queue_mix = _build_station_priority_pool(
            session=session,
            station_limit=station_limit,
            min_station_confidence=settings.station_enrich_min_station_confidence,
        )
    if not stations:
        return {"stations_processed": 0, "queries_executed": 0, "results": 0}

    source_type_map = {
        "brave": SourceType.BRAVE_SEARCH,
        "google_cse": SourceType.GOOGLE_CSE,
        "tavily": SourceType.TAVILY,
        "grok": SourceType.GROK_SEARCH,
        "linkup": SourceType.LINKUP,
        "duckduckgo": SourceType.DUCKDUCKGO,
        "internal": SourceType.WEBSITE,
    }

    source_calls = {"brave": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0, "internal": 0}
    llm_calls_run = 0
    llm_skipped_budget = 0
    llm_skipped_relevance = 0
    page_fetches = 0
    queries_executed = 0
    results_kept = 0
    processed_urls: set[tuple[int, str]] = set()
    score_sum = 0.0

    def _process_result_item(st: Station, item: dict, query_label: str) -> None:
        nonlocal llm_calls_run, llm_skipped_budget, llm_skipped_relevance, page_fetches, results_kept
        title = item.get("title", "") or ""
        snippet = item.get("description", "") or ""
        url = (item.get("url") or "").strip()
        if not url:
            return
        key = (st.id, url)
        if key in processed_urls:
            return
        processed_urls.add(key)

        if not _domain_matches_station(url=url, station_website_url=st.website_url):
            return
        if _is_noise_result(title=title, snippet=snippet, url=url):
            return

        source_name = str(item.get("source") or "internal")
        _save_evidence(
            session=session,
            source_type=source_type_map.get(source_name, SourceType.WEBSITE),
            source_url=url,
            source_id="station_enrich_search",
            raw_title=title,
            raw_snippet=snippet,
            extracted_payload={"station_id": st.id, "query": query_label},
            confidence=0.35,
            station_id=st.id,
        )

        page_text = None
        if page_fetches < settings.station_enrich_max_page_fetches:
            try:
                html = get_text(url)
                page_text = extract_page_text_for_prompt(html)
                page_fetches += 1
            except Exception:
                page_text = None

        llm_candidate = _is_llm_candidate(title, snippet, url)
        estimated_input_tokens = CostGuard.estimate_input_tokens(
            f"{title}\n{snippet}\n{url}\n{(page_text or '')[:settings.llm_max_prompt_chars]}"
        )
        provider = choose_llm_provider(routing_key=url or title)
        estimated_cost = _estimate_llm_call_cost(
            provider=provider,
            input_tokens=estimated_input_tokens,
            output_tokens=settings.llm_estimated_output_tokens,
        )
        use_llm = True
        if provider == "none":
            use_llm = False
            llm_skipped_budget += 1
        if not llm_candidate:
            use_llm = False
            llm_skipped_relevance += 1
        if llm_calls_run >= settings.max_llm_calls_per_run:
            use_llm = False
            llm_skipped_budget += 1
        if not budget.can_call_llm_today() or not budget.can_spend(estimated_cost):
            use_llm = False
            llm_skipped_budget += 1

        extract = extract_station_from_text(
            title=title,
            snippet=snippet,
            url=url,
            page_text=page_text,
            use_llm=use_llm,
            provider_hint=provider,
        )
        payload = extract.model_dump(mode="json")
        payload["canonical_name"] = st.canonical_name
        payload["country_code"] = st.country_code or payload.get("country_code") or ""
        payload["website_url"] = st.website_url or payload.get("website_url")
        payload["stream_url"] = st.stream_url or payload.get("stream_url")
        payload["language"] = payload.get("language") or st.language or ""
        payload["city"] = payload.get("city") or st.city
        payload["confidence"] = max(float(payload.get("confidence", 0.5)), min_confidence)

        if use_llm:
            llm_calls_run += 1
            budget.register_call(estimated_cost)

        station, _created = _upsert_station(
            session=session,
            payload=payload,
            source_type=SourceType.WEBSITE,
            source_url=url,
        )
        _save_evidence(
            session=session,
            source_type=SourceType.WEBSITE,
            source_url=url,
            source_id="station_enrich_search",
            raw_title=title,
            raw_snippet=snippet,
            extracted_payload=payload,
            confidence=float(payload.get("confidence", 0.5)),
            station_id=station.id,
        )
        session.commit()
        results_kept += 1

    for st in stations:
        score_sum += priority_scores.get(st.id, 0.0)
        domain = _domain(st.website_url)
        if not domain:
            continue

        internal_urls = _discover_internal_station_urls(
            website_url=st.website_url or "",
            max_urls=settings.station_enrich_max_internal_urls_per_station,
        )
        source_calls["internal"] += len(internal_urls)
        for url in internal_urls:
            _process_result_item(
                st=st,
                item={"title": "Internal station source", "description": "", "url": url, "source": "internal"},
                query_label="internal_sources",
            )

        query_templates = [
            f'site:{domain} ("submit music" OR "music submission" OR "demo" OR "playlist submission" OR "contact")',
            f'site:{domain} ("program" OR "schedule" OR "shows" OR "host" OR "dj" OR "music director")',
            f'site:{domain} ("about" OR "team" OR "imprint" OR "staff" OR "contact")',
        ]
        plan = query_templates[: max(1, queries_per_station)]

        for q in plan:
            queries_executed += 1
            results, calls = collect_station_enrichment_results(
                query=q,
                country=st.country_code or None,
                max_results=settings.station_enrich_max_results_per_query,
                include_google=settings.station_enrich_use_google,
                include_tavily=settings.station_enrich_use_tavily,
                include_grok=settings.station_enrich_use_grok,
                include_linkup=settings.station_enrich_use_linkup,
                include_duckduckgo=settings.station_enrich_use_duckduckgo,
            )
            for k, v in calls.items():
                source_calls[k] += v

            for item in results:
                _process_result_item(st=st, item=item, query_label=q)

    return {
        "stations_processed": len(stations),
        "avg_priority_score": round(score_sum / max(1, len(stations)), 2),
        "queue_mix": queue_mix,
        "queries_executed": queries_executed,
        "results": results_kept,
        "page_fetches": page_fetches,
        "llm_calls_run": llm_calls_run,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_skipped_relevance": llm_skipped_relevance,
        "llm_calls_today": budget.state.llm_calls,
        "usd_spent_today_estimate": round(budget.state.usd_spent_estimate, 6),
        "source_calls": source_calls,
    }


def cleanup_non_station_records(session: Session, limit: int = 50000, dry_run: bool = True) -> dict:
    stations = session.scalars(select(Station).order_by(Station.updated_at.desc()).limit(limit)).all()
    checked = len(stations)
    rejected_now = 0
    already_rejected = 0

    for st in stations:
        if st.status == StationStatus.REJECTED:
            already_rejected += 1
            continue

        genres = session.scalars(select(StationGenre.genre).where(StationGenre.station_id == st.id)).all()
        programs = session.scalars(select(StationProgram.id).where(StationProgram.station_id == st.id).limit(1)).all()
        contacts = session.scalars(select(StationContact.id).where(StationContact.station_id == st.id).limit(1)).all()
        is_station = _has_radio_signals(
            name=st.canonical_name or "",
            url=st.website_url,
            stream_url=st.stream_url,
            genres=genres,
            programs=[{"id": x} for x in programs],
            contacts=[{"id": x} for x in contacts],
        ) and not _is_known_non_station_domain(st.website_url)
        if (not is_station) or _is_bad_station_identity(st.canonical_name, st.website_url):
            rejected_now += 1
            if not dry_run:
                st.status = StationStatus.REJECTED
                st.confidence_score = min(st.confidence_score, 0.45)

    if not dry_run:
        session.commit()
    return {
        "checked": checked,
        "rejected_now": rejected_now,
        "already_rejected": already_rejected,
        "dry_run": dry_run,
    }


MARKET_FOCUS_MAP: dict[str, set[str]] = {
    "international": set(),
    "dach": {"DE", "AT", "CH"},
    "anglo": {"US", "GB", "CA", "AU", "IE", "NZ"},
    "eu_core": {"DE", "FR", "NL", "BE", "ES", "IT", "SE", "NO", "DK", "FI", "AT", "CH", "IE"},
    "top_major": {"US", "GB", "DE", "FR", "CA", "AU", "NL", "SE", "ES", "IT", "JP"},
}


def market_focus_countries(market_focus: str) -> set[str]:
    key = (market_focus or "international").strip().lower()
    return set(MARKET_FOCUS_MAP.get(key, set()))


def _load_state_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            out = dict(default)
            out.update(payload)
            return out
    except Exception:
        pass
    return dict(default)


def _save_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_scan_focus_state() -> dict[str, Any]:
    return _load_state_file(
        Path(".radio_db_state") / "scan_focus_state.json",
        {
            "enabled": False,
            "market_focus": "international",
            "updated_by": "system",
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    )


def save_scan_focus_state(enabled: bool, market_focus: str = "international", updated_by: str = "system") -> dict[str, Any]:
    payload = {
        "enabled": bool(enabled),
        "market_focus": (market_focus or "international").strip().lower(),
        "updated_by": updated_by,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _save_state_file(Path(".radio_db_state") / "scan_focus_state.json", payload)
    return payload


def get_scan_focus_countries() -> set[str]:
    state = load_scan_focus_state()
    if not state.get("enabled"):
        return set()
    return market_focus_countries(str(state.get("market_focus", "international")))


def load_brave_boost_state() -> dict[str, Any]:
    return _load_state_file(
        Path(".radio_db_state") / "brave_boost_state.json",
        {
            "is_running": False,
            "last_market_focus": "international",
            "last_run_at": None,
            "last_result": {},
            "unique_seen_by_market": {},
        },
    )


def _save_brave_boost_state(payload: dict[str, Any]) -> None:
    _save_state_file(Path(".radio_db_state") / "brave_boost_state.json", payload)


def load_high_priority_state() -> dict[str, Any]:
    return _load_state_file(
        Path(".radio_db_state") / "high_priority_state.json",
        {
            "is_running": False,
            "last_run_at": None,
            "last_result": {},
        },
    )


def _save_high_priority_state(payload: dict[str, Any]) -> None:
    _save_state_file(Path(".radio_db_state") / "high_priority_state.json", payload)


def boost_top_major_station_ids(session: Session, min_station_confidence: float) -> set[int]:
    countries = market_focus_countries("top_major")
    rows = session.scalars(
        select(Station)
        .where(
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
            Station.website_url.is_not(None),
            Station.confidence_score >= min_station_confidence,
            func.upper(Station.country_code).in_(sorted(countries)),
        )
        .order_by(Station.confidence_score.desc(), Station.updated_at.desc())
        .limit(400)
    ).all()
    return {row.id for row in rows}


def run_brave_boost_round(
    session: Session,
    budget_usd: float,
    market_focus: str = "international",
) -> dict[str, Any]:
    state = load_brave_boost_state()
    state["is_running"] = True
    _save_brave_boost_state(state)
    try:
        focus = (market_focus or "international").strip().lower()
        countries = market_focus_countries(focus)
        max_stations = max(1, int(float(budget_usd) / max(0.001, settings.brave_boost_usd_per_call)))
        repeat_cutoff = datetime.utcnow() - timedelta(days=max(1, settings.brave_boost_no_repeat_days))

        station_query = (
            select(Station)
            .where(
                Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
                Station.website_url.is_not(None),
                Station.confidence_score >= settings.brave_boost_min_station_confidence,
            )
            .order_by(Station.confidence_score.desc(), Station.updated_at.desc())
        )
        if countries:
            station_query = station_query.where(func.upper(Station.country_code).in_(sorted(countries)))
        candidates = session.scalars(station_query.limit(max_stations * 5)).all()

        recently_seen = {
            sid
            for (sid,) in session.execute(
                select(Evidence.station_id).where(
                    Evidence.source_id == "brave_boost",
                    Evidence.created_at >= repeat_cutoff,
                    Evidence.station_id.is_not(None),
                )
            ).all()
        }
        picked = [st for st in candidates if st.id not in recently_seen][:max_stations]

        processed = 0
        hits = 0
        for st in picked:
            processed += 1
            result = station_enrich_search(
                session=session,
                station_id=st.id,
                station_limit=1,
                queries_per_station=1,
                min_confidence=0.35,
            )
            hits += int(result.get("results", 0))
            _save_evidence(
                session=session,
                source_type=SourceType.WEBSITE,
                source_url=st.website_url,
                source_id="brave_boost",
                raw_title=st.canonical_name,
                raw_snippet=f"market_focus={focus}",
                extracted_payload={"station_id": st.id, "market_focus": focus},
                confidence=st.confidence_score,
                station_id=st.id,
            )
            session.commit()

        state = load_brave_boost_state()
        unique_seen = dict(state.get("unique_seen_by_market") or {})
        arr = list(unique_seen.get(focus, []) or [])
        arr.extend([st.id for st in picked])
        unique_seen[focus] = sorted(set(int(x) for x in arr))
        result = {
            "market_focus": focus,
            "countries": sorted(countries),
            "processed_stations": processed,
            "result_hits": hits,
            "budget_usd": float(budget_usd),
        }
        state.update(
            {
                "is_running": False,
                "last_market_focus": focus,
                "last_run_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "last_result": result,
                "unique_seen_by_market": unique_seen,
            }
        )
        _save_brave_boost_state(state)
        return result
    except Exception as exc:
        state = load_brave_boost_state()
        state["is_running"] = False
        state["last_error"] = str(exc)
        _save_brave_boost_state(state)
        raise


def run_high_priority_deep_dive(session: Session, station_limit: int) -> dict[str, Any]:
    state = load_high_priority_state()
    state["is_running"] = True
    _save_high_priority_state(state)
    try:
        stations, scores, queue = _build_station_priority_pool(
            session=session,
            station_limit=max(1, station_limit),
            min_station_confidence=settings.station_enrich_min_station_confidence,
        )
        processed = 0
        hits = 0
        for st in stations[: max(1, station_limit)]:
            processed += 1
            result = station_enrich_search(
                session=session,
                station_id=st.id,
                station_limit=1,
                queries_per_station=2,
                min_confidence=0.35,
            )
            hits += int(result.get("results", 0))

        payload = {
            "processed_stations": processed,
            "result_hits": hits,
            "avg_priority_score": round(
                (sum(scores.get(st.id, 0.0) for st in stations[: max(1, station_limit)]) / max(1, processed)),
                2,
            ),
            "queue_mix": queue,
        }
        state = load_high_priority_state()
        state.update(
            {
                "is_running": False,
                "last_run_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "last_result": payload,
            }
        )
        _save_high_priority_state(state)
        return payload
    except Exception as exc:
        state = load_high_priority_state()
        state["is_running"] = False
        state["last_error"] = str(exc)
        _save_high_priority_state(state)
        raise


def stats(session: Session) -> dict:
    total_stations = session.scalar(select(func.count(Station.id))) or 0
    verified = session.scalar(select(func.count(Station.id)).where(Station.status == StationStatus.VERIFIED)) or 0
    submissions = session.scalar(select(func.count(SubmissionChannel.id))) or 0
    contacts = session.scalar(select(func.count(StationContact.id))) or 0
    queries = session.scalar(select(func.count(CrawlFrontier.id))) or 0
    evidence = session.scalar(select(func.count(Evidence.id))) or 0
    profiles = session.scalar(select(func.count(StationProfileSnapshot.id))) or 0
    people = session.scalar(select(func.count(StationPerson.id))) or 0
    forms = session.scalar(select(func.count(SubmissionForm.id))) or 0
    form_fields = session.scalar(select(func.count(SubmissionFormField.id))) or 0
    form_recipes = session.scalar(select(func.count(FormRecipe.id))) or 0
    stations_with_forms = session.scalar(select(func.count(func.distinct(SubmissionForm.station_id)))) or 0
    stations_with_submission = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id)))
    ) or 0
    stations_with_newcomer_signal = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id))).where(
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
            StationPerson.role.in_(list(DECISION_ROLES))
        )
    ) or 0
    pitch_ready_stations = session.scalar(
        select(func.count(Station.id)).where(
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
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
                select(StationPerson.station_id).where(StationPerson.role.in_(list(DECISION_ROLES)))
            ),
        )
    ) or 0

    return {
        "stations_total": total_stations,
        "stations_verified": verified,
        "submission_channels": submissions,
        "stations_with_submission": stations_with_submission,
        "stations_with_newcomer_signal": stations_with_newcomer_signal,
        "stations_with_decision_maker": stations_with_decision_maker,
        "pitch_ready_stations": pitch_ready_stations,
        "contact_targets": contacts,
        "frontier_queries": queries,
        "evidence_rows": evidence,
        "profile_snapshots": profiles,
        "people_entities": people,
        "submission_forms": forms,
        "form_fields": form_fields,
        "form_recipes": form_recipes,
        "stations_with_forms": stations_with_forms,
    }

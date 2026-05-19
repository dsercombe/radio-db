from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    EnrichmentFinding,
    EnrichmentQueue,
    EnrichmentRun,
    Evidence,
    FormStatus,
    FormType,
    SourceType,
    Station,
    StationContact,
    StationPerson,
    StationProfileSnapshot,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionAgentRun,
    SubmissionChannel,
    SubmissionForm,
)
from radio_db.services.source_search import collect_station_enrichment_results


EMAIL_RE = re.compile(r"\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b", re.I)
SUBMISSION_HINTS = (
    "submit music",
    "music submission",
    "demo submission",
    "playlist submission",
    "send your music",
    "airplay",
    "new artist",
    "unsigned artist",
    "music director",
    "program director",
    "promo",
)
STRONG_SUBMISSION_HINTS = (
    "submit music",
    "music submission",
    "demo submission",
    "playlist submission",
    "send your music",
    "send us your music",
    "submit your music",
    "artist submission",
)
PEOPLE_HINTS = (
    "host",
    "presenter",
    "dj",
    "producer",
    "music director",
    "program director",
    "playlist",
    "team",
    "staff",
    "shows",
)
PATH_HINTS = (
    "contact",
    "about",
    "team",
    "staff",
    "shows",
    "program",
    "programs",
    "playlist",
    "music",
    "submit",
    "submission",
    "demo",
    "airplay",
)
STRONG_SUBMISSION_PATH_HINTS = (
    "/submit",
    "/submission",
    "/submit-music",
    "/music-submission",
    "/demo",
    "/airplay",
    "/contact",
)
API_CALL_ESTIMATE_USD = {
    "brave": 0.015,
    "google_cse": 0.005,
    "tavily": 0.004,
    "grok": 0.005,
    "linkup": 0.01,
    "duckduckgo": 0.0,
}
SKIP_URL_EXTENSIONS = (
    ".7z",
    ".aac",
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".m3u",
    ".mp3",
    ".mp4",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".svg",
    ".ttf",
    ".wav",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
)
LANGUAGE_ALIASES = {
    "english": "en",
    "en": "en",
    "german": "de",
    "deutsch": "de",
    "de": "de",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
    "es": "es",
    "portuguese": "pt",
    "brazilian portuguese": "pt",
    "pt": "pt",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "fr": "fr",
    "italian": "it",
    "italiano": "it",
    "it": "it",
    "dutch": "nl",
    "nederlands": "nl",
    "nl": "nl",
    "swedish": "sv",
    "svenska": "sv",
    "sv": "sv",
    "norwegian": "no",
    "norsk": "no",
    "no": "no",
    "danish": "da",
    "dansk": "da",
    "da": "da",
    "polish": "pl",
    "polski": "pl",
    "pl": "pl",
    "hindi": "hi",
    "hi": "hi",
}
COUNTRY_LANGUAGE_MAP = {
    "AR": "es",
    "AT": "de",
    "BE": "nl",
    "BR": "pt",
    "CH": "de",
    "CL": "es",
    "CO": "es",
    "DE": "de",
    "DK": "da",
    "ES": "es",
    "FR": "fr",
    "IE": "en",
    "IN": "hi",
    "IT": "it",
    "MX": "es",
    "NL": "nl",
    "NO": "no",
    "NZ": "en",
    "PE": "es",
    "PL": "pl",
    "PT": "pt",
    "SE": "sv",
    "UK": "en",
    "GB": "en",
    "US": "en",
}
QUERY_TERMS = {
    "en": {
        "contact": ("contact", "team", "staff", "about", "program director", "music director"),
        "submission": ("submit music", "music submission", "send us your music", "airplay", "demo submission"),
        "people": ("hosts", "presenters", "djs", "shows", "team", "music director"),
    },
    "de": {
        "contact": ("kontakt", "team", "redaktion", "impressum", "musikredaktion"),
        "submission": ("musik einreichen", "bemusterung", "demo einreichen", "airplay", "promos"),
        "people": ("moderatoren", "sendungen", "redaktion", "team", "musikredaktion"),
    },
    "es": {
        "contact": ("contacto", "equipo", "programas", "locutores", "director musical"),
        "submission": ("enviar musica", "envia tu musica", "promocion musical", "demo", "airplay"),
        "people": ("locutores", "conductores", "programas", "equipo", "director musical"),
    },
    "pt": {
        "contact": ("contato", "contacto", "equipe", "programas", "diretor musical"),
        "submission": ("enviar musica", "envie sua musica", "divulgacao musical", "demo", "airplay"),
        "people": ("locutores", "apresentadores", "programas", "equipe", "diretor musical"),
    },
    "fr": {
        "contact": ("contact", "equipe", "animateurs", "emissions", "directeur musical"),
        "submission": ("envoyer votre musique", "soumettre musique", "promo musicale", "demo", "airplay"),
        "people": ("animateurs", "emissions", "equipe", "programmation", "directeur musical"),
    },
    "it": {
        "contact": ("contatti", "team", "speaker", "programmi", "direttore musicale"),
        "submission": ("invia musica", "proponi la tua musica", "demo", "promozione musicale", "airplay"),
        "people": ("speaker", "programmi", "conduttori", "redazione", "direttore musicale"),
    },
    "nl": {
        "contact": ("contact", "team", "presentatoren", "programmas", "muziekredactie"),
        "submission": ("muziek insturen", "demo insturen", "promotie", "airplay"),
        "people": ("presentatoren", "programmas", "dj", "team", "muziekredactie"),
    },
    "sv": {
        "contact": ("kontakt", "team", "program", "programledare", "musikredaktion"),
        "submission": ("skicka musik", "demo", "musiksubmission", "airplay", "promo"),
        "people": ("programledare", "program", "dj", "team", "musikredaktion"),
    },
    "no": {
        "contact": ("kontakt", "team", "program", "programledere", "musikkredaksjon"),
        "submission": ("send musikk", "demo", "promo", "airplay"),
        "people": ("programledere", "program", "dj", "team", "musikkredaksjon"),
    },
    "da": {
        "contact": ("kontakt", "team", "programmer", "vaerter", "musikredaktion"),
        "submission": ("send musik", "demo", "promo", "airplay"),
        "people": ("vaerter", "programmer", "dj", "team", "musikredaktion"),
    },
    "pl": {
        "contact": ("kontakt", "zespol", "prowadzacy", "programy", "redakcja muzyczna"),
        "submission": ("wyslij muzyke", "demo", "promocja muzyczna", "airplay"),
        "people": ("prowadzacy", "programy", "dj", "zespol", "redakcja muzyczna"),
    },
    "hi": {
        "contact": ("contact", "team", "rj", "shows", "programming"),
        "submission": ("submit music", "send music", "demo", "airplay", "music director"),
        "people": ("rj", "shows", "presenters", "team", "programming"),
    },
}
LARGE_NETWORK_MARKERS = (
    "bbc",
    "rai",
    "radio france",
    "srf",
    "rtp",
    "rtve",
    "iheart",
    "los 40",
    "los40",
    "rte",
    "rté",
    "kbs",
    "nrk",
    "dr p",
    "abc radio",
    "radio national",
)
COMMUNITY_MARKERS = (
    "college",
    "university",
    "campus",
    "community",
    "freie",
    "universit",
    "associative",
    "student",
    "local",
)
ONLINE_SPECIALIST_MARKERS = (
    "online",
    "indie",
    "rock",
    "metal",
    "jazz",
    "electronic",
    "dance",
    "house",
    "techno",
    "reggae",
    "mix",
)


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {str(_strip_nul(k)): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, tuple):
        return [_strip_nul(v) for v in value]
    return value


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _same_domain(url: str, website_url: str | None) -> bool:
    left = _domain(url)
    right = _domain(website_url)
    return bool(left and right and (left == right or left.endswith(f".{right}")))


def _looks_like_fetchable_page(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    if any(path.endswith(ext) for ext in SKIP_URL_EXTENSIONS):
        return False
    if any(segment in path for segment in ("/files/get/", "/file/get/", "/download/", "/uploads/")):
        return False
    return True


def _compact(text: str | None, limit: int = 500) -> str:
    clean = (text or "").replace("\x00", "")
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def _looks_like_binary_text(text: str | None) -> bool:
    if not text:
        return False
    sample = text[:1200]
    if "\x00" in sample:
        return True
    control_chars = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r")
    replacement_chars = sample.count("\ufffd")
    return (control_chars + replacement_chars) > max(8, len(sample) // 40)


def _emails(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for email in EMAIL_RE.findall(text or ""):
        clean = email.lower().strip().rstrip(".,;:)]}")
        local, _, domain = clean.partition("@")
        labels = domain.split(".")
        if (
            clean in seen
            or len(local) < 2
            or len(labels) < 2
            or any(len(label) < 2 for label in labels)
            or any(clean.endswith(f".{ext}") for ext in ("png", "jpg", "jpeg", "gif", "css", "js"))
        ):
            continue
        seen.add(clean)
        out.append(clean)
    return out[:25]


def _has_any(text: str, hints: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in hints)


def _station_language_keys(station: Station) -> list[str]:
    keys: list[str] = []
    raw_language = (station.language or "").strip().lower()
    if raw_language:
        keys.append(LANGUAGE_ALIASES.get(raw_language, raw_language[:2]))
    country_key = COUNTRY_LANGUAGE_MAP.get((station.country_code or "").upper())
    if country_key:
        keys.append(country_key)
    keys.append("en")
    out: list[str] = []
    for key in keys:
        if key in QUERY_TERMS and key not in out:
            out.append(key)
    return out


def _terms_for_station(station: Station, category: str) -> tuple[str, ...]:
    terms: list[str] = []
    for key in _station_language_keys(station):
        terms.extend(QUERY_TERMS.get(key, {}).get(category, ()))
    return tuple(dict.fromkeys(terms))


def _tokens(text: str | None) -> set[str]:
    stop = {"radio", "fm", "am", "the", "and", "online", "live", "mp3", "aac"}
    return {
        token
        for token in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(token) >= 3 and token not in stop and not token.isdigit()
    }


def _has_station_relevance(station: Station, url: str, text_blob: str) -> bool:
    if _same_domain(url, station.website_url):
        return True
    station_domain = _domain(station.website_url)
    lowered = (text_blob or "").lower()
    if station_domain and station_domain in lowered:
        return True
    station_tokens = _tokens(station.canonical_name)
    if not station_tokens:
        return False
    text_tokens = _tokens(text_blob)
    shared = station_tokens & text_tokens
    return len(shared) >= max(1, min(2, len(station_tokens)))


def _has_strong_submission_signal(url: str, text_blob: str, station: Station | None = None) -> bool:
    lowered = (text_blob or "").lower()
    path = (urlparse(url).path or "").lower()
    hints = list(STRONG_SUBMISSION_HINTS)
    if station is not None:
        hints.extend(_paid_submission_terms(station))
    return any(hint in lowered for hint in hints) or any(hint in path for hint in STRONG_SUBMISSION_PATH_HINTS)


def _paid_submission_terms(station: Station) -> tuple[str, ...]:
    too_broad = {"airplay", "demo", "promo", "promos"}
    localized = [term for term in _terms_for_station(station, "submission") if term not in too_broad and len(term) > 6]
    return tuple(dict.fromkeys(STRONG_SUBMISSION_HINTS + tuple(localized)))


def _has_contact_signal(url: str, text_blob: str, station: Station) -> bool:
    lowered = (text_blob or "").lower()
    path = (urlparse(url).path or "").lower()
    contact_terms = tuple(dict.fromkeys(("contact", "about", "team", "staff", "impressum") + _terms_for_station(station, "contact")))
    return any(term in lowered for term in contact_terms) or any(hint in path for hint in ("/contact", "/contato", "/contacto", "/kontakt", "/about", "/team", "/staff", "/impressum"))


def _has_people_signal(url: str, text_blob: str, station: Station) -> bool:
    lowered = (text_blob or "").lower()
    path = (urlparse(url).path or "").lower()
    people_terms = tuple(
        dict.fromkeys(
            (
                "host",
                "presenter",
                "dj",
                "music director",
                "program director",
                "team",
                "staff",
                "shows",
            )
            + _terms_for_station(station, "people")
        )
    )
    people_paths = ("/team", "/staff", "/shows", "/program", "/programas", "/locutores", "/apresentadores", "/contato", "/contact")
    return any(term in lowered for term in people_terms) and (
        any(hint in path for hint in people_paths) or _has_contact_signal(url, text_blob, station)
    )


def _station_context_text(station: Station) -> str:
    genres = " ".join(getattr(genre, "genre", "") or "" for genre in getattr(station, "genres", []) or [])
    return f"{station.canonical_name} {station.country_code or ''} {station.language or ''} {genres} {_domain(station.website_url)}".lower()


def _classify_station_policy(station: Station) -> str:
    text = _station_context_text(station)
    if any(marker in text for marker in LARGE_NETWORK_MARKERS) or int(station.priority_tier or 0) >= 4:
        return "large_network"
    if any(marker in text for marker in COMMUNITY_MARKERS):
        return "community_college"
    if any(marker in text for marker in ONLINE_SPECIALIST_MARKERS):
        return "online_specialist"
    if int(station.priority_tier or 0) >= 2:
        return "commercial_mainstream"
    return "unknown"


def _or_terms(terms: tuple[str, ...], limit: int = 5) -> str:
    quoted = [f'"{term}"' if " " in term else term for term in terms[:limit]]
    return " OR ".join(quoted)


def _extract_links(base_url: str, html: str, limit: int = 80) -> list[str]:
    links: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"""href=["']([^"']+)["']""", html or "", flags=re.I):
        href = match.group(1).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if url in seen or not _same_domain(url, base_url) or not _looks_like_fetchable_page(url):
            continue
        seen.add(url)
        lowered = url.lower()
        score = 1
        for hint in PATH_HINTS:
            if hint in lowered:
                score += 8
        links.append((score, url))
    links.sort(key=lambda item: (-item[0], item[1]))
    return [url for _score, url in links[:limit]]


def _station_counts(session: Session, station_id: int) -> dict[str, int]:
    return {
        "forms": int(session.scalar(select(func.count(SubmissionForm.id)).where(SubmissionForm.station_id == station_id)) or 0),
        "active_music_forms": int(
            session.scalar(
                select(func.count(SubmissionForm.id)).where(
                    SubmissionForm.station_id == station_id,
                    SubmissionForm.status == FormStatus.ACTIVE,
                    SubmissionForm.form_type.in_([FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER]),
                )
            )
            or 0
        ),
        "submission_channels": int(
            session.scalar(select(func.count(SubmissionChannel.id)).where(SubmissionChannel.station_id == station_id)) or 0
        ),
        "people": int(session.scalar(select(func.count(StationPerson.id)).where(StationPerson.station_id == station_id)) or 0),
        "contacts": int(session.scalar(select(func.count(StationContact.id)).where(StationContact.station_id == station_id)) or 0),
        "profiles": int(
            session.scalar(select(func.count(StationProfileSnapshot.id)).where(StationProfileSnapshot.station_id == station_id)) or 0
        ),
    }


def _latest_assessment(session: Session, station_id: int, kind: str) -> StationSubmissionAssessment | None:
    return session.scalar(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == station_id, StationSubmissionAssessment.assessment_kind == kind)
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )


def _latest_main_scan(session: Session, station_id: int) -> SubmissionAgentRun | None:
    return session.scalar(
        select(SubmissionAgentRun)
        .where(SubmissionAgentRun.station_id == station_id, SubmissionAgentRun.mode == "scan")
        .order_by(SubmissionAgentRun.finished_at.desc().nulls_last(), SubmissionAgentRun.id.desc())
    )


def _upsert_queue(
    session: Session,
    station_id: int,
    source_pool: str,
    priority_score: float,
    reason_codes: list[str],
    api_budget_class: str,
    free_findings: dict[str, Any] | None = None,
) -> EnrichmentQueue:
    row = session.scalar(
        select(EnrichmentQueue).where(
            EnrichmentQueue.station_id == station_id,
            EnrichmentQueue.source_pool == source_pool,
        )
    )
    if row is None:
        row = EnrichmentQueue(station_id=station_id, source_pool=source_pool)
        session.add(row)
        session.flush()
    row.priority_score = max(float(row.priority_score or 0.0), float(priority_score))
    if row.status in {"done", "deferred", "error"}:
        row.status = "pending"
    row.api_budget_class = api_budget_class
    existing = set(_json_loads(row.reason_codes_json, []))
    row.reason_codes_json = _json_dumps(sorted(existing | set(reason_codes)))
    if free_findings:
        current = _json_loads(row.free_findings_json, {})
        current.update(free_findings)
        row.free_findings_json = _json_dumps(current)
    row.updated_at = datetime.utcnow()
    return row


def _record_finding(
    session: Session,
    run: EnrichmentRun,
    station_id: int,
    finding_type: str,
    source_url: str | None,
    title: str | None,
    snippet: str | None,
    confidence: float,
    useful: bool,
    source_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    source_url = _strip_nul(source_url)
    source_name = _strip_nul(source_name)
    title = _compact(_strip_nul(title), 500) if title else title
    snippet = _compact(_strip_nul(snippet), 1200) if snippet else snippet
    payload = _strip_nul(payload or {})
    finding = EnrichmentFinding(
        run_id=run.id,
        station_id=station_id,
        layer=run.layer,
        finding_type=finding_type,
        source_url=source_url,
        source_name=source_name,
        title=title,
        snippet=snippet,
        payload_json=_json_dumps(payload),
        confidence=max(0.0, min(1.0, float(confidence))),
        useful=bool(useful),
    )
    session.add(finding)
    if useful:
        session.add(
            Evidence(
                station_id=station_id,
                source_type=SourceType.WEBSITE if run.layer == "free" else SourceType.BRAVE_SEARCH,
                source_url=source_url,
                source_id=f"enrichment_{run.layer}:{finding_type}",
                raw_title=title,
                raw_snippet=snippet,
                extracted_payload_json=_json_dumps(payload),
                confidence=max(0.0, min(1.0, float(confidence))),
            )
        )


def _score_station_for_free(session: Session, station: Station) -> tuple[float, list[str], str]:
    counts = _station_counts(session, int(station.id))
    latest_scan = _latest_main_scan(session, int(station.id))
    quality = _latest_assessment(session, int(station.id), settings.station_quality_assessment_kind)
    editorial = _latest_assessment(session, int(station.id), settings.editorial_enrichment_assessment_kind)
    reasons: list[str] = []
    score = 0.0

    if station.status == StationStatus.VERIFIED:
        score += 40
    elif station.status == StationStatus.CANDIDATE:
        score += 25
    if int(station.priority_tier or 0) > 0:
        score += 25 + (5 * min(5, int(station.priority_tier or 0)))
        reasons.append("priority_tier")
    if counts["submission_channels"] == 0 and counts["active_music_forms"] == 0:
        score += 45
        reasons.append("missing_submission_route")
    if counts["people"] == 0:
        score += 20
        reasons.append("people_gap")
    if counts["profiles"] == 0 or editorial is None:
        score += 18
        reasons.append("editorial_gap")
    if quality is None:
        score += 15
        reasons.append("quality_gap")
    if latest_scan is not None and latest_scan.status == "blocked":
        score += 20
        reasons.append(f"main_scan_blocked:{latest_scan.blocked_reason or 'unknown'}")

    budget_class = "low"
    if score >= 95:
        budget_class = "high"
    elif score >= 65:
        budget_class = "normal"
    return score, sorted(set(reasons)), budget_class


def _ensure_queue_for_station(session: Session, station_id: int, min_priority: float = 0.0) -> EnrichmentQueue | None:
    station = session.get(Station, int(station_id))
    if station is None or not station.website_url:
        return None
    score, reasons, budget_class = _score_station_for_free(session, station)
    if score < min_priority:
        return None
    policy = _classify_station_policy(station)
    language_keys = _station_language_keys(station)
    reason_set = set(reasons)
    reason_set.add(f"policy:{policy}")
    for language_key in language_keys[:2]:
        reason_set.add(f"language:{language_key}")
    return _upsert_queue(
        session=session,
        station_id=int(station.id),
        source_pool="existing_station",
        priority_score=max(score, min_priority),
        reason_codes=sorted(reason_set),
        api_budget_class=budget_class,
        free_findings={"policy": policy, "language_keys": language_keys},
    )


def seed_enrichment_queue(
    session: Session,
    limit: int = 500,
    min_priority: float = 35.0,
    station_id: int | None = None,
) -> dict[str, Any]:
    if station_id is not None:
        row = _ensure_queue_for_station(session, int(station_id), min_priority=0.0)
        session.commit()
        return {
            "considered": 1,
            "queued": 1 if row is not None else 0,
            "skipped": 0 if row is not None else 1,
            "by_budget": {str(row.api_budget_class): 1} if row is not None else {},
            "station_id": station_id,
        }
    stations = session.scalars(
        select(Station)
        .where(
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
            Station.website_url.is_not(None),
        )
        .order_by(Station.priority_tier.desc(), Station.confidence_score.desc(), Station.updated_at.desc())
        .limit(max(1, limit))
    ).all()
    inserted_or_updated = 0
    skipped = 0
    by_budget = {"low": 0, "normal": 0, "high": 0}
    for station in stations:
        score, reasons, budget_class = _score_station_for_free(session, station)
        if score < min_priority or not reasons:
            skipped += 1
            continue
        policy = _classify_station_policy(station)
        language_keys = _station_language_keys(station)
        reason_set = set(reasons)
        reason_set.add(f"policy:{policy}")
        for language_key in language_keys[:2]:
            reason_set.add(f"language:{language_key}")
        _upsert_queue(
            session=session,
            station_id=int(station.id),
            source_pool="existing_station",
            priority_score=score,
            reason_codes=sorted(reason_set),
            api_budget_class=budget_class,
            free_findings={"policy": policy, "language_keys": language_keys},
        )
        inserted_or_updated += 1
        by_budget[budget_class] = by_budget.get(budget_class, 0) + 1
    session.commit()
    return {"considered": len(stations), "queued": inserted_or_updated, "skipped": skipped, "by_budget": by_budget}


def run_free_enrichment_scan(
    session: Session,
    limit: int = 100,
    max_urls_per_station: int = 12,
    station_id: int | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(hours=max(1, settings.enrichment_free_rescan_hours))
    if station_id is not None:
        row = _ensure_queue_for_station(session, int(station_id), min_priority=0.0)
        rows = [row] if row is not None else []
    else:
        rows = session.scalars(
            select(EnrichmentQueue)
            .where(
                EnrichmentQueue.status.in_(["pending", "free_scanned", "flagged_for_paid", "deferred"]),
                (EnrichmentQueue.free_last_run_at.is_(None) | (EnrichmentQueue.free_last_run_at < stale_cutoff)),
            )
            .order_by(EnrichmentQueue.priority_score.desc(), EnrichmentQueue.updated_at.asc())
            .limit(max(1, limit))
        ).all()
    summary = {
        "layer": "free",
        "queue_rows": len(rows),
        "station_id": station_id,
        "processed": 0,
        "flagged_for_paid": 0,
        "done": 0,
        "deferred": 0,
        "errors": 0,
        "findings": 0,
        "useful_findings": 0,
        "urls_checked": 0,
        "outcomes": {},
    }
    for row in rows:
        station = session.get(Station, int(row.station_id))
        if station is None or not station.website_url:
            row.status = "error"
            row.error = "station_missing_or_no_website"
            row.updated_at = now
            summary["errors"] += 1
            continue
        run = EnrichmentRun(
            queue_id=row.id,
            station_id=station.id,
            layer="free",
            source_pool=row.source_pool,
            reason_codes_json=row.reason_codes_json,
        )
        session.add(run)
        session.flush()
        urls_checked = 0
        findings = 0
        useful = 0
        errors: list[str] = []
        discovered_urls: list[str] = []
        try:
            submission_hints = tuple(dict.fromkeys(SUBMISSION_HINTS + _terms_for_station(station, "submission")))
            people_hints = tuple(dict.fromkeys(PEOPLE_HINTS + _terms_for_station(station, "people") + _terms_for_station(station, "contact")))
            html = get_text(station.website_url, timeout_seconds=settings.enrichment_free_fetch_timeout_seconds, max_attempts=1)
            urls_checked += 1
            discovered_urls = _extract_links(station.website_url, html, limit=max_urls_per_station)
            page_text = _compact(html, 5000)
            page_emails = _emails(html)
            if page_emails:
                findings += len(page_emails)
                useful += len(page_emails)
                _record_finding(
                    session,
                    run,
                    int(station.id),
                    "contact_email_hint",
                    station.website_url,
                    station.canonical_name,
                    ", ".join(page_emails[:6]),
                    0.55,
                    True,
                    payload={"emails": page_emails[:10]},
                )
            if _has_any(page_text, submission_hints):
                findings += 1
                useful += 1
                _record_finding(session, run, int(station.id), "submission_hint", station.website_url, station.canonical_name, page_text[:350], 0.6, True)
            if _has_any(page_text, people_hints):
                findings += 1
                useful += 1
                _record_finding(session, run, int(station.id), "people_hint", station.website_url, station.canonical_name, page_text[:350], 0.45, True)

            for url in discovered_urls[: max(0, max_urls_per_station - 1)]:
                if not _looks_like_fetchable_page(url):
                    continue
                try:
                    text = get_text(url, timeout_seconds=settings.enrichment_free_fetch_timeout_seconds, max_attempts=1)
                    urls_checked += 1
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}:{url[:120]}")
                    continue
                if _looks_like_binary_text(text):
                    errors.append(f"binary_text_skipped:{url[:120]}")
                    continue
                compact = _compact(text, 5000)
                emails = _emails(text)
                is_submission = _has_any(compact, submission_hints)
                is_people = _has_any(compact, people_hints)
                if emails or is_submission or is_people:
                    ftype = "submission_hint" if is_submission else "people_hint" if is_people else "contact_email_hint"
                    confidence = 0.7 if is_submission else 0.5 if is_people else 0.45
                    findings += 1
                    useful += 1
                    _record_finding(
                        session,
                        run,
                        int(station.id),
                        ftype,
                        url,
                        station.canonical_name,
                        compact[:350] if not emails else ", ".join(emails[:6]),
                        confidence,
                        True,
                        payload={"emails": emails[:10], "url": url},
                    )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")

        reason_codes = set(_json_loads(row.reason_codes_json, []))
        if useful > 0:
            reason_codes.add("new_free_findings")
        policy = _classify_station_policy(station)
        reason_codes.add(f"policy:{policy}")
        for language_key in _station_language_keys(station)[:2]:
            reason_codes.add(f"language:{language_key}")
        counts = _station_counts(session, int(station.id))
        should_paid = bool(
            useful > 0
            and (
                "missing_submission_route" in reason_codes
                or "people_gap" in reason_codes
                or "main_scan_blocked:no_form_detected" in reason_codes
            )
        )
        if should_paid:
            row.status = "flagged_for_paid"
            summary["flagged_for_paid"] += 1
            outcome = "flagged_for_paid"
        elif useful > 0 or counts["submission_channels"] > 0 or counts["active_music_forms"] > 0:
            row.status = "free_scanned"
            outcome = "free_findings_recorded" if useful else "already_has_submission_context"
            summary["done"] += 1
        else:
            row.status = "deferred"
            outcome = "no_free_signal"
            summary["deferred"] += 1

        row.reason_codes_json = _json_dumps(sorted(reason_codes))
        row.free_findings_json = _json_dumps(
            {
                "useful_findings": useful,
                "findings": findings,
                "urls_checked": urls_checked,
                "errors": errors[:8],
                "policy": policy,
                "language_keys": _station_language_keys(station),
            }
        )
        row.free_last_run_at = now
        row.last_outcome = outcome
        row.last_run_id = run.id
        row.error = "; ".join(errors[:3]) if errors and useful == 0 else None
        row.updated_at = now
        run.status = "done"
        run.outcome = outcome
        if errors and urls_checked == 0 and useful == 0:
            run.stop_reason = "free_fetch_failed"
        else:
            run.stop_reason = "free_budget_exhausted" if urls_checked >= max_urls_per_station else "free_pass_complete"
        run.urls_checked = urls_checked
        run.findings_count = findings
        run.useful_findings_count = useful
        run.metrics_json = _json_dumps(
            {
                "errors": errors[:8],
                "discovered_urls": discovered_urls[:20],
                "policy": policy,
                "language_keys": _station_language_keys(station),
            }
        )
        run.error = row.error
        run.finished_at = datetime.utcnow()
        summary["processed"] += 1
        summary["findings"] += findings
        summary["useful_findings"] += useful
        summary["urls_checked"] += urls_checked
        summary["outcomes"][outcome] = int(summary["outcomes"].get(outcome, 0)) + 1
        session.commit()
    return summary


def _api_call_budget(api_budget_class: str) -> int:
    if api_budget_class == "high":
        return max(1, settings.enrichment_paid_high_api_calls)
    if api_budget_class == "normal":
        return max(1, settings.enrichment_paid_normal_api_calls)
    if api_budget_class == "low":
        return max(0, settings.enrichment_paid_low_api_calls)
    return 0


def _build_paid_queries(station: Station, reasons: list[str]) -> list[str]:
    name = station.canonical_name
    domain = _domain(station.website_url)
    queries: list[str] = []
    policy = _classify_station_policy(station)
    contact_terms = _or_terms(_terms_for_station(station, "contact"), limit=5)
    submission_terms = _or_terms(_terms_for_station(station, "submission"), limit=5)
    people_terms = _or_terms(_terms_for_station(station, "people"), limit=5)
    contact_query_terms = contact_terms or "contact OR team OR about"
    submission_query_terms = submission_terms or '"submit music" OR "music submission" OR airplay'
    people_query_terms = people_terms or '"music director" OR "program director" OR host OR dj'
    needs_submission = "missing_submission_route" in reasons or any(r.startswith("main_scan_blocked") for r in reasons)
    needs_people = "people_gap" in reasons

    if domain and needs_submission:
        queries.append(f"site:{domain} {submission_query_terms}")
    if domain and needs_people:
        queries.append(f"site:{domain} {people_query_terms}")
    if domain and not (needs_submission or needs_people):
        queries.append(f"site:{domain} {contact_query_terms}")

    if policy == "large_network":
        if domain and len(queries) < 4:
            queries.append(f"site:{domain} {contact_query_terms}")
        if len(queries) < 4:
            queries.append(f'"{name}" official contact programming')
        return list(dict.fromkeys(queries))[:4]

    if policy == "community_college":
        if len(queries) < 4:
            queries.append(f'"{name}" "music director" OR "program director" OR "submit music"')
        if len(queries) < 4:
            queries.append(f'"{name}" campus radio demo submission')
        return list(dict.fromkeys(queries))[:4]

    if "missing_submission_route" in reasons or any(r.startswith("main_scan_blocked") for r in reasons):
        queries.append(f'"{name}" {submission_query_terms}')
    if "people_gap" in reasons:
        queries.append(f'"{name}" {people_query_terms}')
    if not queries:
        queries.append(f'"{name}" radio contact music submission')
    return list(dict.fromkeys(queries))[:4]


def _paid_source_plan(max_calls: int) -> dict[str, bool]:
    # Brave is always the baseline source because the connector already enforces
    # the monthly quota. Add other paid sources progressively by budget class.
    return {
        "include_google": max_calls >= 3,
        "include_tavily": max_calls >= 2,
        "include_grok": False,
        "include_linkup": max_calls >= 4,
        "include_duckduckgo": max_calls >= 5,
    }


def run_paid_enrichment_scan(session: Session, limit: int = 20, station_id: int | None = None) -> dict[str, Any]:
    if station_id is not None:
        row = _ensure_queue_for_station(session, int(station_id), min_priority=0.0)
        if row is not None:
            row.status = "flagged_for_paid"
            row.updated_at = datetime.utcnow()
        rows = [row] if row is not None else []
    else:
        rows = session.scalars(
            select(EnrichmentQueue)
            .where(
                EnrichmentQueue.status == "flagged_for_paid",
                EnrichmentQueue.api_budget_class.in_(["low", "normal", "high"]),
            )
            .order_by(EnrichmentQueue.priority_score.desc(), EnrichmentQueue.updated_at.asc())
            .limit(max(1, limit))
        ).all()
    summary = {
        "layer": "paid",
        "queue_rows": len(rows),
        "station_id": station_id,
        "processed": 0,
        "done": 0,
        "deferred": 0,
        "errors": 0,
        "api_calls": {"brave": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0},
        "cost_estimate_usd": 0.0,
        "findings": 0,
        "useful_findings": 0,
        "outcomes": {},
    }
    for row in rows:
        station = session.get(Station, int(row.station_id))
        if station is None:
            row.status = "error"
            row.error = "station_missing"
            summary["errors"] += 1
            continue
        reasons = _json_loads(row.reason_codes_json, [])
        max_calls = _api_call_budget(row.api_budget_class)
        run = EnrichmentRun(
            queue_id=row.id,
            station_id=station.id,
            layer="paid",
            source_pool=row.source_pool,
            reason_codes_json=row.reason_codes_json,
        )
        session.add(run)
        session.flush()
        if max_calls <= 0:
            row.status = "deferred"
            row.last_outcome = "no_paid_budget_class"
            row.paid_last_run_at = datetime.utcnow()
            row.last_run_id = run.id
            run.status = "done"
            run.outcome = "no_paid_budget_class"
            run.stop_reason = "no_api_budget"
            run.finished_at = datetime.utcnow()
            summary["deferred"] += 1
            session.commit()
            continue

        api_calls = {"brave": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0}
        findings = 0
        useful = 0
        queries = _build_paid_queries(station, reasons)
        source_plan = _paid_source_plan(max_calls)
        provider_yield = {
            "brave": {"findings": 0, "useful": 0},
            "google_cse": {"findings": 0, "useful": 0},
            "tavily": {"findings": 0, "useful": 0},
            "grok": {"findings": 0, "useful": 0},
            "linkup": {"findings": 0, "useful": 0},
            "duckduckgo": {"findings": 0, "useful": 0},
        }
        errors: list[str] = []
        try:
            for query in queries:
                if sum(api_calls.values()) >= max_calls:
                    break
                results, calls = collect_station_enrichment_results(
                    query=query,
                    country=station.country_code or None,
                    max_results=settings.enrichment_paid_results_per_query,
                    include_google=source_plan["include_google"],
                    include_tavily=source_plan["include_tavily"],
                    include_grok=source_plan["include_grok"],
                    include_linkup=source_plan["include_linkup"],
                    include_duckduckgo=source_plan["include_duckduckgo"],
                )
                for key, value in calls.items():
                    api_calls[key] = api_calls.get(key, 0) + int(value or 0)
                for item in results:
                    url = str(item.get("url") or "")
                    title = str(item.get("title") or "")
                    snippet = str(item.get("description") or "")
                    source_name = str(item.get("source") or "")
                    text_blob = f"{title}\n{snippet}\n{url}"
                    submission_hints = _paid_submission_terms(station)
                    people_hints = tuple(dict.fromkeys(PEOPLE_HINTS + _terms_for_station(station, "people") + _terms_for_station(station, "contact")))
                    finding_type = "external_submission_candidate" if _has_any(text_blob, submission_hints) else "external_people_candidate" if _has_any(text_blob, people_hints) else "external_contact_candidate"
                    is_relevant = _has_station_relevance(station, url, text_blob)
                    if finding_type == "external_submission_candidate":
                        is_useful = is_relevant and _has_strong_submission_signal(url, text_blob, station)
                    elif finding_type == "external_contact_candidate":
                        is_useful = is_relevant and _same_domain(url, station.website_url) and _has_contact_signal(url, text_blob, station)
                    else:
                        is_useful = is_relevant and _has_people_signal(url, text_blob, station)
                    findings += 1
                    useful += int(is_useful)
                    provider_stats = provider_yield.setdefault(source_name, {"findings": 0, "useful": 0})
                    provider_stats["findings"] = int(provider_stats.get("findings", 0)) + 1
                    provider_stats["useful"] = int(provider_stats.get("useful", 0)) + int(is_useful)
                    _record_finding(
                        session,
                        run,
                        int(station.id),
                        finding_type,
                        url,
                        title[:500],
                        snippet[:800],
                        0.65 if is_useful else 0.35,
                        is_useful,
                        source_name=source_name,
                        payload={"query": query, "source": source_name},
                    )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")

        cost = round(sum(API_CALL_ESTIMATE_USD.get(k, 0.0) * v for k, v in api_calls.items()), 6)
        if useful > 0:
            outcome = "paid_findings_recorded"
            row.status = "done"
            summary["done"] += 1
        elif errors:
            outcome = "paid_error"
            row.status = "error"
            row.error = "; ".join(errors[:3])
            summary["errors"] += 1
        else:
            outcome = "paid_no_new_info"
            row.status = "deferred"
            summary["deferred"] += 1
        row.last_outcome = outcome
        row.paid_last_run_at = datetime.utcnow()
        row.last_run_id = run.id
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.paid_plan_json = _json_dumps(
            {
                "queries": queries,
                "max_api_calls": max_calls,
                "api_calls": api_calls,
                "provider_yield": provider_yield,
                "policy": _classify_station_policy(station),
                "language_keys": _station_language_keys(station),
            }
        )
        row.updated_at = datetime.utcnow()
        run.status = "done" if not errors or useful else "error"
        run.outcome = outcome
        run.stop_reason = "api_budget_exhausted" if sum(api_calls.values()) >= max_calls else "paid_pass_complete"
        run.findings_count = findings
        run.useful_findings_count = useful
        run.api_calls_json = _json_dumps(api_calls)
        run.cost_estimate_usd = cost
        run.metrics_json = _json_dumps(
            {
                "queries": queries,
                "errors": errors[:8],
                "api_budget_class": row.api_budget_class,
                "max_api_calls": max_calls,
                "source_plan": source_plan,
                "provider_yield": provider_yield,
                "policy": _classify_station_policy(station),
                "language_keys": _station_language_keys(station),
            }
        )
        run.error = row.error
        run.finished_at = datetime.utcnow()
        summary["processed"] += 1
        summary["findings"] += findings
        summary["useful_findings"] += useful
        summary["cost_estimate_usd"] = round(float(summary["cost_estimate_usd"]) + cost, 6)
        for key, value in api_calls.items():
            summary["api_calls"][key] = int(summary["api_calls"].get(key, 0)) + int(value or 0)
        summary["outcomes"][outcome] = int(summary["outcomes"].get(outcome, 0)) + 1
        session.commit()
    return summary


def evaluate_enrichment_golden_set(
    session: Session,
    config_path: str = "config/enrichment_golden_set.json",
    limit: int | None = None,
    run_paid: bool = False,
    max_urls_per_station: int = 6,
) -> dict[str, Any]:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    cases = json.loads(path.read_text(encoding="utf-8"))
    if limit is not None:
        cases = cases[: max(0, limit)]

    started_at = datetime.utcnow()
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    for case in cases:
        station_id = int(case["station_id"])
        station = session.get(Station, station_id)
        if station is None:
            failed += 1
            results.append({"station_id": station_id, "label": case.get("label"), "passed": False, "errors": ["station_missing"]})
            continue

        free_summary = run_free_enrichment_scan(
            session=session,
            limit=1,
            max_urls_per_station=max_urls_per_station,
            station_id=station_id,
        )
        paid_summary = None
        if run_paid:
            paid_summary = run_paid_enrichment_scan(session=session, limit=1, station_id=station_id)

        findings = session.execute(
            select(EnrichmentFinding.layer, EnrichmentFinding.finding_type, EnrichmentFinding.useful, func.count(EnrichmentFinding.id))
            .where(
                EnrichmentFinding.station_id == station_id,
                EnrichmentFinding.created_at >= started_at,
            )
            .group_by(EnrichmentFinding.layer, EnrichmentFinding.finding_type, EnrichmentFinding.useful)
        ).all()
        finding_rows = [
            {"layer": layer, "finding_type": ftype, "useful": bool(useful), "n": int(n)}
            for layer, ftype, useful, n in findings
        ]
        free_useful = sum(row["n"] for row in finding_rows if row["layer"] == "free" and row["useful"])
        paid_useful = sum(row["n"] for row in finding_rows if row["layer"] == "paid" and row["useful"])
        observed_types = {row["finding_type"] for row in finding_rows if row["n"] > 0}
        policy = _classify_station_policy(station)
        language_keys = _station_language_keys(station)
        errors: list[str] = []
        if free_useful < int(case.get("expect_free_min", 0)):
            errors.append(f"free_useful_lt_{case.get('expect_free_min')}")
        if paid_useful < int(case.get("expect_paid_min", 0)):
            errors.append(f"paid_useful_lt_{case.get('expect_paid_min')}")
        expected_policy = case.get("expect_policy")
        if expected_policy and policy != expected_policy:
            errors.append(f"policy_expected_{expected_policy}_got_{policy}")
        expected_language = case.get("expect_language")
        if expected_language and expected_language not in language_keys:
            errors.append(f"language_expected_{expected_language}_got_{','.join(language_keys)}")
        for expected_type in case.get("expect_any_types", []):
            if not any(expected_type in observed for observed in observed_types):
                errors.append(f"missing_type_{expected_type}")

        case_passed = not errors
        passed += int(case_passed)
        failed += int(not case_passed)
        results.append(
            {
                "station_id": station_id,
                "label": case.get("label") or station.canonical_name,
                "passed": case_passed,
                "errors": errors,
                "policy": policy,
                "language_keys": language_keys,
                "free_useful": free_useful,
                "paid_useful": paid_useful,
                "free_summary": free_summary,
                "paid_summary": paid_summary,
                "finding_rows": finding_rows,
            }
        )

    return {
        "config_path": str(path),
        "cases": len(cases),
        "passed": passed,
        "failed": failed,
        "run_paid": run_paid,
        "max_urls_per_station": max_urls_per_station,
        "results": results,
    }


def enrichment_queue_stats(session: Session) -> dict[str, Any]:
    by_status = [
        {"status": status or "", "source_pool": pool or "", "api_budget_class": budget or "", "n": int(n)}
        for status, pool, budget, n in session.execute(
            select(EnrichmentQueue.status, EnrichmentQueue.source_pool, EnrichmentQueue.api_budget_class, func.count(EnrichmentQueue.id))
            .group_by(EnrichmentQueue.status, EnrichmentQueue.source_pool, EnrichmentQueue.api_budget_class)
            .order_by(func.count(EnrichmentQueue.id).desc())
        ).all()
    ]
    recent_runs = [
        {
            "layer": layer or "",
            "status": status or "",
            "outcome": outcome or "",
            "n": int(n),
            "useful_findings": int(useful or 0),
            "api_calls": api_calls or "{}",
            "cost_estimate_usd": round(float(cost or 0.0), 6),
        }
        for layer, status, outcome, n, useful, api_calls, cost in session.execute(
            select(
                EnrichmentRun.layer,
                EnrichmentRun.status,
                EnrichmentRun.outcome,
                func.count(EnrichmentRun.id),
                func.sum(EnrichmentRun.useful_findings_count),
                func.max(EnrichmentRun.api_calls_json),
                func.sum(EnrichmentRun.cost_estimate_usd),
            )
            .where(EnrichmentRun.started_at >= datetime.utcnow() - timedelta(days=1))
            .group_by(EnrichmentRun.layer, EnrichmentRun.status, EnrichmentRun.outcome)
            .order_by(func.count(EnrichmentRun.id).desc())
        ).all()
    ]
    finding_types = [
        {"layer": layer or "", "finding_type": ftype or "", "useful": bool(useful), "n": int(n)}
        for layer, ftype, useful, n in session.execute(
            select(EnrichmentFinding.layer, EnrichmentFinding.finding_type, EnrichmentFinding.useful, func.count(EnrichmentFinding.id))
            .where(EnrichmentFinding.created_at >= datetime.utcnow() - timedelta(days=1))
            .group_by(EnrichmentFinding.layer, EnrichmentFinding.finding_type, EnrichmentFinding.useful)
            .order_by(func.count(EnrichmentFinding.id).desc())
        ).all()
    ]
    return {
        "queue_total": int(session.scalar(select(func.count(EnrichmentQueue.id))) or 0),
        "runs_24h": int(session.scalar(select(func.count(EnrichmentRun.id)).where(EnrichmentRun.started_at >= datetime.utcnow() - timedelta(days=1))) or 0),
        "findings_24h": int(session.scalar(select(func.count(EnrichmentFinding.id)).where(EnrichmentFinding.created_at >= datetime.utcnow() - timedelta(days=1))) or 0),
        "by_status": by_status,
        "recent_runs": recent_runs,
        "finding_types_24h": finding_types,
    }

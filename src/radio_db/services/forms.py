from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from openai import OpenAI
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    Evidence,
    FormRecipe,
    FormStatus,
    FormType,
    SourceType,
    Station,
    StationGenre,
    StationStatus,
    SubmissionChannel,
    SubmissionForm,
    SubmissionFormField,
    SubmissionMethod,
)
from radio_db.services.budget import CostGuard

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None


FORM_KEYWORDS = (
    "submit",
    "submission",
    "send us your music",
    "new artist",
    "demo",
    "playlist",
    "promo",
)

FORM_PATH_HINTS = (
    "/submit",
    "/submission",
    "/submit-music",
    "/music-submission",
    "/contact",
    "/demo",
    "/playlist",
)
DEEP_PATH_HINTS = (
    "/kontakt",
    "/contact-us",
    "/music",
    "/music-submissions",
    "/playlist-submission",
    "/playlists",
    "/shows",
    "/program",
    "/programs",
    "/artists",
    "/soundpark",
    "/demo",
)
DEEP_SUBMISSION_HINTS = (
    "submit",
    "submission",
    "playlist",
    "new artist",
    "unsigned",
    "demo",
    "music",
    "track",
    "show",
    "program",
    "kontakt",
    "contact",
    "soundpark",
    "airplay",
    "editorial",
)
NON_PAGE_URL_SUFFIXES = (
    ".pdf",
    ".zip",
    ".mp3",
    ".mp4",
    ".wav",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
)
STRONG_SUBMISSION_CONTEXT_HINTS = (
    "submit",
    "submission",
    "send us your music",
    "music submission",
    "submit music",
    "new artist",
    "unsigned",
    "demo",
    "airplay",
    "playlist submission",
    "music director",
    "program director",
)
DEEP_EXCLUDE_HINTS = (
    "privacy",
    "impressum",
    "terms",
    "cookie",
    "login",
    "account",
    "shop",
    "advert",
    "sponsor",
    "career",
)
EMAIL_REGEX = re.compile(r"\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b", flags=re.I)
OBFUSCATED_EMAIL_REGEX = re.compile(
    r"\b([a-z0-9._%+\-]+)\s*(?:@|\[at\]|\(at\)|\sat\s)\s*([a-z0-9.\-]+\.[a-z]{2,})\b",
    flags=re.I,
)
INVALID_DISCOVERED_EMAIL_TLDS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "svg",
    "webp",
    "ico",
    "css",
    "js",
    "mp3",
    "mp4",
    "wav",
    "pdf",
}
INVALID_DISCOVERED_EMAILS = {
    "button@click.debounce",
    "email@example.com",
    "input@input.debounce",
    "x-ignore@submit.prevent",
}
INVALID_DISCOVERED_EMAIL_DOMAINS = {
    "example.com",
    "input.debounce",
    "submit.prevent",
}
UNSUPPORTED_DOMAIN_HINTS = (
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "linkedin.com",
    "tiktok.com",
    "iheart.com",
    "mytuner-radio.com",
    "music.apple.com",
    "siriusxm.com",
    "tunein.com",
    "radio.net",
    "onlineradiobox.com",
    "streema.com",
    "live365.com",
    "accuradio.com",
    "mixrecordingstudio.com",
    "radiofidelity.com",
    "radioguide.fm",
    "wikipedia.org",
    "internet-radio.com",
    "smoothjazz.com",
    "apps.apple.com",
    "amazon.",
    "musicgateway.com",
    "groover.co",
    "omarimc.com",
    "musconv.com",
    "radioindiealliance.com",
    "songstuff.com",
    "thecraftymusician.com",
    "kiremico.com",
    "musicinafrica.net",
    "blog.delivermytune.com",
    "sharetopros.com",
    "now-hear-this.net",
    "talentcast.nl",
    "indiexl.nl",
    "chairsandmore.nl",
)
UNSUPPORTED_URL_PATH_HINTS = (
    "/genre/",
    "/city/",
    "/country/",
    "/live/country/",
    "/r/",
    "/forums/",
    "/forum/",
    "/blog/",
    "/how-to-submit",
    "/how-to/",
    "/how-to-submit-your-music",
    "/how-to-submit-music",
    "/submit-your-music-to-radio",
    "/send-your-music-to-radio",
    "/radio-stations-that-accept",
)
UNSUPPORTED_URL_FILE_EXTENSIONS = (
    ".aac",
    ".flac",
    ".m3u",
    ".m3u8",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pls",
    ".wav",
)
UNSUPPORTED_STREAM_URL_HINTS = (
    "/stream",
    "stream.",
    "stream-",
    "stream?",
    "icecast",
    "shoutcast",
    "zeno.fm/",
    "netradyom.com:",
)
UNSUPPORTED_STATION_NAME_HINTS = (
    "listen to ",
    "top ",
    "best ",
    "list of ",
    " radio stations",
    " stations from ",
    " on reddit",
    " - wikipedia",
    "stazioni radio",
)
UNSUPPORTED_STATION_NAME_CONTAINS = (
    "how to submit",
    "submit music to",
    "submit your music to",
    "send your music to",
    "how to send your music",
    "how to get your music played",
    "radio stations that accept",
    "online radio stations to submit",
    "best sites to submit music",
    "step-by-step guide for artists",
    "a simple guide for artists",
    "old channel, new opportunities",
    "music curators",
    "music promotion",
    "one submit",
    "reach 2,200",
)
ENTRYPOINT_ONLY_NAME_EXACT = (
    "music submissions",
    "radio submit",
    "submit music",
)
ENTRYPOINT_ONLY_URL_HINTS = (
    "/music-submissions",
    "/music-submission",
    "/submit-music",
    "/radio-submission",
)

FIELD_MAPPING_HINTS = {
    "artist_name": ("artist", "band", "act", "performer"),
    "release_title": ("release", "song", "track", "title"),
    "contact_email": ("email", "mail"),
    "streaming_link": ("spotify", "youtube", "soundcloud", "link", "url", "stream"),
    "bio_short": ("bio", "about", "description", "artist info"),
    "press_photo": ("photo", "image", "press"),
    "audio_file": ("audio", "upload", "file", "mp3", "wav"),
}


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def is_supported_station_target_url(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    domain = _domain(url)
    if not domain:
        return False
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if any(path.endswith(ext) for ext in UNSUPPORTED_URL_FILE_EXTENSIONS):
        return False
    if any(hint in lowered for hint in UNSUPPORTED_STREAM_URL_HINTS):
        return False
    if any(hint in domain for hint in UNSUPPORTED_DOMAIN_HINTS):
        return False
    if any(hint in lowered for hint in UNSUPPORTED_URL_PATH_HINTS):
        return False
    return True


def _focus_keywords() -> list[str]:
    raw = settings.submission_excluded_focus_keywords or ""
    keywords = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return keywords


def station_matches_excluded_focus(
    station: Station,
    keywords: list[str] | None = None,
    include_genres: bool = False,
) -> bool:
    focus_keywords = keywords or _focus_keywords()
    if not focus_keywords:
        return False
    name = (station.canonical_name or "").lower()
    if any(kw in name for kw in focus_keywords):
        return True
    if include_genres:
        for genre_row in (station.genres or []):
            genre = (genre_row.genre or "").lower()
            if any(kw in genre for kw in focus_keywords):
                return True
    return False


def station_matches_excluded_meta(station: Station) -> bool:
    name = (station.canonical_name or "").lower().strip()
    website = (station.website_url or "").lower().strip()
    domain = _domain(station.website_url)
    if any(hint in domain for hint in UNSUPPORTED_DOMAIN_HINTS):
        return True
    if any(hint in website for hint in UNSUPPORTED_URL_PATH_HINTS):
        return True
    if any(name.startswith(prefix) for prefix in UNSUPPORTED_STATION_NAME_HINTS):
        return True
    if any(token in name for token in UNSUPPORTED_STATION_NAME_CONTAINS):
        return True
    if "submission" in name and ("accept" in name or "stations" in name):
        return True
    return False


def station_matches_entrypoint_only(station: Station) -> bool:
    name = (station.canonical_name or "").lower().strip()
    website = (station.website_url or "").lower().strip()
    if name in ENTRYPOINT_ONLY_NAME_EXACT and any(h in website for h in ENTRYPOINT_ONLY_URL_HINTS):
        return True
    if name == "music submissions" and website:
        return True
    return False


def cleanup_stations_by_focus_keywords(
    session: Session,
    keywords: list[str] | None = None,
    limit: int = 50000,
    dry_run: bool = True,
    include_genres: bool = False,
) -> dict:
    focus_keywords = [kw.strip().lower() for kw in (keywords or _focus_keywords()) if kw.strip()]
    if not focus_keywords:
        return {"keywords": [], "candidates": 0, "rejected": 0, "dry_run": dry_run}

    like_clauses = [Station.canonical_name.ilike(f"%{kw}%") for kw in focus_keywords]
    genre_exists_clauses = []
    if include_genres:
        genre_exists_clauses = [
            exists(
                select(1).where(
                    StationGenre.station_id == Station.id,
                    StationGenre.genre.ilike(f"%{kw}%"),
                )
            )
            for kw in focus_keywords
        ]
    stations = session.scalars(
        select(Station)
        .where(
            Station.status != StationStatus.REJECTED,
            or_(*like_clauses, *genre_exists_clauses),
        )
        .order_by(Station.id.asc())
        .limit(max(1, limit))
    ).all()
    candidates = len(stations)
    rejected = 0
    sample: list[dict] = []
    for st in stations:
        if len(sample) < 30:
            sample.append({"station_id": st.id, "name": st.canonical_name, "website": st.website_url})
        if dry_run:
            continue
        st.status = StationStatus.REJECTED
        st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
        rejected += 1
    if not dry_run:
        session.commit()
    return {
        "keywords": focus_keywords,
        "include_genres": include_genres,
        "candidates": candidates,
        "rejected": rejected,
        "dry_run": dry_run,
        "sample": sample,
    }


def cleanup_stations_by_meta_rules(
    session: Session,
    limit: int = 50000,
    dry_run: bool = True,
) -> dict:
    stations = session.scalars(
        select(Station)
        .where(Station.status != StationStatus.REJECTED)
        .order_by(Station.id.asc())
        .limit(max(1, limit))
    ).all()
    targets = [st for st in stations if station_matches_excluded_meta(st)]
    sample = [
        {"station_id": st.id, "name": st.canonical_name, "website": st.website_url}
        for st in targets[:30]
    ]
    if not dry_run:
        for st in targets:
            st.status = StationStatus.REJECTED
            st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
        session.commit()
    return {
        "candidates_scanned": len(stations),
        "meta_matches": len(targets),
        "rejected": len(targets) if not dry_run else 0,
        "dry_run": dry_run,
        "sample": sample,
    }


def cleanup_stations_by_entrypoint_rules(
    session: Session,
    limit: int = 50000,
    dry_run: bool = True,
) -> dict:
    stations = session.scalars(
        select(Station)
        .where(Station.status != StationStatus.REJECTED)
        .order_by(Station.id.asc())
        .limit(max(1, limit))
    ).all()
    targets = [st for st in stations if station_matches_entrypoint_only(st)]
    sample = [
        {"station_id": st.id, "name": st.canonical_name, "website": st.website_url}
        for st in targets[:30]
    ]
    if not dry_run:
        for st in targets:
            st.status = StationStatus.REJECTED
            st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
        session.commit()
    return {
        "candidates_scanned": len(stations),
        "entrypoint_matches": len(targets),
        "rejected": len(targets) if not dry_run else 0,
        "dry_run": dry_run,
        "sample": sample,
    }


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _clean_html_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _compact_html_text(value: str) -> str:
    return _clean_html_text(value)[:200000]


def _snippet_contexts(text: str, needles: tuple[str, ...], radius: int = 180, limit: int = 8) -> list[dict]:
    hay = _compact_html_text(text)
    lowered = hay.lower()
    out: list[dict] = []
    seen: set[str] = set()
    for needle in needles:
        idx = lowered.find(needle)
        if idx < 0:
            continue
        start = max(0, idx - radius)
        end = min(len(hay), idx + len(needle) + radius)
        snippet = hay[start:end].strip()
        key = f"{needle}:{snippet}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"keyword": needle, "snippet": snippet[:420]})
        if len(out) >= limit:
            break
    return out


def _email_contexts(text: str, emails: list[str], radius: int = 180, limit: int = 10) -> list[dict]:
    hay = _compact_html_text(text)
    lowered = hay.lower()
    out: list[dict] = []
    seen: set[str] = set()
    for email in emails:
        email_lower = email.lower()
        idx = lowered.find(email_lower)
        if idx < 0:
            continue
        start = max(0, idx - radius)
        end = min(len(hay), idx + len(email_lower) + radius)
        snippet = hay[start:end].strip()
        key = f"{email_lower}:{snippet}"
        if key in seen:
            continue
        seen.add(key)
        snippet_lower = snippet.lower()
        out.append(
            {
                "email": email_lower,
                "snippet": snippet[:420],
                "near_submission_signal": any(h in snippet_lower for h in STRONG_SUBMISSION_CONTEXT_HINTS),
            }
        )
        if len(out) >= limit:
            break
    return out


def _extract_emails(text: str) -> list[str]:
    blocked_prefixes = ("noreply@", "donotreply@", "no-reply@")
    seen: set[str] = set()
    out: list[str] = []
    raw = (text or "").lower()
    candidates = list(EMAIL_REGEX.findall(raw)) + [f"{local}@{domain}" for local, domain in OBFUSCATED_EMAIL_REGEX.findall(raw)]
    for match in candidates:
        if match.startswith(blocked_prefixes):
            continue
        if not _is_valid_discovered_email(match):
            continue
        if match in seen:
            continue
        seen.add(match)
        out.append(match)
    return out[:25]


def _is_valid_discovered_email(email: str) -> bool:
    value = str(email or "").strip().lower().rstrip(".,;:)]}>")
    if value in INVALID_DISCOVERED_EMAILS:
        return False
    if "@" not in value:
        return False
    local, domain = value.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    if domain in INVALID_DISCOVERED_EMAIL_DOMAINS:
        return False
    if "/" in value or "\\" in value:
        return False
    if re.fullmatch(r"u[0-9a-f]{3,}", local):
        return False
    tld = domain.rsplit(".", 1)[-1]
    if tld in INVALID_DISCOVERED_EMAIL_TLDS:
        return False
    return True


def _extract_links_from_html(html: str, base_url: str, station_domain: str) -> list[tuple[str, str, float]]:
    out: list[tuple[str, str, float]] = []
    for href, label_html in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, flags=re.I | re.S):
        href = (href or "").strip()
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _is_http_url(url):
            continue
        if _domain(url) != station_domain:
            continue
        lowered = f"{url} {_clean_html_text(label_html)}".lower()
        score = 0.0
        if any(h in lowered for h in DEEP_SUBMISSION_HINTS):
            score += 4.0
        if any(h in lowered for h in ("music", "musik", "musique", "musica", "programming", "playlist")):
            score += 2.0
        if any(h in lowered for h in ("team", "staff", "host", "presenter", "dj", "music director", "program director")):
            score += 1.6
        if any(h in lowered for h in DEEP_EXCLUDE_HINTS):
            score -= 2.5
        if "/submit" in lowered or "submission" in lowered:
            score += 3.0
        if any(path in lowered for path in ("/contact", "/kontakt", "/contato", "/contacto", "/team", "/staff")):
            score += 1.8
        if any(path in lowered for path in ("/privacy", "/terms", "/advertis", "/shop", "/events", "/podcast")):
            score -= 2.0
        depth = len([p for p in urlparse(url).path.split("/") if p])
        score -= min(2.0, max(0, depth - 4) * 0.35)
        out.append((url, _clean_html_text(label_html), score))
    out.sort(key=lambda item: item[2], reverse=True)
    return out


def _safe_key(value: str, idx: int) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return base or f"field_{idx}"


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * settings.codex_input_price_per_1m + (
        output_tokens / 1_000_000
    ) * settings.codex_output_price_per_1m


def _priority_countries() -> list[str]:
    raw = settings.priority_countries or ""
    out = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return out or ["US", "GB", "DE", "FR"]


def _country_cycle_state_path() -> Path:
    return Path(".radio_db_state") / "country_cycle_state.json"


def _load_country_cycle_state() -> dict:
    path = _country_cycle_state_path()
    if not path.exists():
        return {"index": 0, "countries": _priority_countries(), "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        countries = [str(x).upper() for x in payload.get("countries", []) if str(x).strip()]
        if not countries:
            countries = _priority_countries()
        return {
            "index": int(payload.get("index", 0)) % max(1, len(countries)),
            "countries": countries,
            "updated_at": str(payload.get("updated_at", datetime.now(timezone.utc).isoformat())),
        }
    except Exception:
        return {"index": 0, "countries": _priority_countries(), "updated_at": datetime.now(timezone.utc).isoformat()}


def _save_country_cycle_state(state: dict) -> None:
    path = _country_cycle_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _candidate_urls_for_station(
    station: Station,
    max_urls: int,
    include_deep_pass: bool | None = None,
) -> list[tuple[str, list[str]]]:
    seen: set[str] = set()
    candidates: list[tuple[str, list[str]]] = []

    def add(url: str | None, path: list[str]) -> None:
        if not url:
            return
        u = url.strip()
        if not u or u in seen:
            return
        if not _is_http_url(u):
            return
        parsed = urlparse(u)
        if any(parsed.path.lower().endswith(suffix) for suffix in NON_PAGE_URL_SUFFIXES):
            return
        if _domain(u) and station.website_url and _domain(u) != _domain(station.website_url):
            # Keep off-domain URLs only if explicitly stored as form channels.
            if path and path[0] != "submission_channel":
                return
        seen.add(u)
        candidates.append((u, path))

    for ch in station.submissions:
        if ch.method in (SubmissionMethod.FORM, SubmissionMethod.PORTAL) and ch.url:
            add(ch.url, ["submission_channel", "stored_url"])

    if station.website_url:
        add(station.website_url, ["homepage"])
        for hint in FORM_PATH_HINTS:
            add(urljoin(station.website_url, hint), ["homepage", hint.strip("/") or "root"])

    deep_enabled = settings.browser_enable_deep_pass if include_deep_pass is None else include_deep_pass
    if deep_enabled and station.website_url and len(candidates) < max(1, max_urls):
        for hint in DEEP_PATH_HINTS:
            add(urljoin(station.website_url, hint), ["deep_hint", hint.strip("/") or "root"])
        station_domain = _domain(station.website_url)
        if station_domain:
            try:
                homepage_html = get_text(station.website_url)
                ranked_links = _extract_links_from_html(
                    homepage_html,
                    base_url=station.website_url,
                    station_domain=station_domain,
                )
                for url, label, score in ranked_links:
                    if score <= 0:
                        continue
                    add(url, ["ranked_homepage_link", f"{score:.2f}", label[:120] or "anchor"])
                    if len(candidates) >= max(1, max_urls):
                        break
                if len(candidates) < max(1, max_urls):
                    for sitemap_url in (urljoin(station.website_url, "/sitemap.xml"),):
                        try:
                            sitemap_text = get_text(sitemap_url)
                        except Exception:
                            continue
                        sitemap_links = [
                            m.group(1)
                            for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_text, flags=re.I)
                        ]
                        scored_sitemap: list[tuple[float, str]] = []
                        for sitemap_link in sitemap_links[:1000]:
                            if _domain(sitemap_link) != station_domain:
                                continue
                            hay = sitemap_link.lower()
                            score = 0.0
                            if any(h in hay for h in DEEP_SUBMISSION_HINTS):
                                score += 4.0
                            if any(h in hay for h in ("contact", "team", "staff", "music", "playlist", "program")):
                                score += 2.0
                            if any(h in hay for h in DEEP_EXCLUDE_HINTS):
                                score -= 2.0
                            if score > 0:
                                scored_sitemap.append((score, sitemap_link))
                        for score, sitemap_link in sorted(scored_sitemap, reverse=True):
                            add(sitemap_link, ["sitemap", f"{score:.2f}"])
                            if len(candidates) >= max(1, max_urls):
                                break
            except Exception:
                pass

    return candidates[: max(1, max_urls)]


def _persist_email_channels(session: Session, station: Station, source_url: str, emails: list[str]) -> int:
    saved = 0
    for email in emails:
        if not _is_valid_discovered_email(email):
            continue
        existing = session.scalar(
            select(SubmissionChannel).where(
                SubmissionChannel.station_id == station.id,
                SubmissionChannel.email == email,
            )
        )
        if existing is not None:
            continue
        session.add(
            SubmissionChannel(
                station_id=station.id,
                method=SubmissionMethod.EMAIL,
                email=email,
                url=None,
                requirements="auto_discovered_from_page",
                accepts_newcomers=False,
            )
        )
        saved += 1
    return saved


def _classify_form_type(url: str, title: str, fields: list[dict]) -> FormType:
    field_text = " ".join(
        " ".join(
            str(f.get(key) or "")
            for key in ("label", "name", "placeholder", "key_seed", "accept_types")
        )
        for f in fields
    )
    page_text = f"{title} {field_text}".lower()
    url_lower = (url or "").lower()
    combined = f"{url_lower} {page_text}"
    strong_music_page = any(
        hint in combined
        for hint in (
            "submit music",
            "music submission",
            "music-submission",
            "music-submissions",
            "submit-music",
            "demo submission",
            "playlist submission",
            "new artist",
            "unsigned",
            "airplay",
        )
    )
    if any(k in combined for k in ("newcomer", "unsigned", "emerging artist")):
        return FormType.NEWCOMER
    if any(k in combined for k in ("show pitch", "program pitch", "host pitch")):
        return FormType.SHOW_PITCH
    if strong_music_page and any(k in combined for k in ("upload", "mp3", "wav", "track file", "audio file")):
        return FormType.ARTIST_UPLOAD
    if strong_music_page:
        return FormType.MUSIC_SUBMISSION
    if any(k in page_text for k in ("contact", "message", "your name", "email")):
        return FormType.GENERAL_CONTACT
    return FormType.UNKNOWN


def _heuristic_recipe(fields: list[dict]) -> dict:
    mapping: dict[str, str] = {}
    required_fields = [f["field_key"] for f in fields if f.get("required")]
    field_order = [f["field_key"] for f in fields]
    upload_rules: dict[str, str] = {}
    validation_rules: list[str] = []

    for f in fields:
        key = f["field_key"]
        label = f"{f.get('label', '')} {f.get('name', '')}".lower()
        for logical_key, hints in FIELD_MAPPING_HINTS.items():
            if any(h in label for h in hints) and logical_key not in mapping:
                mapping[logical_key] = key
                break
        if f.get("input_type") == "file":
            upload_rules[key] = f.get("accept_types") or "file"
        if f.get("validation_hint"):
            validation_rules.append(f"{key}: {f.get('validation_hint')}")

    return {
        "required_fields": required_fields,
        "mapping_rules": mapping,
        "field_order": field_order,
        "upload_rules": upload_rules,
        "validation_rules": validation_rules,
        "success_detection": ["thank you", "successfully submitted", "submission received"],
        "error_detection": ["required field", "invalid", "captcha", "error"],
        "notes_for_future_runs": (
            "Standard mode: zuerst Pflichtfelder ausfuellen, Upload-Felder zuletzt setzen, "
            "kein finaler Submit ohne Controlled-Submit-Freigabe."
        ),
    }


def _codex_enrich_recipe(fields: list[dict], base_recipe: dict, form_url: str) -> dict:
    if not settings.enable_codex_recipe_enrichment or not settings.openai_api_key:
        return base_recipe

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "codex_recipe_budget.json",
        max_daily_usd=settings.max_codex_daily_usd,
        max_llm_calls_per_day=settings.max_codex_calls_per_day,
        input_price_per_1m=settings.codex_input_price_per_1m,
        output_price_per_1m=settings.codex_output_price_per_1m,
    )
    prompt = json.dumps({"form_url": form_url, "fields": fields, "base_recipe": base_recipe}, ensure_ascii=False)
    estimated_input = CostGuard.estimate_input_tokens(prompt)
    estimated_cost = _estimate_cost(estimated_input, settings.llm_estimated_output_tokens)
    if not budget.can_call_llm_today() or not budget.can_spend(estimated_cost):
        return base_recipe

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    schema = {
        "type": "object",
        "properties": {
            "instructions_text": {"type": "string"},
            "mapping_rules": {"type": "object", "additionalProperties": {"type": "string"}},
            "success_detection": {"type": "array", "items": {"type": "string"}},
            "error_detection": {"type": "array", "items": {"type": "string"}},
            "manual_checks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["instructions_text", "mapping_rules", "success_detection", "error_detection", "manual_checks"],
        "additionalProperties": False,
    }
    try:
        response = client.responses.create(
            model=settings.codex_recipe_model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Du bist ein Formular-Automations-Agent. Liefere nur JSON. "
                        "Kein finales Submitting empfehlen, nur sichere Vorschlaege."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            text={"format": {"type": "json_schema", "name": "form_recipe_patch", "schema": schema, "strict": True}},
        )
        patch = json.loads(response.output_text)
        budget.register_call(estimated_cost)
        merged = dict(base_recipe)
        merged["mapping_rules"] = patch.get("mapping_rules") or base_recipe["mapping_rules"]
        merged["success_detection"] = patch.get("success_detection") or base_recipe["success_detection"]
        merged["error_detection"] = patch.get("error_detection") or base_recipe["error_detection"]
        merged["notes_for_future_runs"] = (
            (patch.get("instructions_text") or "").strip()
            + "\n\nManual checks:\n- "
            + "\n- ".join(patch.get("manual_checks") or [])
        ).strip()
        return merged
    except Exception:
        return base_recipe


def _extract_forms_playwright(url: str) -> dict:
    if sync_playwright is None:
        raise RuntimeError("playwright_not_installed")

    snapshot_root = Path(settings.browser_snapshot_dir)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    shot_path = snapshot_root / f"tmp_{timestamp}.png"
    html_path = snapshot_root / f"tmp_{timestamp}.html"
    snapshot_root.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        launch_kwargs: dict = {"headless": settings.browser_headless}
        if settings.browser_proxy_url:
            launch_kwargs["proxy"] = {"server": settings.browser_proxy_url}
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)
        page.goto(url, wait_until="domcontentloaded")
        page.screenshot(path=str(shot_path), full_page=True)
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
        final_url = page.url or url
        title = page.title() or ""
        lang = page.eval_on_selector("html", "el => el.lang || ''") or ""
        has_login = bool(page.locator('input[type="password"]').count())
        has_captcha = bool(page.locator('iframe[src*=\"captcha\"], [id*=\"captcha\"], [class*=\"captcha\"]').count())

        forms = page.evaluate(
            """
            () => {
              const out = [];
              const forms = Array.from(document.querySelectorAll("form"));
              for (let idx = 0; idx < forms.length; idx++) {
                const form = forms[idx];
                const fields = [];
                const els = Array.from(form.querySelectorAll("input, textarea, select"));
                for (let i = 0; i < els.length; i++) {
                  const el = els[i];
                  const tag = (el.tagName || "").toLowerCase();
                  const rawType = (el.getAttribute("type") || "").toLowerCase();
                  const type = tag === "select" ? "select" : (tag === "textarea" ? "textarea" : (rawType || "text"));
                  if (["submit", "button", "image", "reset"].includes(type)) continue;
                  const labels = el.labels && el.labels.length ? Array.from(el.labels).map(x => (x.innerText || "").trim()).filter(Boolean) : [];
                  const options = tag === "select"
                    ? Array.from(el.querySelectorAll("option")).map(o => (o.textContent || "").trim()).filter(Boolean).slice(0, 50)
                    : [];
                  const hint = [
                    el.getAttribute("pattern") ? `pattern=${el.getAttribute("pattern")}` : "",
                    el.getAttribute("minlength") ? `minlength=${el.getAttribute("minlength")}` : "",
                    el.getAttribute("maxlength") ? `maxlength=${el.getAttribute("maxlength")}` : ""
                  ].filter(Boolean).join("; ");
                  fields.push({
                    key_seed: el.getAttribute("name") || el.getAttribute("id") || `field_${idx}_${i}`,
                    name: el.getAttribute("name") || "",
                    label: (labels[0] || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "").trim(),
                    input_type: type,
                    required: !!(el.required || el.getAttribute("aria-required") === "true"),
                    placeholder: el.getAttribute("placeholder") || "",
                    options: options,
                    validation_hint: hint || "",
                    max_length: el.maxLength > 0 ? el.maxLength : null,
                    accept_types: el.getAttribute("accept") || "",
                  });
                }
                out.push({
                  index: idx,
                  action: form.getAttribute("action") || "",
                  method: (form.getAttribute("method") || "get").toLowerCase(),
                  fields,
                });
              }
              return out;
            }
            """
        )
        ranked_links = _extract_links_from_html(html, base_url=url, station_domain=_domain(url))
        emails = _extract_emails(html)
        submission_contexts = _snippet_contexts(html, DEEP_SUBMISSION_HINTS)
        email_context_samples = _email_contexts(html, emails)

        browser.close()
        return {
            "url": final_url,
            "requested_url": url,
            "title": title,
            "language": lang,
            "requires_login": has_login,
            "has_captcha": has_captcha,
            "forms": forms,
            "links": [
                {"href": href, "text": text, "score": round(float(score), 3)}
                for href, text, score in ranked_links[:300]
            ],
            "emails": emails,
            "email_contexts": email_context_samples,
            "submission_keyword_contexts": submission_contexts,
            "snapshot_path": str(shot_path),
            "dom_snapshot_path": str(html_path),
        }


def _extract_forms_fallback(url: str) -> dict:
    html = get_text(url)
    lowered = html.lower()
    form_count = lowered.count("<form")
    fields: list[dict] = []
    for idx, match in enumerate(re.finditer(r"<input[^>]+>", html, flags=re.I)):
        chunk = match.group(0)
        name_match = re.search(r'name=["\']([^"\']+)["\']', chunk, flags=re.I)
        type_match = re.search(r'type=["\']([^"\']+)["\']', chunk, flags=re.I)
        placeholder_match = re.search(r'placeholder=["\']([^"\']+)["\']', chunk, flags=re.I)
        required = bool(re.search(r"\srequired(\s|>|=)", chunk, flags=re.I))
        raw_name = name_match.group(1) if name_match else f"field_{idx}"
        fields.append(
            {
                "key_seed": raw_name,
                "name": raw_name,
                "label": raw_name,
                "input_type": (type_match.group(1).lower() if type_match else "text"),
                "required": required,
                "placeholder": placeholder_match.group(1) if placeholder_match else "",
                "options": [],
                "validation_hint": "",
                "max_length": None,
                "accept_types": "",
            }
        )
    links = _extract_links_from_html(html, base_url=url, station_domain=_domain(url))
    emails = _extract_emails(html)
    forms_payload: list[dict] = []
    if form_count > 0 and fields:
        forms_payload = [{"index": i, "action": "", "method": "post", "fields": fields} for i in range(form_count)]
    return {
        "url": url,
        "title": "",
        "language": "",
        "requires_login": ("type=\"password\"" in lowered) or ("name=\"password\"" in lowered),
        "has_captcha": ("captcha" in lowered),
        "forms": forms_payload,
        "links": [
            {"href": href, "text": text, "score": round(float(score), 3)}
            for href, text, score in links[:300]
        ],
        "emails": emails,
        "email_contexts": _email_contexts(html, emails),
        "submission_keyword_contexts": _snippet_contexts(html, DEEP_SUBMISSION_HINTS),
        "snapshot_path": "",
        "dom_snapshot_path": "",
    }


def _persist_form(
    session: Session,
    station: Station,
    page_url: str,
    entry_path: list[str],
    form_payload: dict,
    mode: str,
    scan_meta: dict,
) -> SubmissionForm:
    form_type = _classify_form_type(page_url, scan_meta.get("title", ""), form_payload.get("fields", []))
    status = FormStatus.ACTIVE
    if scan_meta.get("has_captcha"):
        status = FormStatus.CAPTCHA_PRESENT
    elif scan_meta.get("requires_login"):
        status = FormStatus.LOGIN_REQUIRED

    form_row = session.scalar(
        select(SubmissionForm).where(SubmissionForm.station_id == station.id, SubmissionForm.url == page_url)
    )
    if form_row is None:
        form_row = SubmissionForm(
            station_id=station.id,
            url=page_url,
            discovered_at=datetime.utcnow(),
        )
        session.add(form_row)
        session.flush()

    form_row.page_title = scan_meta.get("title") or None
    form_row.language = scan_meta.get("language") or None
    form_row.form_type = form_type
    form_row.status = status
    form_row.requires_login = bool(scan_meta.get("requires_login"))
    form_row.has_captcha = bool(scan_meta.get("has_captcha"))
    form_row.confidence = 0.9 if status == FormStatus.ACTIVE else 0.6
    form_row.entry_path_json = json.dumps(entry_path, ensure_ascii=False)
    form_row.snapshot_path = scan_meta.get("snapshot_path") or None
    form_row.dom_snapshot_path = scan_meta.get("dom_snapshot_path") or None
    form_row.last_verified_at = datetime.utcnow()

    session.query(SubmissionFormField).where(SubmissionFormField.form_id == form_row.id).delete()
    normalized_fields: list[dict] = []
    used_field_keys: set[str] = set()
    for idx, field in enumerate(form_payload.get("fields", []), start=1):
        key_seed = field.get("key_seed") or field.get("name") or field.get("label") or f"field_{idx}"
        base_key = _safe_key(str(key_seed), idx)
        field_key = base_key
        collision_idx = 2
        while field_key in used_field_keys:
            field_key = f"{base_key}_{collision_idx}"
            collision_idx += 1
        used_field_keys.add(field_key)
        row = SubmissionFormField(
            form_id=form_row.id,
            field_key=field_key,
            name=(field.get("name") or None),
            label=(field.get("label") or None),
            input_type=(field.get("input_type") or "text"),
            required=bool(field.get("required")),
            placeholder=(field.get("placeholder") or None),
            options_json=json.dumps(field.get("options") or [], ensure_ascii=False),
            validation_hint=(field.get("validation_hint") or None),
            max_length=field.get("max_length"),
            accept_types=(field.get("accept_types") or None),
            upload_max_mb=None,
            source_snapshot_path=scan_meta.get("dom_snapshot_path") or None,
        )
        session.add(row)
        normalized_fields.append(
            {
                "field_key": field_key,
                "name": row.name,
                "label": row.label,
                "input_type": row.input_type,
                "required": row.required,
                "placeholder": row.placeholder,
                "options": json.loads(row.options_json),
                "validation_hint": row.validation_hint,
                "max_length": row.max_length,
                "accept_types": row.accept_types,
                "upload_max_mb": None,
            }
        )

    base_recipe = _heuristic_recipe(normalized_fields)
    recipe_payload = _codex_enrich_recipe(normalized_fields, base_recipe, page_url)
    current_version = (
        session.scalar(
            select(func.max(FormRecipe.version)).where(FormRecipe.form_id == form_row.id)
        )
        or 0
    )
    recipe = FormRecipe(
        form_id=form_row.id,
        version=int(current_version) + 1,
        mode=mode,
        confidence_score=form_row.confidence,
        status="active" if status == FormStatus.ACTIVE else "restricted",
        instructions_text=recipe_payload.get("notes_for_future_runs"),
        machine_mapping_json=json.dumps(recipe_payload.get("mapping_rules") or {}, ensure_ascii=False),
        field_order_json=json.dumps(recipe_payload.get("field_order") or [], ensure_ascii=False),
        upload_strategy_json=json.dumps(recipe_payload.get("upload_rules") or {}, ensure_ascii=False),
        submit_strategy_json=json.dumps(
            {
                "mode": mode,
                "requires_login": form_row.requires_login,
                "has_captcha": form_row.has_captcha,
                "allow_live_submit": False,
            },
            ensure_ascii=False,
        ),
        success_detection_rules_json=json.dumps(recipe_payload.get("success_detection") or [], ensure_ascii=False),
        error_detection_rules_json=json.dumps(recipe_payload.get("error_detection") or [], ensure_ascii=False),
        retry_rules_json=json.dumps(["retry_once_if_network_error", "skip_on_captcha"], ensure_ascii=False),
        notes_for_future_runs=recipe_payload.get("notes_for_future_runs"),
        discovered_at=datetime.utcnow(),
        last_verified_at=datetime.utcnow(),
    )
    session.add(recipe)
    session.flush()

    session.add(
        Evidence(
            station_id=station.id,
            source_type=SourceType.WEBSITE,
            source_url=page_url,
            source_id="form_scan",
            raw_title=scan_meta.get("title"),
            raw_snippet=f"mode={mode} form_type={form_type.value} status={status.value}",
            extracted_payload_json=json.dumps(
                {
                    "entry_path": entry_path,
                    "form_type": form_type.value,
                    "status": status.value,
                    "field_count": len(normalized_fields),
                    "snapshot_path": scan_meta.get("snapshot_path"),
                    "dom_snapshot_path": scan_meta.get("dom_snapshot_path"),
                },
                ensure_ascii=False,
            ),
            confidence=form_row.confidence,
        )
    )
    return form_row


def scan_submission_forms(
    session: Session,
    station_limit: int | None = None,
    country: str | None = None,
    station_id: int | None = None,
    mode: str = "read",
    max_forms_per_station: int | None = None,
) -> dict:
    if not settings.browser_worker_enabled:
        return {"stations_processed": 0, "forms_saved": 0, "reason": "browser_worker_disabled"}

    limit = station_limit or settings.browser_max_stations_per_run
    form_limit = max_forms_per_station or settings.browser_max_forms_per_station
    q = select(Station).where(
        Station.status != StationStatus.REJECTED,
        Station.website_url.is_not(None),
    )
    if station_id is not None:
        q = q.where(Station.id == station_id)
    if country:
        q = q.where(Station.country_code == country.upper())
    stations = session.scalars(q.order_by(Station.updated_at.desc()).limit(max(1, limit))).all()
    if not stations:
        return {"stations_processed": 0, "forms_saved": 0}

    stations_processed = 0
    forms_saved = 0
    pages_scanned = 0
    errors = 0
    emails_saved = 0
    skipped_unsupported_domain = 0
    skipped_excluded_focus = 0
    skipped_excluded_meta = 0
    skipped_entrypoint_only = 0

    for st in stations:
        if not is_supported_station_target_url(st.website_url):
            skipped_unsupported_domain += 1
            continue
        if station_matches_excluded_focus(st):
            skipped_excluded_focus += 1
            continue
        if station_matches_excluded_meta(st):
            skipped_excluded_meta += 1
            continue
        if station_matches_entrypoint_only(st):
            skipped_entrypoint_only += 1
            continue
        stations_processed += 1
        candidates = _candidate_urls_for_station(
            st,
            max_urls=max(2, settings.browser_deep_max_urls_per_station, form_limit * 2),
        )
        station_forms = 0
        for url, entry_path in candidates:
            if station_forms >= form_limit:
                break
            try:
                if sync_playwright is not None:
                    try:
                        meta = _extract_forms_playwright(url)
                    except Exception:
                        meta = _extract_forms_fallback(url)
                else:
                    meta = _extract_forms_fallback(url)
                pages_scanned += 1
            except Exception:
                errors += 1
                continue

            page_emails = [str(e).strip().lower() for e in (meta.get("emails") or []) if str(e).strip()]
            if page_emails:
                emails_saved += _persist_email_channels(session=session, station=st, source_url=url, emails=page_emails)

            for form_payload in meta.get("forms", []):
                if station_forms >= form_limit:
                    break
                if not form_payload.get("fields"):
                    continue
                _persist_form(
                    session=session,
                    station=st,
                    page_url=url,
                    entry_path=entry_path,
                    form_payload=form_payload,
                    mode=mode,
                    scan_meta=meta,
                )
                forms_saved += 1
                station_forms += 1
            session.commit()

    return {
        "stations_processed": stations_processed,
        "pages_scanned": pages_scanned,
        "forms_saved": forms_saved,
        "emails_saved": emails_saved,
        "errors": errors,
        "mode": mode,
        "country": country or "",
        "playwright_enabled": sync_playwright is not None,
        "deep_pass_enabled": bool(settings.browser_enable_deep_pass),
        "skipped_unsupported_domain": skipped_unsupported_domain,
        "skipped_excluded_focus": skipped_excluded_focus,
        "skipped_excluded_meta": skipped_excluded_meta,
        "skipped_entrypoint_only": skipped_entrypoint_only,
    }


def run_country_form_cycle(
    session: Session,
    station_limit: int | None = None,
    mode: str = "read",
    max_forms_per_station: int | None = None,
    countries: list[str] | None = None,
) -> dict:
    state = _load_country_cycle_state()
    if countries:
        cleaned = [c.strip().upper() for c in countries if c.strip()]
        if cleaned:
            state["countries"] = cleaned
            state["index"] = state["index"] % len(cleaned)
    sequence = state["countries"]
    if not sequence:
        sequence = _priority_countries()
        state["countries"] = sequence
        state["index"] = 0

    idx = int(state.get("index", 0)) % len(sequence)
    current = sequence[idx]
    result = scan_submission_forms(
        session=session,
        station_limit=station_limit or settings.browser_max_stations_per_run,
        country=current,
        mode=mode,
        max_forms_per_station=max_forms_per_station,
    )
    state["index"] = (idx + 1) % len(sequence)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_country_cycle_state(state)
    result["country_processed"] = current
    result["next_country"] = sequence[state["index"]]
    result["country_index_after_run"] = state["index"]
    return result

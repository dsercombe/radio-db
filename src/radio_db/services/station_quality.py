from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from openai import OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.gemini_generate import gemini_generate_content_json
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    Evidence,
    FormStatus,
    FormType,
    Station,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionAgentRun,
    SubmissionMethod,
)
from radio_db.services.budget import CostGuard


AGGREGATOR_DOMAIN_HINTS = (
    "emisora.org",
    "emisora.org.es",
    "radio.net",
    "radio-usa.net",
    "onlineradiobox",
    "mytuner",
    "tunein",
    "streema",
    "radio.garden",
    "zeno.fm",
    "liveonlineradio",
    "earshot-distro",
    "soundcloud.com",
    "topradio.me",
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
NON_STATION_ENTRYPOINT_HINTS = (
    "airplay guide",
    "award submission",
    "call for entries",
    "call for scores",
    "distro",
    "distribution",
    "how to submit",
    "independent radio exchange",
    "music distribution",
    "music submission",
    "music submissions",
    "send music to stations",
    "sending music to stations",
    "submission guidelines",
    "submissions —",
    "submissions -",
    "submit music",
    "submitting music",
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
    "outside",
    "purroy",
}
BEST_ROUTE_STRONG_TYPES = frozenset({"direct_music_form", "gated_music_form", "explicit_submission_email"})
PROMOTE_BLOCKER_REASON_CODES = {
    "aggregator_domain",
    "is_aggregator_domain",
    "website_is_not_station",
    "station_rejected",
    "rejected_status",
    "status_rejected",
    "low_station_confidence",
    "station_confidence_low",
    "low_confidence_score",
}
SPORT_FORMAT_HINTS = (
    "sport",
    "sports",
    "deport",
    "deportes",
    "deportiva",
    "futbol",
    "football",
    "soccer",
    "basket",
    "hockey",
    "baseball",
)
NEWS_TALK_FORMAT_HINTS = (
    "news",
    "nyheter",
    "noticias",
    "notizie",
    "nachrichten",
    "actualites",
    "actualidad",
    "talk",
    "npr",
)
RELIGIOUS_FORMAT_HINTS = (
    "christian",
    "christlich",
    "christ",
    "jesus",
    "gospel",
    "bible",
    "biblia",
    "bibel",
    "faith",
    "worship",
    "catholic",
    "church",
    "iglesia",
    "eglise",
    "evangel",
    "prayer",
    "religious",
    "radio maria",
    "bbn",
    "life channel",
)
SUBCHANNEL_PATH_HINTS = (
    "stations-de-radio",
    "webradio",
    "channels",
    "listen",
    "player",
)
SUBCHANNEL_NAME_HINTS = (
    "feelings",
    "greatest hits",
    "best hits",
    "non-stop",
    "non stop",
    "lounge",
    "chill",
    "love",
    "party",
    "artist",
    "artists",
)

DEEP_PATH_HINTS = (
    "/contact",
    "/kontakt",
    "/contacts",
    "/contato",
    "/contatto",
    "/contacto",
    "/nous-contacter",
    "/contactez-nous",
    "/contacter",
    "/about",
    "/about-us",
    "/team",
    "/staff",
    "/program",
    "/programs",
    "/programmation",
    "/programming",
    "/programacion",
    "/programacao",
    "/playlisting",
    "/music",
    "/musique",
    "/musica",
    "/musik",
    "/submit",
    "/submission",
    "/submissions",
    "/music-submissions",
    "/demo",
    "/playlist",
    "/airplay",
    "/promo",
    "/promos",
    "/soundpark",
)

DEEP_SUBMISSION_HINTS = (
    "submit music",
    "music submission",
    "demo submission",
    "playlist submission",
    "send your music",
    "new artist",
    "unsigned artist",
    "music programming",
    "programmation musicale",
    "programmation",
    "programacion musical",
    "programação musical",
    "promo musicale",
    "promotion musicale",
    "music promo",
    "playlisting",
    "contact us",
    "nous contacter",
    "contactez-nous",
    "contacto",
    "contato",
    "contatti",
    "einreichen",
    "musik einreichen",
    "vurdert for vare spillelister",
    "soundpark",
)
STRONG_SUBMISSION_CONTEXT_HINTS = (
    "submit",
    "submission",
    "submit music",
    "music submission",
    "demo",
    "demo submission",
    "playlist submission",
    "send your music",
    "new artist",
    "unsigned",
    "unsigned artist",
    "musik einreichen",
    "airplay",
    "music director",
    "program director",
    "playlisting",
    "soundpark",
)
DIRECT_MUSIC_SUBMISSION_PHRASES = (
    "submit music",
    "submit your music",
    "submit tracks",
    "submit your tracks",
    "send music",
    "send your music",
    "send us your music",
    "get your music on",
    "get your music played",
    "music submission",
    "music submissions",
    "new music submission",
    "new music submissions",
    "demo submission",
    "demo submissions",
    "playlist submission",
    "playlist submissions",
    "airplay consideration",
    "for airplay",
    "music for airplay",
    "tracks for airplay",
    "artist submission",
    "artist submissions",
    "musik einreichen",
    "musique soumettre",
    "soumettre votre musique",
    "envoyez votre musique",
    "enviar musica",
    "envie sua musica",
    "invia la tua musica",
)
DEDICATED_SUBMISSION_EMAIL_LOCALS = {
    "airplay",
    "artists",
    "demo",
    "demos",
    "music",
    "musicdirector",
    "musicmail",
    "musicsubmissions",
    "newmusic",
    "playlist",
    "playlisting",
    "songs",
    "submit",
    "submissions",
    "submitmusic",
    "tracks",
}
GENERIC_CONTACT_EMAIL_LOCALS = {
    "admin",
    "ads",
    "advertising",
    "contact",
    "hello",
    "info",
    "mail",
    "marketing",
    "news",
    "office",
    "press",
    "promo",
    "promos",
    "programming",
    "promotion",
    "promotions",
    "sales",
    "studio",
    "webmaster",
}
OBFUSCATED_EMAIL_REGEX = re.compile(
    r"\b([a-z0-9._%+\-]+)\s*(?:@|\[at\]|\(at\)|\sat\s)\s*([a-z0-9.\-]+\.[a-z]{2,})\b",
    flags=re.I,
)


def _safe_domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _is_aggregator_domain(url: str | None) -> bool:
    d = _safe_domain(url)
    return bool(d) and any(hint in d for hint in AGGREGATOR_DOMAIN_HINTS)


def _domain_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _route_matches_station_domain(station: Station, url: str = "", email: str = "") -> bool:
    station_domain = _safe_domain(station.website_url)
    route_domain = _safe_domain(url)
    if route_domain and station_domain and _domain_matches(route_domain, station_domain):
        return True
    if email and "@" in email and station_domain:
        email_domain = email.lower().split("@", 1)[1]
        if _domain_matches(email_domain, station_domain):
            return True
    return False


def _is_blocked_submission_route(station: Station, url: str = "", email: str = "", requirements: str = "") -> bool:
    route_domain = _safe_domain(url)
    email_lower = str(email or "").strip().lower()
    email_domain = email_lower.split("@", 1)[1] if "@" in email_lower else ""
    text = f"{url} {email_lower} {requirements}".lower()
    if route_domain and any(hint in route_domain for hint in AGGREGATOR_DOMAIN_HINTS):
        return True
    if email_lower in GENERIC_PLATFORM_EMAILS:
        return True
    if email_domain in PLATFORM_EMAIL_DOMAINS and not _route_matches_station_domain(station, url=url, email=email_lower):
        return True
    if "submit your radio station" in text or "directory for" in text and "radio stations" in text:
        return True
    return False


def _extract_emails(text: str) -> list[str]:
    raw = text or ""
    found = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", raw, flags=re.I)
    obfuscated = [f"{local}@{domain}" for local, domain in OBFUSCATED_EMAIL_REGEX.findall(raw)]
    out: list[str] = []
    seen: set[str] = set()
    for e in list(found) + obfuscated:
        v = e.strip().lower().rstrip(".,;:)]}>")
        if not _is_valid_discovered_email(v) or len(v) > 320 or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _extract_email_contexts(text: str, emails: list[str], url: str, radius: int = 180) -> list[dict[str, str]]:
    hay = _compact_text(text)
    lowered = hay.lower()
    contexts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for email in emails:
        idx = lowered.find(email.lower())
        if idx == -1:
            continue
        start = max(0, idx - radius)
        end = min(len(hay), idx + len(email) + radius)
        snippet = hay[start:end].strip()
        key = (email, snippet)
        if key in seen:
            continue
        seen.add(key)
        contexts.append({"email": email, "url": url, "snippet": snippet})
    return contexts[:8]


def _extract_keyword_contexts(text: str, url: str, radius: int = 180) -> list[dict[str, str]]:
    hay = _compact_text(text)
    lowered = hay.lower()
    contexts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for needle in DEEP_SUBMISSION_HINTS:
        idx = lowered.find(needle)
        if idx == -1:
            continue
        start = max(0, idx - radius)
        end = min(len(hay), idx + len(needle) + radius)
        snippet = hay[start:end].strip()
        key = (needle, snippet)
        if key in seen:
            continue
        seen.add(key)
        contexts.append({"keyword": needle, "url": url, "snippet": snippet})
        if len(contexts) >= 8:
            break
    return contexts


def _context_has_submission_signal(snippet: str) -> bool:
    lowered = _compact_text(snippet).lower()
    return any(hint in lowered for hint in STRONG_SUBMISSION_CONTEXT_HINTS)


def _has_direct_music_submission_phrase(text: str) -> bool:
    lowered = _compact_text(text).lower()
    return any(phrase in lowered for phrase in DIRECT_MUSIC_SUBMISSION_PHRASES)


def _email_local_part(email: str) -> str:
    local = str(email or "").split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9]+", "", local)


def _is_dedicated_submission_email(email: str) -> bool:
    local = _email_local_part(email)
    if not local or local in GENERIC_CONTACT_EMAIL_LOCALS:
        return False
    return local in DEDICATED_SUBMISSION_EMAIL_LOCALS or any(
        token in local
        for token in (
            "airplay",
            "demo",
            "newmusic",
            "playlist",
            "songsubmit",
            "submitmusic",
            "submission",
        )
    )


def _is_strong_submission_email_context(email: str, snippet: str) -> bool:
    text = _compact_text(snippet)
    if not text:
        return False
    if _has_direct_music_submission_phrase(text):
        return True
    return _is_dedicated_submission_email(email) and _context_has_submission_signal(text)


def _url_has_submission_path(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(
        token in lowered
        for token in (
            "airplay",
            "demo-submission",
            "music-submission",
            "music_submissions",
            "send-music",
            "submit-music",
            "submit_music",
        )
    )


def _has_direct_submission_page_signal(features: dict[str, Any]) -> bool:
    latest_page_evidence = (
        features.get("latest_scan_page_evidence")
        if isinstance(features.get("latest_scan_page_evidence"), list)
        else []
    )
    for page in latest_page_evidence:
        if not isinstance(page, dict):
            continue
        if _url_has_submission_path(str(page.get("url") or "")):
            return True
        contexts = page.get("submission_keyword_contexts") if isinstance(page.get("submission_keyword_contexts"), list) else []
        if any(_has_direct_music_submission_phrase(str(ctx.get("snippet") or "")) for ctx in contexts if isinstance(ctx, dict)):
            return True

    for value in features.get("deep_submission_url_samples") or []:
        if _url_has_submission_path(str(value or "")):
            return True
    for value in features.get("deep_submission_context_samples") or []:
        if _has_direct_music_submission_phrase(str(value or "")):
            return True
    return False


def _looks_like_non_music_submission_surface(url: str, requirements: str = "") -> bool:
    text = f"{str(url or '').lower()} {str(requirements or '').lower()}"
    path = urlparse(str(url or "")).path.lower()
    if re.search(r"\.(?:avif|gif|jpe?g|png|svg|webp)(?:$|[?#])", path):
        return True
    return any(
        token in text
        for token in (
            "submit-your-radio-station",
            "submit your radio station",
            "radio station directory",
            "submit an event",
            "event calendar",
            "host-an-hour",
            "host an hour",
            "award submission",
            "awards",
            "made in virginia",
            "request-a-song",
            "wake-up-song",
            "song challenge",
            "song-challenge",
            "songchallenge",
            "/program/",
            "contest",
            "sweepstake",
            "sweepstakes",
            "vote",
            "listener",
            "playlist request",
        )
    )


def _looks_like_structured_music_submission_surface(url: str, requirements: str = "") -> bool:
    text = f"{str(url or '').lower()} {str(requirements or '').lower()}"
    if _looks_like_non_music_submission_surface(url, requirements):
        return False
    strong_url = any(
        token in text
        for token in (
            "submit-music",
            "music-submission",
            "music-submissions",
            "submission-guidelines",
            "send us your music",
            "digital submissions",
            "contact/submit",
        )
    )
    strong_requirements = any(
        token in text
        for token in (
            "form_type=music_submission",
            "submit material",
            "for consideration",
            "focus tracks",
            "airplay consideration",
            "ready for airplay",
            "bandcamp",
            "soundcloud",
            "youtube",
            "streaming link",
            "release date",
            "artist bio",
            "music submissions are preferred",
            "digital submissions are preferred",
        )
    )
    return strong_url or strong_requirements


def _best_route_base_score(route_type: str) -> int:
    return {
        "direct_music_form": 100,
        "gated_music_form": 85,
        "explicit_submission_email": 76,
        "submission_page_signal": 58,
        "contact_email_only": 30,
        "contact_form_only": 30,
    }.get(str(route_type or ""), 0)


def _route_candidate(
    *,
    route_type: str,
    source: str,
    url: str = "",
    email: str = "",
    reason: str = "",
    confidence: float = 0.0,
) -> dict[str, Any]:
    return {
        "route_type": str(route_type or ""),
        "source": str(source or ""),
        "url": str(url or "")[:1024],
        "email": str(email or "")[:320],
        "reason": str(reason or "")[:280],
        "confidence": round(max(0.0, min(1.0, float(confidence or 0.0))), 4),
        "score": _best_route_base_score(route_type),
    }


def _collect_submission_route_candidates(station: Station) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(candidate: dict[str, Any]) -> None:
        key = (
            str(candidate.get("route_type") or ""),
            str(candidate.get("url") or ""),
            str(candidate.get("email") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    for form in station.forms:
        url = str(getattr(form, "url", "") or "")
        if _looks_like_non_music_submission_surface(url):
            continue
        if form.form_type not in {FormType.MUSIC_SUBMISSION, FormType.NEWCOMER, FormType.ARTIST_UPLOAD}:
            continue
        if form.status == FormStatus.ACTIVE:
            add(
                _route_candidate(
                    route_type="direct_music_form",
                    source="form",
                    url=url,
                    reason="Active music submission form",
                    confidence=0.95,
                )
            )
        elif form.status in {FormStatus.LOGIN_REQUIRED, FormStatus.CAPTCHA_PRESENT}:
            add(
                _route_candidate(
                    route_type="gated_music_form",
                    source="form",
                    url=url,
                    reason="Gated music submission form",
                    confidence=0.82,
                )
            )

    for form in station.forms:
        url = str(getattr(form, "url", "") or "")
        if _looks_like_non_music_submission_surface(url):
            continue
        if form.form_type != FormType.GENERAL_CONTACT or form.status != FormStatus.ACTIVE:
            continue
        add(
            _route_candidate(
                route_type="contact_form_only",
                source="form",
                url=url,
                reason="General station contact form only",
                confidence=0.35,
            )
        )

    for channel in station.submissions:
        url = str(getattr(channel, "url", "") or "")
        email = str(getattr(channel, "email", "") or "")
        requirements = str(getattr(channel, "requirements", "") or "")
        if channel.method == SubmissionMethod.FORM:
            if _is_blocked_submission_route(station, url=url, email=email, requirements=requirements):
                continue
            if _looks_like_non_music_submission_surface(url, requirements):
                continue
            if url and not _route_matches_station_domain(station, url=url, email=email):
                continue
            route_type = "contact_form_only"
            confidence = 0.35
            reason = requirements or "Stored contact form channel"
            if _looks_like_structured_music_submission_surface(url, requirements):
                route_type = "direct_music_form"
                confidence = 0.88
                reason = requirements or "Stored music submission form channel"
            add(
                _route_candidate(
                    route_type=route_type,
                    source="submission_channel_form",
                    url=url,
                    email=email,
                    reason=reason,
                    confidence=confidence,
                )
            )
            continue

        if channel.method == SubmissionMethod.UNKNOWN and _looks_like_structured_music_submission_surface(url, requirements):
            if _is_blocked_submission_route(station, url=url, email=email, requirements=requirements):
                continue
            if url and not _route_matches_station_domain(station, url=url, email=email):
                continue
            route_type = "explicit_submission_email" if email else "submission_page_signal"
            add(
                _route_candidate(
                    route_type=route_type,
                    source="submission_channel_unknown",
                    url=url,
                    email=email,
                    reason=requirements or "Structured stored submission route",
                    confidence=0.84 if email else 0.68,
                )
            )
            continue

        if channel.method == SubmissionMethod.EMAIL and email:
            email_lower = email.lower()
            if _is_blocked_submission_route(station, url=url, email=email_lower, requirements=requirements):
                continue
            if any(token in email_lower for token in ("music@", "submit", "submission", "newmusic@", "airplay@")):
                if not _route_matches_station_domain(station, url=url, email=email_lower) and not _has_direct_music_submission_phrase(requirements):
                    continue
                add(
                    _route_candidate(
                        route_type="explicit_submission_email",
                        source="submission_channel_email",
                        url=url,
                        email=email,
                        reason=requirements or "Dedicated submission-style email",
                        confidence=0.78,
                    )
                )
            else:
                add(
                    _route_candidate(
                        route_type="contact_email_only",
                        source="submission_channel_email",
                        url=url,
                        email=email,
                        reason=requirements or "General contact email only",
                        confidence=0.42,
                    )
                )

    for contact in station.contacts:
        email = str(getattr(contact, "email", "") or "")
        contact_url = str(getattr(contact, "contact_url", "") or "")
        if not email and not contact_url:
            continue
        route_type = "contact_email_only" if email else "contact_form_only"
        reason = str(getattr(contact, "notes", "") or "") or (
            "Station contact email" if email else "Station contact form"
        )
        add(
            _route_candidate(
                route_type=route_type,
                source="contact",
                url=contact_url,
                email=email,
                reason=reason,
                confidence=0.35,
            )
        )

    return sorted(
        candidates,
        key=lambda item: (
            int(item.get("score") or 0),
            float(item.get("confidence") or 0.0),
            len(str(item.get("reason") or "")),
        ),
        reverse=True,
    )


def _maybe_rank_best_submission_route_with_gemini(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    strong = [c for c in candidates if str(c.get("route_type") or "") in BEST_ROUTE_STRONG_TYPES]
    if len(strong) < 2 or not settings.gemini_api_key:
        return None
    prompt = (
        "Choose the single best music submission route.\n"
        "Return strict JSON with keys: best_index (int), reason_short (string), reject_indices (array of ints), needs_manual_review (bool).\n"
        "Prefer artist/music submission routes over listener requests, contests, wake-up songs, programming requests, or generic contact.\n"
        f"CANDIDATES:\n{json.dumps(strong[:5], ensure_ascii=False)}"
    )
    try:
        payload = _call_gemini_verifier(prompt)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        idx = int(payload.get("best_index"))
    except Exception:
        return None
    if idx < 0 or idx >= len(strong[:5]):
        return None
    selected = dict(strong[idx])
    selected["reason"] = str(payload.get("reason_short") or selected.get("reason") or "")[:280]
    selected["needs_manual_review"] = bool(payload.get("needs_manual_review"))
    return selected


def _select_best_submission_route(station: Station) -> dict[str, Any]:
    candidates = _collect_submission_route_candidates(station)
    if not candidates:
        return {"best": None, "secondary": []}
    best = dict(candidates[0])
    gemini_choice = _maybe_rank_best_submission_route_with_gemini(candidates)
    if gemini_choice is not None:
        best = gemini_choice
    secondary = [
        item
        for item in candidates
        if (
            str(item.get("url") or ""),
            str(item.get("email") or ""),
            str(item.get("route_type") or ""),
        )
        != (
            str(best.get("url") or ""),
            str(best.get("email") or ""),
            str(best.get("route_type") or ""),
        )
    ][:5]
    return {"best": best, "secondary": secondary}


def _is_valid_discovered_email(email: str) -> bool:
    value = str(email or "").strip().lower().rstrip(".,;:)]}>")
    if "@" not in value:
        return False
    local, domain = value.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    if "/" in value or "\\" in value:
        return False
    if re.fullmatch(r"u[0-9a-f]{3,}", local):
        return False
    if local.startswith("x-") or local in {"x-transition", "click"}:
        return False
    if local in {"johndoe", "john.doe", "jane.doe", "your-name", "yourname", "name", "exemple"}:
        return False
    if domain in {"address.com", "example.com", "example.org", "example.net", "mail.fr"}:
        return False
    tld = domain.rsplit(".", 1)[-1]
    if tld in INVALID_DISCOVERED_EMAIL_TLDS:
        return False
    return True


def _normalize_text_bits(values: list[str | None]) -> str:
    return " ".join(str(v or "").strip().lower() for v in values if str(v or "").strip())


def _path_depth(url: str | None) -> int:
    path = (urlparse(url or "").path or "").strip("/")
    if not path:
        return 0
    return len([part for part in path.split("/") if part])


def _has_any_hint(text: str, hints: tuple[str, ...]) -> bool:
    hay = f" {text.strip().lower()} "
    return any(f" {hint} " in hay or hint in hay for hint in hints)


def _load_exception_rules() -> list[dict[str, Any]]:
    path = Path(settings.station_quality_exception_registry_path)
    if not path.is_absolute():
        path = Path(path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def _rule_active(rule: dict[str, Any]) -> bool:
    expires_at = (rule.get("expires_at") or "").strip() if isinstance(rule.get("expires_at"), str) else ""
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except Exception:
        return True
    now = datetime.utcnow().astimezone(expiry.tzinfo)
    return now <= expiry


def _rule_matches(rule: dict[str, Any], station: Station, features: dict[str, Any]) -> bool:
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    if not match:
        return False
    if "station_id" in match and int(match.get("station_id")) != int(station.id):
        return False
    if "domain" in match:
        domain = str(match.get("domain") or "").strip().lower()
        if domain and domain != str(features.get("website_domain") or "").lower():
            return False
    if "domain_contains" in match:
        needle = str(match.get("domain_contains") or "").strip().lower()
        if needle and needle not in str(features.get("website_domain") or "").lower():
            return False
    if "name_contains" in match:
        needle = str(match.get("name_contains") or "").strip().lower()
        if needle and needle not in (station.canonical_name or "").lower():
            return False
    return True


def _apply_exception_rules(
    station: Station, features: dict[str, Any], verdict: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    rules = _load_exception_rules()
    applied: list[str] = []
    if not rules:
        return verdict, applied
    out = dict(verdict)
    reason_codes = out.get("reason_codes") if isinstance(out.get("reason_codes"), list) else []
    for rule in rules:
        if not _rule_active(rule):
            continue
        if not _rule_matches(rule, station, features):
            continue
        force = rule.get("force") if isinstance(rule.get("force"), dict) else {}
        if not force:
            continue
        for key, value in force.items():
            out[key] = value
        code = str(rule.get("id") or "rule").strip() or "rule"
        reason_codes.append(f"exception_override:{code}")
        applied.append(code)
    out["reason_codes"] = reason_codes
    return out, applied


def _deep_candidate_urls(station: Station, features: dict[str, Any]) -> list[str]:
    seed: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if not u:
            return
        v = str(u).strip()
        if not v or v in seen:
            return
        if not v.startswith(("http://", "https://")):
            return
        if _safe_domain(v) != _safe_domain(station.website_url):
            return
        seen.add(v)
        seed.append(v)

    add(station.website_url)
    for hint in DEEP_PATH_HINTS:
        add(urljoin(station.website_url or "", hint))
    for u in features.get("playwright_interesting_url_samples") or []:
        add(u)
    return seed[: max(1, int(settings.station_quality_deep_max_pages))]


def _run_deep_pass(station: Station, features: dict[str, Any]) -> dict[str, Any]:
    urls = _deep_candidate_urls(station, features)
    pages_checked = 0
    found_submission_urls: list[str] = []
    found_emails: set[str] = set()
    submission_signal = False
    email_context_samples: list[str] = []
    submission_context_samples: list[str] = []
    contextual_submission_emails: set[str] = set()

    for url in urls:
        try:
            html = get_text(
                url=url,
                timeout_seconds=float(settings.station_quality_deep_fetch_timeout_seconds),
                max_attempts=2,
            )
        except Exception:
            continue
        pages_checked += 1
        if len(html) > 500_000:
            html = html[:500_000]
        lowered = html.lower()
        if any(h in lowered for h in STRONG_SUBMISSION_CONTEXT_HINTS):
            submission_signal = True
            if len(found_submission_urls) < 10:
                found_submission_urls.append(url)
        emails = _extract_emails(html)
        keyword_contexts = _extract_keyword_contexts(html, url=url)
        for ctx in keyword_contexts:
            sample = f"{ctx['url']} :: {ctx['snippet'][:260]}"
            if sample not in submission_context_samples:
                submission_context_samples.append(sample)
        for email in emails:
            found_emails.add(email)
        for ctx in _extract_email_contexts(html, emails=emails, url=url):
            sample = f"{ctx['email']} @ {ctx['url']} :: {ctx['snippet'][:260]}"
            if sample not in email_context_samples:
                email_context_samples.append(sample)
            if _is_strong_submission_email_context(ctx["email"], ctx["snippet"]):
                contextual_submission_emails.add(ctx["email"])
                submission_signal = True
                if len(found_submission_urls) < 10:
                    found_submission_urls.append(url)

    merged = dict(features)
    merged["deep_pass_enabled"] = True
    merged["deep_pages_checked"] = pages_checked
    merged["deep_submission_signal"] = submission_signal
    merged["deep_submission_url_samples"] = found_submission_urls[:10]
    merged["deep_email_count"] = len(found_emails)
    merged["deep_email_samples"] = sorted(found_emails)[:10]
    merged["deep_email_context_samples"] = email_context_samples[:8]
    merged["deep_submission_context_samples"] = submission_context_samples[:8]
    merged["contextual_submission_email_count"] = len(contextual_submission_emails)
    merged["contextual_submission_email_samples"] = sorted(contextual_submission_emails)[:8]
    merged["contact_with_email_count"] = int(merged.get("contact_with_email_count", 0)) + len(found_emails)
    if submission_signal and int(merged.get("submission_channel_total", 0)) == 0:
        merged["submission_channel_total"] = 1
        merged["submission_channel_form_count"] = max(1, int(merged.get("submission_channel_form_count", 0)))
    if contextual_submission_emails:
        merged["submission_channel_email_count"] = max(
            int(merged.get("submission_channel_email_count", 0)),
            len(contextual_submission_emails),
        )
    if submission_signal and not bool(merged.get("newcomer_signal")):
        merged["newcomer_signal"] = True
    return merged


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty_response")
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("no_json_object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("json_not_object")
    return payload


def _estimate_call_usd(prompt_text: str, provider: str) -> float:
    input_tokens = CostGuard.estimate_input_tokens(prompt_text)
    output_tokens = max(64, int(settings.station_quality_estimated_output_tokens))
    p = (provider or "").strip().lower()
    if p == "gemini":
        return (input_tokens / 1_000_000) * settings.station_quality_gemini_input_price_per_1m + (
            output_tokens / 1_000_000
        ) * settings.station_quality_gemini_output_price_per_1m
    return (input_tokens / 1_000_000) * settings.openai_input_price_per_1m + (
        output_tokens / 1_000_000
    ) * settings.openai_output_price_per_1m


def _normalize_reason_codes(verdict: dict[str, Any]) -> list[str]:
    raw = verdict.get("reason_codes") if isinstance(verdict.get("reason_codes"), list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        code = str(item or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _submission_path_quality(features: dict[str, Any]) -> str:
    latest_scan_pages = int(features.get("latest_scan_pages_scanned", 0) or 0)
    strong_unknown_channels = int(features.get("strong_unknown_submission_channel_count", 0) or 0)
    strong_unknown_emails = int(features.get("strong_unknown_submission_email_count", 0) or 0)
    best_route = features.get("best_submission_route") if isinstance(features.get("best_submission_route"), dict) else {}
    best_route_type = str(best_route.get("route_type") or "")
    if best_route_type:
        return best_route_type
    if latest_scan_pages <= 0:
        if int(features.get("active_music_form_count", 0) or 0) > 0:
            return "direct_music_form"
        if int(features.get("gated_music_form_count", 0) or 0) > 0:
            return "gated_music_form"
        if strong_unknown_emails > 0:
            return "explicit_submission_email"
        if strong_unknown_channels > 0:
            return "submission_page_signal"
    if int(features.get("latest_scan_strict_contextual_email_count", 0) or 0) > 0:
        return "explicit_submission_email"
    latest_page_evidence = (
        features.get("latest_scan_page_evidence")
        if isinstance(features.get("latest_scan_page_evidence"), list)
        else []
    )
    if int(features.get("latest_scan_forms_saved", 0) or 0) > 0:
        if any(bool(page.get("music_form_hint")) for page in latest_page_evidence if isinstance(page, dict)):
            return "direct_music_form"
        if _has_direct_submission_page_signal(features):
            return "submission_page_signal"
    if int(features.get("contextual_submission_email_count", 0) or 0) > 0:
        return "explicit_submission_email"
    if latest_scan_pages <= 0:
        if int(features.get("active_music_form_count", 0) or 0) > 0:
            return "direct_music_form"
        if int(features.get("gated_music_form_count", 0) or 0) > 0:
            return "gated_music_form"
        if strong_unknown_emails > 0:
            return "explicit_submission_email"
        if strong_unknown_channels > 0:
            return "submission_page_signal"
    if _has_direct_submission_page_signal(features) and int(features.get("submission_channel_total", 0) or 0) > 0:
        return "submission_page_signal"
    if (
        int(features.get("submission_channel_email_count", 0) or 0) > 0
        or int(features.get("contact_with_email_count", 0) or 0) > 0
        or int(features.get("deep_email_count", 0) or 0) > 0
        or int(features.get("latest_scan_emails_saved", 0) or 0) > 0
    ):
        return "contact_email_only"
    return "none"


def _apply_decision_guardrails(station: Station, features: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    out = dict(verdict)
    decision = str(out.get("decision") or "review").strip().lower()
    quality_score = max(0, min(100, int(out.get("quality_score", 50))))
    confidence = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    reason_codes = _normalize_reason_codes(out)

    active_music_form = int(features.get("active_music_form_count", 0)) > 0
    gated_music_form = int(features.get("gated_music_form_count", 0)) > 0
    submission_path_quality = str(features.get("submission_path_quality") or _submission_path_quality(features))
    explicit_submission_email = submission_path_quality == "explicit_submission_email"
    contact_only = submission_path_quality in {"contact_email_only", "contact_form_only"}
    contact_email = int(features.get("contact_with_email_count", 0)) > 0
    contextual_submission_email = (
        int(features.get("contextual_submission_email_count", 0)) > 0
        or int(features.get("latest_scan_strict_contextual_email_count", 0) or 0) > 0
    )
    newcomer_signal = bool(features.get("newcomer_signal"))
    is_real_station = bool(out.get("is_real_station", False))
    is_aggregator = bool(features.get("is_aggregator_domain"))
    has_blocker = any(code in PROMOTE_BLOCKER_REASON_CODES for code in reason_codes)
    topical_exclusion_labels = [str(x) for x in (features.get("topical_exclusion_labels") or [])]
    likely_subchannel = bool(features.get("likely_shared_domain_subchannel"))

    strong_outreach_path = active_music_form or gated_music_form or explicit_submission_email

    if (
        decision == "reject"
        and strong_outreach_path
        and is_real_station
        and quality_score >= 65
        and not is_aggregator
        and not has_blocker
    ):
        out["decision"] = "promote"
        out["accepts_music_submissions"] = True
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", True) or newcomer_signal)
        reason_codes.append("guardrail:strong_submission_path_overrides_format_noise")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        decision = "promote"

    if submission_path_quality in {"unknown", "none", ""} and decision == "promote":
        out["decision"] = "review"
        out["accepts_music_submissions"] = False
        reason_codes.append("review_guardrail:no_confirmed_submission_path")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if is_aggregator and decision == "promote":
        out["decision"] = "review"
        out["accepts_music_submissions"] = False
        reason_codes.append("review_guardrail:aggregator_not_verified")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if contact_only and decision == "promote":
        out["decision"] = "review"
        out["accepts_music_submissions"] = False
        reason_codes.append("review_guardrail:contact_only_not_submission")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if submission_path_quality == "submission_page_signal" and decision == "promote":
        out["decision"] = "review"
        out["accepts_music_submissions"] = False
        reason_codes.append("review_guardrail:submission_page_signal_needs_confirmed_channel")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if topical_exclusion_labels and decision != "reject" and not strong_outreach_path:
        out["decision"] = "reject"
        out["accepts_music_submissions"] = False
        out["accepts_new_artists"] = False
        for label in topical_exclusion_labels:
            reason_codes.append(f"format_rejected:{label}")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if likely_subchannel and decision == "promote":
        out["decision"] = "review"
        reason_codes.append("review_guardrail:shared_domain_subchannel")
        out["reason_codes"] = _normalize_reason_codes({"reason_codes": reason_codes})
        return out

    if (
        decision == "review"
        and not is_aggregator
        and not has_blocker
        and not likely_subchannel
        and is_real_station
        and quality_score >= 70
        and submission_path_quality in {"direct_music_form", "gated_music_form", "explicit_submission_email"}
        and (
            (active_music_form and confidence >= 0.25)
            or (confidence >= 0.6 and strong_outreach_path)
        )
        and strong_outreach_path
    ):
        out["decision"] = "promote"
        out["accepts_music_submissions"] = bool(out.get("accepts_music_submissions", False) or strong_outreach_path)
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", False) or newcomer_signal)
        reason_codes.append("promote_guardrail:real_station_with_outreach_path")
        out["reason_codes"] = reason_codes
        return out

    if (
        decision == "review"
        and not is_aggregator
        and not has_blocker
        and not likely_subchannel
        and is_real_station
        and quality_score >= 60
        and contextual_submission_email
        and (gated_music_form or newcomer_signal or explicit_submission_email)
    ):
        # Don't promote on contextual_submission_email alone - requires stronger signal
        # (actual music form, not just generic contact email on website)
        out["decision"] = "review"
        out["accepts_music_submissions"] = bool(gated_music_form or (explicit_submission_email and newcomer_signal))
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", False) or newcomer_signal)
        reason_codes.append("review_guardrail:contextual_email_needs_stronger_signal")
        out["reason_codes"] = reason_codes
        return out

    if (
        decision == "reject"
        and not is_aggregator
        and not has_blocker
        and is_real_station
        and quality_score >= 60
        and confidence >= 0.6
        and gated_music_form
        and (explicit_submission_email or contact_email)
    ):
        out["decision"] = "review"
        out["accepts_music_submissions"] = True
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", False) or newcomer_signal)
        reason_codes.append("review_guardrail:gated_submission_path")
        out["reason_codes"] = reason_codes
        return out

    out["reason_codes"] = reason_codes
    return out


UNCONFIRMED_SUBMISSION_CLAIM_PHRASES = (
    "accepts submissions",
    "accepts music",
    "actively seeks submissions",
    "actively seeks music",
    "takes submissions",
    "open to submissions",
    "welcomes submissions",
    "direct submission",
    "confirmed submission",
    "clear submission",
)


def _mentions_confirmed_submission(summary: str) -> bool:
    lowered = str(summary or "").lower()
    return any(phrase in lowered for phrase in UNCONFIRMED_SUBMISSION_CLAIM_PHRASES)


def _build_station_features(session: Session, station: Station, include_deep_pass: bool = True) -> dict[str, Any]:
    submissions = list(station.submissions or [])
    forms = list(station.forms or [])
    contacts = list(station.contacts or [])
    evidence_row = session.scalar(
        select(Evidence)
        .where(Evidence.station_id == station.id, Evidence.source_id == "playwright_page_signals")
        .order_by(Evidence.created_at.desc())
    )
    payload: dict[str, Any] = {}
    if evidence_row and evidence_row.extracted_payload_json:
        try:
            parsed = json.loads(evidence_row.extracted_payload_json)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
    latest_scan_run = session.scalar(
        select(SubmissionAgentRun)
        .where(SubmissionAgentRun.station_id == station.id)
        .order_by(SubmissionAgentRun.finished_at.desc().nulls_last(), SubmissionAgentRun.id.desc())
    )
    latest_scan_summary: dict[str, Any] = {}
    if latest_scan_run and latest_scan_run.summary_json:
        try:
            parsed_summary = json.loads(latest_scan_run.summary_json)
            if isinstance(parsed_summary, dict):
                latest_scan_summary = parsed_summary
        except Exception:
            latest_scan_summary = {}
    page_evidence = latest_scan_summary.get("page_evidence") if isinstance(latest_scan_summary.get("page_evidence"), list) else []
    compact_page_evidence: list[dict[str, Any]] = []
    contextual_email_count = 0
    strict_contextual_email_count = 0
    strict_contextual_email_samples: list[str] = []
    for page in page_evidence[:12]:
        if not isinstance(page, dict):
            continue
        email_contexts = page.get("email_contexts") if isinstance(page.get("email_contexts"), list) else []
        keyword_contexts = (
            page.get("submission_keyword_contexts")
            if isinstance(page.get("submission_keyword_contexts"), list)
            else []
        )
        contextual_email_count += sum(1 for ctx in email_contexts if isinstance(ctx, dict) and ctx.get("near_submission_signal"))
        for ctx in email_contexts:
            if not isinstance(ctx, dict):
                continue
            email = str(ctx.get("email") or "").strip().lower()
            snippet = str(ctx.get("snippet") or "")
            if not _is_strong_submission_email_context(email, snippet):
                continue
            strict_contextual_email_count += 1
            sample = f"{email} :: {snippet[:220]}"
            if sample not in strict_contextual_email_samples:
                strict_contextual_email_samples.append(sample)
        compact_page_evidence.append(
            {
                "url": str(page.get("url") or "")[:180],
                "entry_path": page.get("entry_path") if isinstance(page.get("entry_path"), list) else [],
                "title": str(page.get("page_title") or "")[:120],
                "form_count": int(page.get("form_count") or 0),
                "music_form_hint": bool(page.get("music_form_hint")),
                "email_count": len(page.get("emails") or []) if isinstance(page.get("emails"), list) else 0,
                "email_contexts": email_contexts[:4],
                "submission_keyword_contexts": keyword_contexts[:4],
            }
        )

    active_forms = [f for f in forms if f.status == FormStatus.ACTIVE]
    high_value_forms = [
        f
        for f in active_forms
        if f.form_type in {FormType.MUSIC_SUBMISSION, FormType.NEWCOMER, FormType.ARTIST_UPLOAD}
        and not _looks_like_non_music_submission_surface(str(getattr(f, "url", "") or ""))
    ]
    gated_music_forms = [
        f
        for f in forms
        if f.status in {FormStatus.LOGIN_REQUIRED, FormStatus.CAPTCHA_PRESENT}
        and f.form_type in {FormType.MUSIC_SUBMISSION, FormType.NEWCOMER, FormType.ARTIST_UPLOAD}
    ]
    form_methods = [
        s
        for s in submissions
        if s.method == SubmissionMethod.FORM
        and not _looks_like_non_music_submission_surface(str(getattr(s, "url", "") or ""), str(getattr(s, "requirements", "") or ""))
    ]
    email_methods = [s for s in submissions if s.method == SubmissionMethod.EMAIL]
    strong_unknown_channels = [
        s
        for s in submissions
        if s.method == SubmissionMethod.UNKNOWN
        and _looks_like_structured_music_submission_surface(
            str(getattr(s, "url", "") or ""),
            str(getattr(s, "requirements", "") or ""),
        )
    ]
    unknown_email_methods = [s for s in strong_unknown_channels if getattr(s, "email", None)]
    newcomer_signal = any(bool(s.accepts_newcomers) for s in submissions) or bool(high_value_forms) or bool(gated_music_forms)
    discovered_emails = payload.get("emails") if isinstance(payload.get("emails"), list) else []
    interesting_urls = payload.get("interesting_urls") if isinstance(payload.get("interesting_urls"), list) else []
    genres = [str(g.genre or "").strip().lower() for g in station.genres if str(g.genre or "").strip()]
    website_path = (urlparse(station.website_url or "").path or "").strip().lower()
    website_path_depth = _path_depth(station.website_url)
    domain_count = 0
    if station.website_url:
        domain = _safe_domain(station.website_url)
        if domain:
            domain_count = int(
                session.scalar(
                    select(func.count(Station.id)).where(
                        Station.id != station.id,
                        Station.website_url.is_not(None),
                        Station.website_url.ilike(f"%{domain}%"),
                    )
                )
                or 0
            )
    classification_text = _normalize_text_bits(
        [
            station.canonical_name,
            station.normalized_name,
            station.website_url,
            website_path,
            *genres,
        ]
    )
    name_text = _normalize_text_bits([station.canonical_name, station.normalized_name])
    short_generic_name = len([part for part in re.split(r"[^a-z0-9]+", name_text) if part]) <= 4
    has_frequency_or_location = bool(re.search(r"\d", name_text) or "," in (station.canonical_name or ""))
    topical_exclusion_labels: list[str] = []
    if _has_any_hint(classification_text, SPORT_FORMAT_HINTS):
        topical_exclusion_labels.append("sport")
    if _has_any_hint(classification_text, NEWS_TALK_FORMAT_HINTS):
        topical_exclusion_labels.append("news_talk")
    if _has_any_hint(classification_text, RELIGIOUS_FORMAT_HINTS):
        topical_exclusion_labels.append("religious")
    if _has_any_hint(classification_text, NON_STATION_ENTRYPOINT_HINTS) and not has_frequency_or_location:
        topical_exclusion_labels.append("submission_entrypoint")
    likely_shared_domain_subchannel = bool(
        domain_count >= 2
        and (
            website_path_depth >= 2
            or _has_any_hint(classification_text, SUBCHANNEL_PATH_HINTS)
            or " artists " in f" {classification_text} "
            or (
                domain_count >= 5
                and website_path_depth == 0
                and short_generic_name
                and not has_frequency_or_location
                and _has_any_hint(classification_text, SUBCHANNEL_NAME_HINTS)
            )
        )
    )

    priority_tier = int(getattr(station, "priority_tier", 0) or 0)
    route_selection = _select_best_submission_route(station)
    best_route = route_selection.get("best") if isinstance(route_selection, dict) else None
    secondary_routes = route_selection.get("secondary") if isinstance(route_selection, dict) else []

    features = {
        "station_id": station.id,
        "status": station.status.value if isinstance(station.status, StationStatus) else str(station.status),
        "country_code": station.country_code or "",
        "language": station.language or "",
        "website_url": station.website_url or "",
        "website_domain": _safe_domain(station.website_url),
        "website_path": website_path,
        "website_path_depth": website_path_depth,
        "is_aggregator_domain": _is_aggregator_domain(station.website_url),
        "has_stream_url": bool(station.stream_url),
        "priority_tier": priority_tier,
        "station_confidence_score": float(station.confidence_score or 0.0),
        "genre_samples": genres[:10],
        "same_domain_station_count": domain_count,
        "likely_shared_domain_subchannel": likely_shared_domain_subchannel,
        "topical_exclusion_labels": topical_exclusion_labels,
        "submission_channel_form_count": len(form_methods) + len(strong_unknown_channels),
        "submission_channel_email_count": len(email_methods) + len(unknown_email_methods),
        "submission_channel_total": len(submissions),
        "strong_unknown_submission_channel_count": len(strong_unknown_channels),
        "strong_unknown_submission_email_count": len(unknown_email_methods),
        "contact_total": len(contacts),
        "contact_with_email_count": sum(1 for c in contacts if c.email),
        "forms_total": len(forms),
        "active_forms_total": len(active_forms),
        "active_music_form_count": len(high_value_forms),
        "gated_music_form_count": len(gated_music_forms),
        "newcomer_signal": newcomer_signal,
        "playwright_discovered_email_count": len(discovered_emails),
        "playwright_interesting_url_count": len(interesting_urls),
        "playwright_interesting_url_samples": [str(x)[:180] for x in interesting_urls[:8]],
        "playwright_email_samples": [str(x)[:180] for x in discovered_emails[:8]],
        "latest_scan_run_id": int(latest_scan_run.id) if latest_scan_run else None,
        "latest_scan_status": str(latest_scan_run.status) if latest_scan_run else "",
        "latest_scan_blocked_reason": str(latest_scan_run.blocked_reason or "") if latest_scan_run else "",
        "latest_scan_pages_scanned": int(latest_scan_summary.get("pages_scanned", 0) or 0),
        "latest_scan_forms_saved": int(latest_scan_summary.get("forms_saved", 0) or 0),
        "latest_scan_emails_saved": int(latest_scan_summary.get("emails_saved", 0) or 0),
        "latest_scan_page_evidence": compact_page_evidence,
        "latest_scan_contextual_email_count": contextual_email_count,
        "latest_scan_strict_contextual_email_count": strict_contextual_email_count,
        "latest_scan_strict_contextual_email_samples": strict_contextual_email_samples[:8],
        "best_submission_route": best_route if isinstance(best_route, dict) else None,
        "secondary_submission_routes": secondary_routes if isinstance(secondary_routes, list) else [],
    }
    should_run_deep_pass = bool(station.website_url) and not bool(features["is_aggregator_domain"]) and (
        features["active_music_form_count"] > 0
        or features["gated_music_form_count"] > 0
        or features["submission_channel_email_count"] > 0
        or features["playwright_interesting_url_count"] > 0
    )
    if include_deep_pass and should_run_deep_pass:
        features = _run_deep_pass(station=station, features=features)
    features["submission_path_quality"] = _submission_path_quality(features)
    features["submission_path_policy"] = (
        "direct/gated music forms and contextual submission emails are submission signals; "
        "generic contact emails are contact-only and must not be described as accepting submissions."
    )
    return features


def _build_prompt(features: dict[str, Any]) -> str:
    return (
        "You are a radio-station quality verifier for music pitching.\n"
        "Classify the station based on the provided FEATURES JSON only.\n"
        "Return STRICT JSON with keys:\n"
        "is_real_station (bool), quality_score (int 0-100), confidence (float 0-1),\n"
        "decision ('promote'|'review'|'reject'), accepts_music_submissions (bool),\n"
        "accepts_new_artists (bool), reason_codes (array of strings), rationale_short (string),\n"
        "editorial_summary_short (string), editorial_format (string), style_tags (array of strings),\n"
        "campaign_fit_tags (array of strings), pitch_angle_hint (string).\n"
        "Rules:\n"
        "- reject obvious aggregators/directories/proxies.\n"
        "- promote if editorially real and useful for submissions.\n"
        "- if only login/captcha-gated music submission exists, prefer review over reject.\n"
        "- submission_path_quality is authoritative for outreach-path wording.\n"
        "- contact_email_only/contact_form_only means the station has a contact route but NO proven submission channel.\n"
        "- for contact_email_only/contact_form_only, rationale_short/editorial_summary_short must explicitly say: no confirmed submission route; contact option only.\n"
        "- do not say 'accepts submissions' or set accepts_music_submissions=true for contact_email_only/contact_form_only.\n"
        "- contact-only routes can still be useful for manual outreach; prefer review with clear wording and lower confidence.\n"
        "- explicit_submission_email requires direct music-submission wording near the email, or a dedicated local-part such as music@, submissions@, newmusic@ with submission context.\n"
        "- info@, contact@, promo@, advertising@, programming@, staff/person emails, and generic department emails are contact-only unless direct music-submission wording is present.\n"
        "- promote only for direct_music_form, gated_music_form, or strict explicit_submission_email.\n"
        "- submission_page_signal alone is not verified; keep it review unless a confirmed form/email channel is present.\n"
        "- for submission_page_signal without a confirmed form/email channel, do not say the station accepts, seeks, welcomes, or has a clear/direct submission route; say potential submission-related page signal only.\n"
        "- if FEATURES indicate sport, news/talk, or religious/christian positioning, reject unless there is overwhelming contrary evidence.\n"
        "- if FEATURES indicate a likely shared-domain theme subchannel rather than a standalone station brand, prefer review over promote.\n"
        "- deep_email_context_samples and deep_submission_context_samples may be in any language; interpret them semantically.\n"
        "- if a snippet explicitly says a specific email handles programming, playlisting, promo, or sent music, treat that as a strong submission signal.\n"
        "- keep rationale_short under 220 chars.\n"
        "- keep editorial_summary_short under 240 chars.\n"
        "- editorial_format should be a short label like 'community', 'college', 'mainstream pop', 'regional CHR', 'talk-heavy', 'specialist'.\n"
        "- style_tags and campaign_fit_tags should each contain 0-5 short lowercase tags.\n"
        "- pitch_angle_hint should be a short practical hint for outreach tone or framing.\n"
        f"\nFEATURES:\n{json.dumps(features, ensure_ascii=False)}"
    )


def _call_openai_verifier(prompt: str) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing")
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "is_real_station": {"type": "boolean"},
            "quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "decision": {"type": "string", "enum": ["promote", "review", "reject"]},
            "accepts_music_submissions": {"type": "boolean"},
            "accepts_new_artists": {"type": "boolean"},
            "reason_codes": {"type": "array", "items": {"type": "string"}},
            "rationale_short": {"type": "string"},
            "editorial_summary_short": {"type": "string"},
            "editorial_format": {"type": "string"},
            "style_tags": {"type": "array", "items": {"type": "string"}},
            "campaign_fit_tags": {"type": "array", "items": {"type": "string"}},
            "pitch_angle_hint": {"type": "string"},
        },
        "required": [
            "is_real_station",
            "quality_score",
            "confidence",
            "decision",
            "accepts_music_submissions",
            "accepts_new_artists",
            "reason_codes",
            "rationale_short",
            "editorial_summary_short",
            "editorial_format",
            "style_tags",
            "campaign_fit_tags",
            "pitch_angle_hint",
        ],
        "additionalProperties": False,
    }
    response = client.responses.create(
        model=settings.station_quality_openai_model,
        input=[{"role": "user", "content": prompt}],
        text={"format": {"type": "json_schema", "name": "station_quality_verdict", "schema": schema, "strict": True}},
    )
    return _extract_json_object(response.output_text)


def _call_gemini_verifier(prompt: str) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    base_url = (settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = (settings.station_quality_gemini_model or "gemini-2.5-flash-lite").strip()
    url = f"{base_url}/models/{model}:generateContent"
    params = {"key": settings.gemini_api_key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": int(settings.station_quality_gemini_max_output_tokens),
            "responseMimeType": "application/json",
        },
    }
    data = gemini_generate_content_json(
        url=url,
        params=params,
        json_payload=payload,
        wall_timeout_per_attempt=float(settings.gemini_http_wall_timeout_seconds),
        connect_timeout=float(settings.gemini_http_connect_timeout_seconds),
        read_timeout=float(settings.gemini_http_read_timeout_seconds),
    )
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("gemini_no_candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
    merged = "\n".join(text_parts).strip()
    return _extract_json_object(merged)


def _call_verifier(provider: str, prompt: str) -> dict[str, Any]:
    mode = (provider or "").strip().lower()
    if mode == "gemini":
        return _call_gemini_verifier(prompt)
    if mode == "openai":
        return _call_openai_verifier(prompt)
    raise RuntimeError(f"unsupported_provider={provider}")


def _persist_assessment(
    session: Session,
    station: Station,
    features: dict[str, Any],
    verdict: dict[str, Any],
    provider: str,
    estimated_usd: float,
) -> StationSubmissionAssessment:
    kind = settings.station_quality_assessment_kind
    row = session.scalar(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == station.id, StationSubmissionAssessment.assessment_kind == kind)
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )
    if row is None:
        row = StationSubmissionAssessment(station_id=station.id, assessment_kind=kind, status="active")
        session.add(row)
        session.flush()

    decision = str(verdict.get("decision") or "review").strip().lower()
    quality_score = max(0, min(100, int(verdict.get("quality_score", 50))))
    confidence = float(verdict.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    is_real_station = bool(verdict.get("is_real_station", False))
    accepts_music_submissions = bool(verdict.get("accepts_music_submissions", False))
    accepts_new_artists = bool(verdict.get("accepts_new_artists", False))
    reason_codes = verdict.get("reason_codes") if isinstance(verdict.get("reason_codes"), list) else []
    rationale = str(verdict.get("rationale_short") or "").strip()
    editorial_summary_short = str(verdict.get("editorial_summary_short") or "").strip()
    editorial_format = str(verdict.get("editorial_format") or "").strip()
    style_tags = [str(tag).strip().lower() for tag in verdict.get("style_tags", []) if str(tag).strip()] if isinstance(verdict.get("style_tags"), list) else []
    campaign_fit_tags = [str(tag).strip().lower() for tag in verdict.get("campaign_fit_tags", []) if str(tag).strip()] if isinstance(verdict.get("campaign_fit_tags"), list) else []
    pitch_angle_hint = str(verdict.get("pitch_angle_hint") or "").strip()
    submission_path_quality = str(features.get("submission_path_quality") or _submission_path_quality(features))
    if submission_path_quality in {"contact_email_only", "contact_form_only"}:
        accepts_music_submissions = False
        if submission_path_quality not in reason_codes:
            reason_codes.append(submission_path_quality)
        contact_only_note = "No confirmed submission route; contact option only."
        if not editorial_summary_short or _mentions_confirmed_submission(editorial_summary_short):
            editorial_summary_short = contact_only_note
        elif "no confirmed submission" not in editorial_summary_short.lower():
            editorial_summary_short = f"{contact_only_note} {editorial_summary_short}"[:240]
        if not rationale or "submission" in rationale.lower():
            rationale = contact_only_note
    elif submission_path_quality == "submission_page_signal":
        accepts_music_submissions = False
        if "submission_page_signal_needs_confirmation" not in reason_codes:
            reason_codes.append("submission_page_signal_needs_confirmation")
        unconfirmed_note = "Potential submission-page signal only; no confirmed submission route."
        if not editorial_summary_short or _mentions_confirmed_submission(editorial_summary_short):
            editorial_summary_short = unconfirmed_note
        elif "no confirmed submission" not in editorial_summary_short.lower():
            editorial_summary_short = f"{unconfirmed_note} {editorial_summary_short}"[:240]
        if not rationale or _mentions_confirmed_submission(rationale):
            rationale = unconfirmed_note

    has_editorial_surface = bool(
        features.get("has_stream_url")
        or int(features.get("contact_total", 0)) > 0
        or int(features.get("active_forms_total", 0)) > 0
        or int(features.get("submission_channel_total", 0)) > 0
    )

    row.is_real_station = is_real_station
    row.has_real_editorial_surface = has_editorial_surface
    row.accepts_music_submissions = accepts_music_submissions
    row.accepts_new_artists = accepts_new_artists
    row.automation_readiness = round(quality_score / 100.0, 4)
    row.risk_score = round(1.0 - confidence, 4)
    path_note = {
        "direct_music_form": "Direct music submission form found.",
        "gated_music_form": "Music submission form found, but it appears gated by login or captcha.",
        "explicit_submission_email": "Explicit submission-context email found.",
        "submission_page_signal": "Submission-page signal found, but channel needs manual confirmation.",
        "contact_email_only": "Contact email only; no confirmed music submission channel.",
        "contact_form_only": "Contact form only; no confirmed music submission channel.",
        "none": "No usable submission/contact path found.",
    }.get(submission_path_quality, f"Submission path quality: {submission_path_quality}.")
    row.notes = f"{path_note} {(editorial_summary_short or rationale)[:430]}".strip()[:500] or f"decision={decision}"
    best_route = features.get("best_submission_route") if isinstance(features.get("best_submission_route"), dict) else {}
    secondary_routes = features.get("secondary_submission_routes") if isinstance(features.get("secondary_submission_routes"), list) else []
    station.best_submission_route_type = str(best_route.get("route_type") or "") or None
    station.best_submission_route_url = str(best_route.get("url") or "") or None
    station.best_submission_route_email = str(best_route.get("email") or "") or None
    station.best_submission_route_confidence = (
        round(float(best_route.get("confidence") or 0.0), 4) if best_route else None
    )
    station.best_submission_route_reason = str(best_route.get("reason") or "")[:500] or None
    station.best_submission_route_updated_at = datetime.utcnow() if best_route else None
    station.secondary_submission_routes_json = json.dumps(secondary_routes[:5], ensure_ascii=False)
    session.add(station)
    row.evidence_json = json.dumps(
        {
            "provider": provider,
            "model": (
                settings.station_quality_gemini_model if provider == "gemini" else settings.station_quality_openai_model
            ),
            "decision": decision,
            "quality_score": quality_score,
            "confidence": confidence,
            "reason_codes": reason_codes,
            "editorial_summary_short": editorial_summary_short[:240],
            "editorial_format": editorial_format[:80],
            "style_tags": style_tags[:5],
            "campaign_fit_tags": campaign_fit_tags[:5],
            "pitch_angle_hint": pitch_angle_hint[:180],
            "submission_path_quality": submission_path_quality,
            "submission_path_note": path_note,
            "best_submission_route": best_route,
            "secondary_submission_routes": secondary_routes[:5],
            "estimated_usd": round(float(estimated_usd), 6),
            "features": features,
            "verdict": verdict,
            "evaluated_at": datetime.utcnow().isoformat() + "Z",
        },
        ensure_ascii=False,
    )
    return row


def run_station_quality_verification(
    session: Session,
    limit: int = 50,
    station_id: int | None = None,
    provider: str | None = None,
    only_unassessed: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    chosen_provider = (provider or settings.station_quality_provider or "gemini").strip().lower()
    if chosen_provider not in {"gemini", "openai"}:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": f"unsupported_provider={chosen_provider}"}
    if chosen_provider == "gemini" and not settings.gemini_api_key:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "GEMINI_API_KEY missing"}
    if chosen_provider == "openai" and not settings.openai_api_key:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "OPENAI_API_KEY missing"}

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "station_quality_budget.json",
        max_llm_calls_per_day=settings.station_quality_max_calls_per_day,
        max_daily_usd=settings.station_quality_max_daily_usd,
        input_price_per_1m=settings.station_quality_gemini_input_price_per_1m,
        output_price_per_1m=settings.station_quality_gemini_output_price_per_1m,
    )

    q = select(Station).where(Station.website_url.is_not(None))
    if station_id is not None:
        q = q.where(Station.id == station_id)
    else:
        q = q.where(Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]))
    if only_unassessed:
        assessed_ids = select(StationSubmissionAssessment.station_id).where(
            StationSubmissionAssessment.assessment_kind == settings.station_quality_assessment_kind
        )
        q = q.where(Station.id.not_in(assessed_ids))

    priority_tier_column = getattr(Station, "priority_tier", None)
    if priority_tier_column is not None:
        q = q.order_by(priority_tier_column.desc(), Station.confidence_score.desc(), Station.id.asc())
    else:
        q = q.order_by(Station.confidence_score.desc(), Station.id.asc())

    stations = session.scalars(q.limit(max(1, limit))).all()
    if not stations:
        return {"processed": 0, "saved": 0, "errors": 0, "reason": "no_candidates"}

    processed = 0
    saved = 0
    errors = 0
    skipped_budget = 0
    decisions = {"promote": 0, "review": 0, "reject": 0}
    samples: list[dict[str, Any]] = []

    for station in stations:
        features = _build_station_features(session=session, station=station)
        prompt = _build_prompt(features)
        estimated_usd = max(0.00001, _estimate_call_usd(prompt, provider=chosen_provider))
        if hasattr(budget, "try_reserve_call"):
            if not budget.try_reserve_call(estimated_usd):
                skipped_budget += 1
                continue
        else:
            if not budget.can_call_llm_today() or not budget.can_spend(estimated_usd):
                skipped_budget += 1
                continue
        processed += 1
        try:
            verdict = _call_verifier(provider=chosen_provider, prompt=prompt)
            verdict, _ = _apply_exception_rules(station=station, features=features, verdict=verdict)
            verdict = _apply_decision_guardrails(station=station, features=features, verdict=verdict)
            decision = str(verdict.get("decision") or "review").strip().lower()
            if decision not in decisions:
                decision = "review"
                verdict["decision"] = "review"
            decisions[decision] += 1
            if apply:
                _persist_assessment(
                    session=session,
                    station=station,
                    features=features,
                    verdict=verdict,
                    provider=chosen_provider,
                    estimated_usd=estimated_usd,
                )
                session.commit()
                if not hasattr(budget, "try_reserve_call"):
                    budget.register_call(estimated_usd)
                saved += 1
            if len(samples) < 10:
                samples.append(
                    {
                        "station_id": station.id,
                        "name": station.canonical_name,
                        "decision": decision,
                        "quality_score": verdict.get("quality_score"),
                        "confidence": verdict.get("confidence"),
                    }
                )
        except Exception as exc:
            errors += 1
            session.rollback()
            if len(samples) < 10:
                samples.append(
                    {
                        "station_id": station.id,
                        "name": station.canonical_name,
                        "error": str(exc)[:180],
                    }
                )

    return {
        "provider": chosen_provider,
        "assessment_kind": settings.station_quality_assessment_kind,
        "processed": processed,
        "saved": saved,
        "errors": errors,
        "skipped_budget": skipped_budget,
        "decisions": decisions,
        "samples": samples,
        "apply": apply,
        "only_unassessed": only_unassessed,
    }

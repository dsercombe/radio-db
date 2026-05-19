from __future__ import annotations

import json
import re
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.brave_keys import (
    effective_daily_limit,
    effective_monthly_limit,
    phase2_brave_answer_keys,
    pick_key,
)
from radio_db.connectors.gemini_generate import gemini_generate_content_json
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    ContactRole,
    Evidence,
    SourceType,
    Station,
    StationContact,
    StationGenre,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionChannel,
    SubmissionMethod,
)
from radio_db.services.budget import ApiUsageGuard
from radio_db.services.forms_agent import _extract_forms_fallback_payload, _extract_forms_playwright
from radio_db.services.station_quality import (
    _build_station_features,
    _compact_text,
    _domain_matches,
    _extract_json_object,
    _is_aggregator_domain,
    _is_dedicated_submission_email,
    _is_valid_discovered_email,
    _safe_domain,
)


ASSESSMENT_KIND = "route_discovery_v1"
STRONG_ROUTE_TYPES = {"direct_music_form", "gated_music_form", "explicit_submission_email", "music_director_contact"}
CONTACT_ROUTE_TYPES = {"contact_email_only", "contact_form_only"}
SUPPORTED_ROUTE_TYPES = STRONG_ROUTE_TYPES | CONTACT_ROUTE_TYPES | {"submission_page_signal", "none"}
NEGATIVE_ROUTE_HINTS = (
    "community-calendar",
    "event submission",
    "events submission",
    "submit an event",
    "psa request",
    "public service announcement",
    "contest",
    "giveaway",
    "request a song",
    "listener request",
)
DIRECT_SUBMISSION_HINTS = (
    "submit music",
    "submit your music",
    "send us your music",
    "send your songs",
    "music submission",
    "music submissions",
    "digital submissions",
    "airplay consideration",
    "for airplay",
    "get your music played",
    "get your music on radio",
    "send your tracks",
    "submit tracks",
    "email your mp3",
    "mp3 and bio",
    "upload your music",
    "upload song",
    "upload music",
    "music for airplay consideration",
    "local music submissions",
    "new music submissions",
    "submitting music",
    "submitting your music",
    "if you're submitting music",
    "if you are submitting music",
    "musik vorstellen",
    "eure musik vorstellen",
    "deine musik vorstellen",
    "schickt uns eure musik",
    "sendet uns eure musik",
    "bemusterung",
    "bemusterungen",
    "musik einreichen",
    "musikeinsendung",
    "musikeinsendungen",
    "promo mails",
    "promo mail",
    "promo-mails",
    "promo e-mails",
    "promo email",
    "promo emails",
    "songs einreichen",
    "demos einreichen",
    "demo einsenden",
    "newcomer",
)
MUSIC_DIRECTOR_HINTS = (
    "music director",
    "program director",
    "programme director",
    "new music director",
    "attn: music director",
)
URL_RE = re.compile(r"https?://[^\s)\]>\"}]+", flags=re.I)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", flags=re.I)
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([a-z0-9._%+\-]+)\s*(?:@|\[at\]|\(at\)|\sat\s)\s*([a-z0-9.\-]+\.[a-z]{2,})\b",
    flags=re.I,
)
CFEMAIL_RE = re.compile(r'data-cfemail=["\']([0-9a-fA-F]+)["\']')


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _usage_path() -> Path:
    return Path(".radio_db_state") / "api_usage.json"


def _api_usage_guard() -> ApiUsageGuard:
    return ApiUsageGuard(state_path=_usage_path(), max_brave_calls_per_month=settings.max_brave_calls_per_month)


def _can_call_brave_answer() -> bool:
    keys = phase2_brave_answer_keys()
    if not settings.enable_brave_answer or not keys:
        return False
    return _api_usage_guard().can_call_brave_answer(
        max_daily_calls=effective_daily_limit(settings.max_brave_answer_calls_per_day, len(keys)),
        max_monthly_calls=effective_monthly_limit(settings.max_brave_answer_calls_per_month, len(keys)),
    )


def _brave_answer_remaining() -> int:
    keys = phase2_brave_answer_keys()
    if not settings.enable_brave_answer or not keys:
        return 0
    usage = _api_usage_guard()
    day_remaining = effective_daily_limit(settings.max_brave_answer_calls_per_day, len(keys)) - int(
        usage.state.brave_answer_calls_day
    )
    month_remaining = effective_monthly_limit(settings.max_brave_answer_calls_per_month, len(keys)) - int(
        usage.state.brave_answer_calls_month
    )
    return max(0, min(day_remaining, month_remaining))


def _register_brave_answer_call() -> None:
    _api_usage_guard().register_brave_answer_call()


def _is_brave_answer_limit_error(result: dict[str, Any]) -> bool:
    error = str(result.get("error") or "")
    return "402" in error or "429" in error or "Payment Required" in error or "Too Many Requests" in error


def _brave_answer(prompt: str) -> dict[str, Any]:
    keys = phase2_brave_answer_keys()
    if not _can_call_brave_answer():
        return {"ok": False, "error": "phase2_brave_answer_budget_or_key_missing", "content": ""}
    usage = _api_usage_guard()
    api_key = pick_key(keys, usage.state.brave_answer_calls_month)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": "brave", "messages": [{"role": "user", "content": prompt}], "stream": False}
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.post(settings.brave_answer_base_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}:{str(exc)[:220]}", "content": ""}
    _register_brave_answer_call()
    choices = data.get("choices") if isinstance(data, dict) else None
    content = ""
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            content = str(msg.get("content") or "")
    return {"ok": True, "content": content, "usage": data.get("usage") if isinstance(data, dict) else {}}


def _official_urls(text: str, station_domain: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:)]}>")
        domain = _safe_domain(url)
        if station_domain and domain and not _domain_matches(domain, station_domain):
            continue
        if _looks_like_non_page_url(url):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _emails(text: str) -> list[str]:
    found = [e.lower().rstrip(".,;:)]}>") for e in EMAIL_RE.findall(text or "")]
    found.extend(f"{local.lower()}@{domain.lower()}" for local, domain in OBFUSCATED_EMAIL_RE.findall(text or ""))
    found.extend(_decode_cfemail(value) for value in CFEMAIL_RE.findall(text or ""))
    out: list[str] = []
    seen: set[str] = set()
    for email in found:
        if (
            "@" not in email
            or "example." in email
            or email in seen
            or len(email) > 320
            or not _is_valid_discovered_email(email)
        ):
            continue
        seen.add(email)
        out.append(email)
    return out


def _decode_cfemail(value: str) -> str:
    try:
        data = bytes.fromhex(value)
        key = data[0]
        return bytes(b ^ key for b in data[1:]).decode("utf-8", errors="replace").lower()
    except Exception:
        return ""


def _looks_like_non_page_url(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return bool(re.search(r"\.(?:avif|gif|jpe?g|mp3|mp4|ogg|pdf|png|svg|webp|wav|zip)(?:$|[?#])", path))


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = _compact_text(text).lower()
    return any(needle in lowered for needle in needles)


def _score_route_type(route_type: str) -> int:
    return {
        "direct_music_form": 100,
        "explicit_submission_email": 90,
        "music_director_contact": 82,
        "gated_music_form": 78,
        "submission_page_signal": 48,
        "contact_email_only": 32,
        "contact_form_only": 28,
        "none": 0,
    }.get(route_type, 0)


def _is_generic_contact_email(email: str) -> bool:
    local = str(email or "").split("@", 1)[0].lower()
    return local in {"info", "contact", "admin", "hello", "feedback", "webmaster", "office", "mail", "studio"}


def _is_dedicated_submission_email(email: str) -> bool:
    local = str(email or "").split("@", 1)[0].lower()
    return any(token in local for token in ("music", "submission", "submit", "promo", "bemuster", "demo"))


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(candidate.get("route_type") or ""),
        str(candidate.get("url") or "").strip().rstrip("/"),
        str(candidate.get("email") or "").strip().lower(),
    )


def _best_fallback_candidate(candidates: list[dict[str, Any]], *, allow_signal: bool = True) -> dict[str, Any]:
    preferred = list(CONTACT_ROUTE_TYPES)
    if allow_signal:
        preferred.append("submission_page_signal")
    for route_type in preferred:
        for candidate in candidates:
            if str(candidate.get("route_type") or "") == route_type and (candidate.get("url") or candidate.get("email")):
                return candidate
    return {}


def _downgrade_strong_route(
    out: dict[str, Any],
    fallback_candidates: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    replacement = _best_fallback_candidate(fallback_candidates)
    if replacement:
        out["best_route"] = replacement
        replacement_type = str(replacement.get("route_type") or "none")
        out["decision"] = "review"
        out["outreach_bucket"] = "contact_only" if replacement_type in CONTACT_ROUTE_TYPES else "needs_review"
        out["manual_review_reason"] = reason
    else:
        out["best_route"] = {"route_type": "none", "confidence": 0.0, "reason": reason}
        out["decision"] = "review"
        out["outreach_bucket"] = "needs_review"
        out["manual_review_reason"] = reason
    return out


def _route_candidate(
    *,
    route_type: str,
    source: str,
    url: str = "",
    email: str = "",
    person_name: str = "",
    role: str = "",
    evidence_snippet: str = "",
    confidence: float = 0.0,
) -> dict[str, Any]:
    return {
        "route_type": route_type if route_type in SUPPORTED_ROUTE_TYPES else "none",
        "source": source,
        "url": url[:1024],
        "email": email[:320],
        "person_name": person_name[:120],
        "role": role[:80],
        "evidence_snippet": _compact_text(evidence_snippet)[:600],
        "confidence": round(max(0.0, min(1.0, float(confidence or 0.0))), 4),
        "score": _score_route_type(route_type),
    }


def _extract_text_from_meta(meta: dict[str, Any]) -> str:
    chunks: list[str] = [str(meta.get("title") or ""), str(meta.get("url") or "")]
    for ctx in meta.get("submission_keyword_contexts") or []:
        if isinstance(ctx, dict):
            chunks.append(str(ctx.get("snippet") or ""))
    for ctx in meta.get("email_contexts") or []:
        if isinstance(ctx, dict):
            chunks.append(str(ctx.get("snippet") or ""))
    for link in meta.get("links") or []:
        if isinstance(link, dict):
            chunks.append(str(link.get("text") or ""))
            chunks.append(str(link.get("href") or ""))
    return _compact_text(" ".join(chunks))


def _visible_text_from_html(html: str, limit: int = 6000) -> str:
    text = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _verify_url(url: str, station: Station) -> dict[str, Any]:
    if not url or _looks_like_non_page_url(url):
        return {"url": url, "status": "skipped", "reason": "non_page_url", "candidates": []}
    try:
        meta = _extract_forms_playwright(url)
        mode = "playwright"
    except Exception as exc:
        try:
            html = get_text(url, timeout_seconds=15.0, max_attempts=1)
            meta = _extract_forms_fallback_payload(url=url, html=html)
            meta["_route_discovery_raw_html"] = html[:200000]
            mode = f"http_fallback:{exc.__class__.__name__}"
        except Exception as fallback_exc:
            return {
                "url": url,
                "status": "error",
                "reason": f"{fallback_exc.__class__.__name__}:{str(fallback_exc)[:180]}",
                "candidates": [],
            }
    text = _extract_text_from_meta(meta)
    lowered = text.lower()
    raw_html = str(meta.get("_route_discovery_raw_html") or "")
    visible_html_text = _visible_text_from_html(raw_html) if raw_html else ""
    if visible_html_text and len(text) < 1200:
        text = _compact_text(f"{text} {visible_html_text}")[:8000]
        lowered = text.lower()
    emails = _emails(f"{text} {raw_html}")
    candidates: list[dict[str, Any]] = []
    form_count = len(meta.get("forms") or [])
    has_submission_text = _has_any(text, DIRECT_SUBMISSION_HINTS)
    has_direct_submission = has_submission_text or any(
        token in url.lower()
        for token in (
            "submit-music",
            "music-submission",
            "music-submissions",
            "/submissions",
            "/submission",
            "artist-submission",
        )
    )
    has_music_director = _has_any(text, MUSIC_DIRECTOR_HINTS)
    has_negative = _has_any(text, NEGATIVE_ROUTE_HINTS) or any(token in url.lower() for token in ("event", "calendar", "psa"))
    if has_submission_text and form_count > 0 and not has_negative:
        candidates.append(
            _route_candidate(
                route_type="direct_music_form",
                source=mode,
                url=str(meta.get("url") or url),
                evidence_snippet=text,
                confidence=0.9,
            )
        )
    if emails:
        has_dedicated_submission_email = any(_is_dedicated_submission_email(email) for email in emails)
        for email in emails[:5]:
            email_domain = email.split("@", 1)[1] if "@" in email else ""
            official_page = _domain_matches(_safe_domain(str(meta.get("url") or url)), _safe_domain(station.website_url))
            same_domain = _domain_matches(email_domain, _safe_domain(station.website_url))
            source_backed = same_domain or (official_page and (has_direct_submission or has_music_director))
            if (has_submission_text or has_music_director) and source_backed and not has_negative:
                if has_submission_text and has_dedicated_submission_email and not _is_dedicated_submission_email(email):
                    continue
                route_type = "explicit_submission_email"
                confidence = 0.82
                if _is_generic_contact_email(email) and not has_submission_text:
                    route_type = "contact_email_only"
                    confidence = 0.42
                candidates.append(
                    _route_candidate(
                        route_type=route_type,
                        source=mode,
                        url=str(meta.get("url") or url),
                        email=email,
                        evidence_snippet=text,
                        confidence=confidence,
                    )
                )
            elif has_music_director and source_backed:
                candidates.append(
                    _route_candidate(
                        route_type="music_director_contact",
                        source=mode,
                        url=str(meta.get("url") or url),
                        email=email,
                        role="music_director",
                        evidence_snippet=text,
                        confidence=0.72,
                    )
                )
            elif source_backed:
                candidates.append(
                    _route_candidate(
                        route_type="contact_email_only",
                        source=mode,
                        url=str(meta.get("url") or url),
                        email=email,
                        evidence_snippet=text,
                        confidence=0.42,
                    )
                )
    if has_music_director and not emails and not has_direct_submission and not has_negative:
        candidates.append(
            _route_candidate(
                route_type="music_director_contact",
                source=mode,
                url=str(meta.get("url") or url),
                role="music_director",
                evidence_snippet=text,
                confidence=0.58,
            )
        )
    if form_count > 0 and not candidates and not has_negative:
        candidates.append(
            _route_candidate(
                route_type="contact_form_only",
                source=mode,
                url=str(meta.get("url") or url),
                evidence_snippet=text,
                confidence=0.35,
            )
        )
    if has_direct_submission and not candidates and not has_negative:
        candidates.append(
            _route_candidate(
                route_type="submission_page_signal",
                source=mode,
                url=str(meta.get("url") or url),
                evidence_snippet=text,
                confidence=0.48,
            )
        )
    return {
        "url": url,
        "status": "ok",
        "mode": mode,
        "final_url": str(meta.get("url") or url),
        "form_count": form_count,
        "emails": emails[:10],
        "has_direct_submission": has_direct_submission,
        "has_music_director": has_music_director,
        "has_negative": has_negative,
        "candidates": candidates,
    }


def _candidate_urls_from_existing(station: Station, old_features: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("best_submission_route",):
        route = old_features.get(key) if isinstance(old_features.get(key), dict) else {}
        if route.get("url"):
            urls.append(str(route["url"]))
    for route in old_features.get("secondary_submission_routes") or []:
        if isinstance(route, dict) and route.get("url"):
            urls.append(str(route["url"]))
    for channel in station.submissions:
        if channel.url:
            urls.append(str(channel.url))
    for form in station.forms:
        urls.append(str(form.url))
    for contact in station.contacts:
        if contact.contact_url:
            urls.append(str(contact.contact_url))
    for sample in old_features.get("playwright_interesting_url_samples") or []:
        urls.append(str(sample))
    return urls


def _candidate_urls_from_common_music_paths(station: Station) -> list[str]:
    website = str(station.website_url or "").strip()
    parsed = urlparse(website)
    if not parsed.scheme or not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    paths = (
        "",
        "submit",
        "submissions",
        "music-submissions",
        "submit-music",
        "new-music",
        "airplay",
        "bemusterung",
        "musik",
        "newcomer",
        "demo",
        "radar",
    )
    return [f"{base}/{path}".rstrip("/") + "/" for path in paths]


def _dedupe_urls(urls: list[str], station: Station, limit: int) -> list[str]:
    station_domain = _safe_domain(station.website_url)
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        clean = str(url or "").strip().rstrip(".,;:)]}>")
        if not clean.startswith(("http://", "https://")) or clean in seen or _looks_like_non_page_url(clean):
            continue
        domain = _safe_domain(clean)
        if station_domain and domain and not _domain_matches(domain, station_domain):
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _build_brave_prompts(station: Station) -> list[tuple[str, str]]:
    domain = _safe_domain(station.website_url)
    name = station.canonical_name
    return [
        (
            "site_domain",
            (
                f'Search primarily the official domain. Query: site:{domain} '
                '("submit music" OR "music submissions" OR "music director" OR airplay OR promo OR "new music"). '
                "Return official URLs, emails, names/roles, and one-line confidence. "
                "If no official route exists, say NO_OFFICIAL_ROUTE."
            ),
        ),
        (
            "music_director",
            (
                f'Search the web for the official music director, program director, promo, airplay, or new music contact for "{name}". '
                "Return official URLs, emails, names/roles, and one-line confidence. "
                "Be conservative and prefer the station's own website. If no official route exists, say NO_OFFICIAL_ROUTE."
            ),
        ),
    ]


def _call_gemini_route_discovery(prompt: str) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    base_url = (settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = (settings.station_quality_gemini_model or "gemini-2.5-flash-lite").strip()
    url = f"{base_url}/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max(1200, int(settings.station_quality_gemini_max_output_tokens)),
            "responseMimeType": "application/json",
        },
    }
    data = gemini_generate_content_json(
        url=url,
        params={"key": settings.gemini_api_key},
        json_payload=payload,
        wall_timeout_per_attempt=float(settings.gemini_http_wall_timeout_seconds),
        connect_timeout=float(settings.gemini_http_connect_timeout_seconds),
        read_timeout=float(settings.gemini_http_read_timeout_seconds),
    )
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    return _extract_json_object(text)


def _build_gemini_prompt(station: Station, old_features: dict[str, Any], discovery: dict[str, Any]) -> str:
    compact_features = {
        "station_id": station.id,
        "name": station.canonical_name,
        "status": station.status.value if isinstance(station.status, StationStatus) else str(station.status),
        "country_code": station.country_code,
        "city": station.city,
        "website_url": station.website_url,
        "confidence_score": station.confidence_score,
        "genres": [g.genre for g in station.genres[:12]],
        "old_submission_path_quality": old_features.get("submission_path_quality"),
        "old_best_submission_route": old_features.get("best_submission_route"),
        "old_quality_score": old_features.get("quality_score"),
    }
    compact_discovery = _compact_discovery_for_gemini(discovery)
    return (
        "You are a conservative phase-2 radio route discovery verifier.\n"
        "Use OLD_FEATURES plus NEW_DISCOVERY evidence. Prefer official station-domain evidence.\n"
        "Do not treat event submissions, PSA forms, listener requests, contests, station-directory submissions, images, or generic artist resources as verified music submission routes.\n"
        "A music_director_contact is valid if an official page identifies a music/program/new-music contact or gives a Music Director mailing/contact route for submissions.\n"
        "Return valid compact JSON only. No markdown, no comments, no trailing commas.\n"
        "Required keys: decision, quality_score, confidence, is_real_station, accepts_music_submissions, accepts_new_artists, outreach_bucket, best_route, secondary_routes, style_tags, campaign_fit_tags, editorial_format, pitch_angle_hint, negative_flags, manual_review_reason, rationale_short.\n"
        "decision enum: promote|review|reject|keep. outreach_bucket enum: verified_submission|music_director_contact|contact_only|needs_review|none.\n"
        "best_route object keys: route_type,url,email,person_name,role,confidence,reason. route_type enum: direct_music_form|gated_music_form|explicit_submission_email|music_director_contact|contact_form_only|contact_email_only|submission_page_signal|none.\n"
        "Keep all strings concise. If evidence is weak, choose review/contact_only/needs_review rather than promote.\n"
        f"\nOLD_FEATURES:\n{json.dumps(compact_features, ensure_ascii=False)}\n"
        f"\nNEW_DISCOVERY:\n{json.dumps(compact_discovery, ensure_ascii=False)}"
    )


def _compact_discovery_for_gemini(discovery: dict[str, Any]) -> dict[str, Any]:
    answers = []
    for item in discovery.get("brave_answers") or []:
        if not isinstance(item, dict):
            continue
        answers.append(
            {
                "query_type": item.get("query_type"),
                "ok": bool(item.get("ok")),
                "content": _compact_text(str(item.get("content") or ""))[:1200],
                "error": str(item.get("error") or "")[:200],
            }
        )
    verifications = []
    for item in discovery.get("url_verifications") or []:
        if not isinstance(item, dict):
            continue
        verifications.append(
            {
                "url": item.get("url"),
                "status": item.get("status"),
                "mode": item.get("mode"),
                "final_url": item.get("final_url"),
                "form_count": item.get("form_count"),
                "emails": (item.get("emails") or [])[:5],
                "has_direct_submission": bool(item.get("has_direct_submission")),
                "has_music_director": bool(item.get("has_music_director")),
                "has_negative": bool(item.get("has_negative")),
                "candidates": [
                    {
                        **{k: c.get(k) for k in ("route_type", "source", "url", "email", "person_name", "role", "confidence", "score")},
                        "evidence_snippet": _compact_text(str(c.get("evidence_snippet") or ""))[:450],
                    }
                    for c in (item.get("candidates") or [])[:4]
                    if isinstance(c, dict)
                ],
            }
        )
    candidates = [
        {
            **{k: c.get(k) for k in ("route_type", "source", "url", "email", "person_name", "role", "confidence", "score")},
            "evidence_snippet": _compact_text(str(c.get("evidence_snippet") or ""))[:450],
        }
        for c in (discovery.get("route_candidates") or [])[:10]
        if isinstance(c, dict)
    ]
    return {
        "brave_answers": answers,
        "candidate_urls": (discovery.get("candidate_urls") or [])[:10],
        "url_verifications": verifications,
        "route_candidates": candidates,
    }


def _normalize_verdict(verdict: dict[str, Any], fallback_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(verdict or {})
    out["decision"] = str(out.get("decision") or "review").lower()
    if out["decision"] not in {"promote", "review", "reject", "keep"}:
        out["decision"] = "review"
    try:
        out["quality_score"] = max(0, min(100, int(out.get("quality_score", 50))))
    except Exception:
        out["quality_score"] = 50
    try:
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    except Exception:
        out["confidence"] = 0.5
    best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
    if not best or str(best.get("route_type") or "none") == "none":
        best = fallback_candidates[0] if fallback_candidates else {"route_type": "none", "confidence": 0.0}
    route_type = str(best.get("route_type") or "none")
    if route_type not in SUPPORTED_ROUTE_TYPES:
        route_type = "none"
    best["route_type"] = route_type
    if route_type == "none":
        out["quality_score"] = min(int(out.get("quality_score") or 50), 50)
        if out["decision"] in {"promote", "keep"}:
            out["decision"] = "review"
    try:
        best["confidence"] = max(0.0, min(1.0, float(best.get("confidence", 0.0))))
    except Exception:
        best["confidence"] = 0.0
    out["best_route"] = best
    secondary = out.get("secondary_routes") if isinstance(out.get("secondary_routes"), list) else []
    out["secondary_routes"] = [r for r in secondary if isinstance(r, dict)][:5]
    if not out.get("outreach_bucket"):
        out["outreach_bucket"] = (
            "verified_submission"
            if route_type in {"direct_music_form", "explicit_submission_email"}
            else "music_director_contact"
            if route_type == "music_director_contact"
            else "contact_only"
            if route_type in CONTACT_ROUTE_TYPES
            else "needs_review"
            if route_type == "submission_page_signal"
            else "none"
        )
    if out["decision"] == "promote" and int(out.get("quality_score") or 0) < 60:
        out["quality_score"] = 80 if out["best_route"]["route_type"] in STRONG_ROUTE_TYPES else 65
    elif out["decision"] == "review" and int(out.get("quality_score") or 0) < 35:
        out["quality_score"] = 50
    return out


def _guard_verdict_against_discovery(
    verdict: dict[str, Any],
    discovery: dict[str, Any],
    fallback_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(verdict)
    best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
    route_type = str(best.get("route_type") or "none")
    url = str(best.get("url") or "").strip()
    reason = str(best.get("reason") or "").lower()
    verified_urls: set[str] = set()
    verified_email_pages: set[tuple[str, str]] = set()
    verified_submission_pages: set[str] = set()
    for item in discovery.get("url_verifications") or []:
        if not isinstance(item, dict) or item.get("status") != "ok":
            continue
        for key in ("url", "final_url"):
            value = str(item.get(key) or "").strip()
            if value:
                normalized_url = value.rstrip("/")
                verified_urls.add(normalized_url)
                if item.get("has_direct_submission") and not item.get("has_negative"):
                    verified_submission_pages.add(normalized_url)
                for email in item.get("emails") or []:
                    normalized_email = str(email or "").strip().lower()
                    if normalized_email:
                        verified_email_pages.add((normalized_url, normalized_email))
    if route_type in {"direct_music_form", "gated_music_form", "explicit_submission_email"}:
        best_url = str(best.get("url") or "").strip().rstrip("/")
        best_email = str(best.get("email") or "").strip().lower()
        candidate_types = {str(candidate.get("route_type") or "") for candidate in fallback_candidates}
        has_backing_candidate = any(
            str(candidate.get("route_type") or "") == route_type
            and (
                (best_url and str(candidate.get("url") or "").strip().rstrip("/") == best_url)
                or (best_email and str(candidate.get("email") or "").strip().lower() == best_email)
            )
            for candidate in fallback_candidates
        )
        if (
            not has_backing_candidate
            and route_type == "explicit_submission_email"
            and best_url
            and best_email
            and (best_url, best_email) in verified_email_pages
            and best_url in verified_submission_pages
        ):
            has_backing_candidate = True
        if route_type == "gated_music_form" and "direct_music_form" in candidate_types:
            has_backing_candidate = True
        if not has_backing_candidate:
            out = _downgrade_strong_route(
                out,
                fallback_candidates,
                "Strong route was not backed by verified Phase-2 candidates.",
            )
            best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
            route_type = str(best.get("route_type") or "none")
            url = str(best.get("url") or "").strip()
            reason = str(best.get("reason") or "").lower()
    if route_type == "explicit_submission_email" and _is_generic_contact_email(str(best.get("email") or "")):
        best_email = str(best.get("email") or "").strip().lower()
        best_url = str(best.get("url") or "").strip().rstrip("/")
        has_direct_email_context = any(
            str(candidate.get("email") or "").strip().lower() == best_email
            and (not best_url or str(candidate.get("url") or "").strip().rstrip("/") == best_url)
            and any(
                term in str(candidate.get("evidence_snippet") or "").lower()
                for term in (
                    "submission",
                    "submissions",
                    "submit",
                    "music submissions can be emailed",
                    "airplay",
                    "promo",
                    "demo",
                    "mp3",
                    "send your",
                    "send us",
                    "musikeinsend",
                    "bemuster",
                )
            )
            for candidate in fallback_candidates
        )
        if not has_direct_email_context:
            out = _downgrade_strong_route(
                out,
                fallback_candidates,
                "Generic contact email is not a verified submission route without direct submission evidence.",
            )
            best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
            route_type = str(best.get("route_type") or "none")
            url = str(best.get("url") or "").strip()
            reason = str(best.get("reason") or "").lower()
    if route_type in {"direct_music_form", "gated_music_form", "explicit_submission_email"} and (
        "old" in reason
        and ("no official route" in reason or "broken" in reason or "not found" in reason)
    ):
        out = _downgrade_strong_route(
            out,
            fallback_candidates,
            "Old strong route was not confirmed by new discovery.",
        )
        best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
        route_type = str(best.get("route_type") or "none")
        url = str(best.get("url") or "").strip()
        reason = str(best.get("reason") or "").lower()
    if route_type == "music_director_contact" and not str(best.get("email") or "").strip():
        out = _downgrade_strong_route(
            out,
            fallback_candidates,
            "Music-director signal had no direct email address; downgraded.",
        )
        best = out.get("best_route") if isinstance(out.get("best_route"), dict) else {}
        route_type = str(best.get("route_type") or "none")
        url = str(best.get("url") or "").strip()
        reason = str(best.get("reason") or "").lower()
    if route_type in STRONG_ROUTE_TYPES and url and url.rstrip("/") not in verified_urls and "broken" in reason:
        out = _downgrade_strong_route(
            out,
            fallback_candidates,
            "Strong route from model referenced a broken or unverified URL.",
        )
    return _normalize_verdict(out, fallback_candidates)


def _persist_route_discovery(
    session: Session,
    station: Station,
    *,
    old_features: dict[str, Any],
    discovery: dict[str, Any],
    verdict: dict[str, Any],
    apply: bool,
) -> StationSubmissionAssessment:
    row = session.scalar(
        select(StationSubmissionAssessment).where(
            StationSubmissionAssessment.station_id == station.id,
            StationSubmissionAssessment.assessment_kind == ASSESSMENT_KIND,
        )
    )
    if row is None:
        row = StationSubmissionAssessment(station_id=station.id, assessment_kind=ASSESSMENT_KIND, status="active")
        session.add(row)
        session.flush()
    best = verdict.get("best_route") if isinstance(verdict.get("best_route"), dict) else {}
    route_type = str(best.get("route_type") or "none")
    confidence = float(verdict.get("confidence") or 0.5)
    quality_score = int(verdict.get("quality_score") or 50)
    row.is_real_station = bool(verdict.get("is_real_station", True))
    row.has_real_editorial_surface = True
    row.accepts_music_submissions = (
        (route_type in STRONG_ROUTE_TYPES and route_type != "music_director_contact")
        or bool(verdict.get("accepts_music_submissions", False))
    )
    row.accepts_new_artists = bool(verdict.get("accepts_new_artists", row.accepts_music_submissions))
    row.automation_readiness = round(quality_score / 100.0, 4)
    row.risk_score = round(1.0 - confidence, 4)
    row.notes = str(verdict.get("rationale_short") or verdict.get("manual_review_reason") or route_type)[:500]
    row.evidence_json = _json_dumps(
        {
            "provider": "gemini",
            "assessment_kind": ASSESSMENT_KIND,
            "decision": verdict.get("decision"),
            "quality_score": quality_score,
            "confidence": confidence,
            "outreach_bucket": verdict.get("outreach_bucket"),
            "submission_path_quality": route_type,
            "best_submission_route": best,
            "secondary_submission_routes": verdict.get("secondary_routes", [])[:5],
            "style_tags": verdict.get("style_tags", [])[:5] if isinstance(verdict.get("style_tags"), list) else [],
            "campaign_fit_tags": (
                verdict.get("campaign_fit_tags", [])[:5] if isinstance(verdict.get("campaign_fit_tags"), list) else []
            ),
            "editorial_format": str(verdict.get("editorial_format") or "")[:80],
            "pitch_angle_hint": str(verdict.get("pitch_angle_hint") or "")[:180],
            "negative_flags": verdict.get("negative_flags", []) if isinstance(verdict.get("negative_flags"), list) else [],
            "manual_review_reason": str(verdict.get("manual_review_reason") or "")[:300],
            "old_features": old_features,
            "discovery": discovery,
            "verdict": verdict,
            "evaluated_at": datetime.utcnow().isoformat() + "Z",
        }
    )
    if apply and route_type != "none":
        station.best_submission_route_type = route_type
        station.best_submission_route_url = str(best.get("url") or "")[:1024] or None
        station.best_submission_route_email = str(best.get("email") or "")[:320] or None
        station.best_submission_route_confidence = round(float(best.get("confidence") or confidence), 4)
        station.best_submission_route_reason = str(best.get("reason") or row.notes or "")[:500] or None
        station.best_submission_route_updated_at = datetime.utcnow()
        station.secondary_submission_routes_json = _json_dumps(verdict.get("secondary_routes", [])[:5])
        _upsert_route_records(session, station, best, row.notes or "")
        session.add(station)
    elif apply:
        station.best_submission_route_type = None
        station.best_submission_route_url = None
        station.best_submission_route_email = None
        station.best_submission_route_confidence = None
        station.best_submission_route_reason = "No usable Phase-2 route found."
        station.best_submission_route_updated_at = datetime.utcnow()
        station.secondary_submission_routes_json = _json_dumps(verdict.get("secondary_routes", [])[:5])
        session.add(station)
    session.add(row)
    return row


def _upsert_route_records(session: Session, station: Station, route: dict[str, Any], notes: str) -> None:
    route_type = str(route.get("route_type") or "")
    url = str(route.get("url") or "").strip() or None
    email = str(route.get("email") or "").strip().lower() or None
    if route_type in STRONG_ROUTE_TYPES | CONTACT_ROUTE_TYPES and (url or email):
        method = SubmissionMethod.EMAIL if email and route_type != "direct_music_form" else SubmissionMethod.FORM
        exists = session.scalar(
            select(SubmissionChannel).where(
                SubmissionChannel.station_id == station.id,
                SubmissionChannel.method == method,
                SubmissionChannel.url == url,
                SubmissionChannel.email == email,
            )
        )
        if exists is None:
            session.add(
                SubmissionChannel(
                    station_id=station.id,
                    method=method,
                    url=url,
                    email=email,
                    requirements=f"route_discovery_v1: {notes}"[:1000],
                    accepts_newcomers=route_type in STRONG_ROUTE_TYPES,
                )
            )
    if route_type == "music_director_contact" and (email or url):
        name = str(route.get("person_name") or "")[:255] or None
        exists_contact = session.scalar(
            select(StationContact).where(
                StationContact.station_id == station.id,
                StationContact.name == name,
                StationContact.email == email,
                StationContact.contact_url == url,
            )
        )
        if exists_contact is None:
            session.add(
                StationContact(
                    station_id=station.id,
                    name=name,
                    role=ContactRole.MUSIC_DIRECTOR,
                    email=email,
                    contact_url=url,
                    notes=f"route_discovery_v1: {notes}"[:1000],
                    confidence=float(route.get("confidence") or 0.6),
                )
            )


def _store_evidence(session: Session, station: Station, source_id: str, payload: dict[str, Any], confidence: float = 0.5) -> None:
    session.add(
        Evidence(
            station_id=station.id,
            source_type=SourceType.BRAVE_SEARCH,
            source_url=station.website_url,
            source_id=source_id,
            raw_title=f"Route discovery: {station.canonical_name}"[:500],
            raw_snippet=str(payload.get("content") or payload.get("summary") or "")[:2000],
            extracted_payload_json=_json_dumps(payload),
            confidence=confidence,
        )
    )


def route_discovery_candidates(
    session: Session,
    limit: int = 50,
    min_confidence: float = 0.6,
    min_quality_score: int = 60,
) -> list[dict[str, Any]]:
    latest_quality = (
        select(func.max(StationSubmissionAssessment.id))
        .where(
            StationSubmissionAssessment.station_id == Station.id,
            StationSubmissionAssessment.assessment_kind == "llm_quality_v1",
        )
        .correlate(Station)
        .scalar_subquery()
    )
    latest_route_discovery = (
        select(func.max(StationSubmissionAssessment.id))
        .where(
            StationSubmissionAssessment.station_id == Station.id,
            StationSubmissionAssessment.assessment_kind == ASSESSMENT_KIND,
        )
        .correlate(Station)
        .scalar_subquery()
    )
    q = (
        select(Station, StationSubmissionAssessment)
        .join(StationSubmissionAssessment, StationSubmissionAssessment.id == latest_quality)
        .where(
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
            Station.website_url.is_not(None),
            Station.confidence_score >= min_confidence,
            latest_route_discovery.is_(None),
        )
        .order_by(Station.confidence_score.desc(), Station.id.asc())
        .limit(max(1, limit * 4))
    )
    out: list[dict[str, Any]] = []
    for station, assessment in session.execute(q).all():
        if _is_aggregator_domain(station.website_url):
            continue
        if station.best_submission_route_type in STRONG_ROUTE_TYPES:
            continue
        try:
            payload = json.loads(assessment.evidence_json or "{}")
        except Exception:
            payload = {}
        quality_score = int(payload.get("quality_score") or 0)
        path = str(payload.get("submission_path_quality") or station.best_submission_route_type or "none")
        if quality_score < min_quality_score and station.best_submission_route_type not in CONTACT_ROUTE_TYPES:
            continue
        if path not in {"none", "unknown", "submission_page_signal", "contact_email_only", "contact_form_only"} and (
            station.best_submission_route_type not in CONTACT_ROUTE_TYPES
        ):
            continue
        genres = [g.genre for g in station.genres[:12]]
        out.append(
            {
                "station_id": station.id,
                "name": station.canonical_name,
                "website_url": station.website_url,
                "status": station.status.value if isinstance(station.status, StationStatus) else str(station.status),
                "confidence_score": station.confidence_score,
                "quality_score": quality_score,
                "path_quality": path,
                "best_submission_route_type": station.best_submission_route_type,
                "genres": genres,
            }
        )
        if len(out) >= limit:
            break
    return out


def run_route_discovery_for_station(
    session: Session,
    station_id: int,
    *,
    max_brave_calls: int = 2,
    max_pages: int = 8,
    apply: bool = False,
) -> dict[str, Any]:
    station = session.get(Station, station_id)
    if station is None:
        return {"station_id": station_id, "status": "error", "error": "station_not_found"}
    old_features = _build_station_features(session=session, station=station, include_deep_pass=False)
    station_domain = _safe_domain(station.website_url)
    brave_answers: list[dict[str, Any]] = []
    answer_urls: list[str] = []
    answer_emails: list[str] = []
    for label, prompt in _build_brave_prompts(station)[: max(0, max_brave_calls)]:
        result = _brave_answer(prompt)
        result["query_type"] = label
        brave_answers.append(result)
        content = str(result.get("content") or "")
        answer_urls.extend(_official_urls(content, station_domain))
        answer_emails.extend(_emails(content))
        _store_evidence(session, station, f"{ASSESSMENT_KIND}:{label}", result, confidence=0.55 if result.get("ok") else 0.1)
        if _is_brave_answer_limit_error(result):
            break
    ok_brave_answers = sum(1 for item in brave_answers if item.get("ok"))
    if max_brave_calls > 0 and ok_brave_answers == 0 and any(_is_brave_answer_limit_error(item) for item in brave_answers):
        session.rollback()
        return {
            "station_id": station.id,
            "name": station.canonical_name,
            "status": "error",
            "error": "brave_answer_unavailable_or_rate_limited",
            "applied": False,
            "brave_calls": 0,
            "brave_answers": brave_answers,
        }
    existing_urls = _candidate_urls_from_existing(station, old_features)
    common_music_urls = _candidate_urls_from_common_music_paths(station)
    urls = _dedupe_urls(answer_urls + existing_urls + common_music_urls, station, max_pages)
    verifications = [_verify_url(url, station) for url in urls]
    candidates: list[dict[str, Any]] = []
    for verification in verifications:
        candidates.extend([c for c in verification.get("candidates", []) if isinstance(c, dict)])
    answer_text = _compact_text(
        " ".join(str(item.get("content") or "") for item in brave_answers if isinstance(item, dict))
    )
    has_answer_submission_context = _has_any(answer_text, DIRECT_SUBMISSION_HINTS) or _has_any(
        answer_text, MUSIC_DIRECTOR_HINTS
    )
    for email in answer_emails:
        email_domain = email.split("@", 1)[1] if "@" in email else ""
        if _domain_matches(email_domain, station_domain):
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
                    source="brave_answer",
                    email=email,
                    evidence_snippet=answer_text[:600] or "Email extracted from Brave Answer result",
                    confidence=0.58 if route_type == "explicit_submission_email" else 0.38,
                )
            )
    candidates = sorted(
        {(_json_dumps({k: c.get(k) for k in ("route_type", "url", "email")})): c for c in candidates}.values(),
        key=lambda c: (int(c.get("score") or 0), float(c.get("confidence") or 0.0)),
        reverse=True,
    )
    discovery = {
        "brave_answers": brave_answers,
        "candidate_urls": urls,
        "url_verifications": verifications,
        "route_candidates": candidates[:12],
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
        apply=apply,
    )
    session.commit()
    return {
        "station_id": station.id,
        "name": station.canonical_name,
        "status": "ok",
        "assessment_id": assessment.id,
        "applied": apply,
        "gemini_error": gemini_error,
        "brave_calls": sum(1 for item in brave_answers if item.get("ok")),
        "verified_urls": len(verifications),
        "route_candidates": candidates[:5],
        "verdict": {
            "decision": verdict.get("decision"),
            "quality_score": verdict.get("quality_score"),
            "confidence": verdict.get("confidence"),
            "outreach_bucket": verdict.get("outreach_bucket"),
            "best_route": verdict.get("best_route"),
        },
    }


def run_route_discovery_batch(
    session: Session,
    *,
    limit: int = 5,
    station_id: int | None = None,
    max_brave_calls: int = 2,
    max_pages: int = 8,
    apply: bool = False,
    min_confidence: float = 0.6,
    min_quality_score: int = 60,
) -> dict[str, Any]:
    if station_id is not None:
        station_ids = [station_id]
    else:
        station_ids = [
            int(row["station_id"])
            for row in route_discovery_candidates(
                session,
                limit=limit,
                min_confidence=min_confidence,
                min_quality_score=min_quality_score,
            )
        ]
    results = []
    stop_reason = ""
    for sid in station_ids[: max(1, limit)]:
        station_brave_calls = max(0, max_brave_calls)
        if station_brave_calls > 0:
            remaining = _brave_answer_remaining()
            if remaining <= 0:
                stop_reason = "brave_answer_quota_exhausted"
                break
            station_brave_calls = min(station_brave_calls, remaining)
        results.append(
            run_route_discovery_for_station(
                session,
                sid,
                max_brave_calls=station_brave_calls,
                max_pages=max_pages,
                apply=apply,
            )
        )
    summary: dict[str, int] = {}
    for result in results:
        route = str(((result.get("verdict") or {}).get("best_route") or {}).get("route_type") or "error")
        summary[route] = summary.get(route, 0) + 1
    return {
        "processed": len(results),
        "apply": apply,
        "stop_reason": stop_reason,
        "brave_answer_remaining": _brave_answer_remaining(),
        "summary": summary,
        "results": results,
    }

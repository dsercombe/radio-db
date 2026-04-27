from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from openai import OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    Evidence,
    FormStatus,
    FormType,
    Station,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionMethod,
)
from radio_db.services.budget import CostGuard


AGGREGATOR_DOMAIN_HINTS = (
    "radio.net",
    "onlineradiobox",
    "mytuner",
    "tunein",
    "streema",
    "radio.garden",
    "zeno.fm",
    "liveonlineradio",
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
    return any(hint in lowered for hint in DEEP_SUBMISSION_HINTS)


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
        lowered = html.lower()
        if any(h in lowered for h in DEEP_SUBMISSION_HINTS):
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
            if _context_has_submission_signal(ctx["snippet"]):
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


def _apply_decision_guardrails(station: Station, features: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    out = dict(verdict)
    decision = str(out.get("decision") or "review").strip().lower()
    quality_score = max(0, min(100, int(out.get("quality_score", 50))))
    confidence = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    reason_codes = _normalize_reason_codes(out)

    active_music_form = int(features.get("active_music_form_count", 0)) > 0
    gated_music_form = int(features.get("gated_music_form_count", 0)) > 0
    submission_email = int(features.get("submission_channel_email_count", 0)) > 0
    contact_email = int(features.get("contact_with_email_count", 0)) > 0
    contextual_submission_email = int(features.get("contextual_submission_email_count", 0)) > 0
    newcomer_signal = bool(features.get("newcomer_signal"))
    is_real_station = bool(out.get("is_real_station", False))
    is_aggregator = bool(features.get("is_aggregator_domain"))
    has_blocker = any(code in PROMOTE_BLOCKER_REASON_CODES for code in reason_codes)
    topical_exclusion_labels = [str(x) for x in (features.get("topical_exclusion_labels") or [])]
    likely_subchannel = bool(features.get("likely_shared_domain_subchannel"))

    strong_outreach_path = (
        active_music_form
        or contextual_submission_email
        or (gated_music_form and (submission_email or contact_email or contextual_submission_email))
        or (submission_email and contact_email)
    )

    if topical_exclusion_labels and decision != "reject":
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
        and (
            (active_music_form and confidence >= 0.25)
            or (confidence >= 0.6 and strong_outreach_path)
        )
        and (active_music_form or strong_outreach_path)
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
        and (gated_music_form or newcomer_signal or submission_email)
    ):
        out["decision"] = "promote"
        out["accepts_music_submissions"] = True
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", False) or newcomer_signal)
        reason_codes.append("promote_guardrail:contextual_submission_email")
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
        and (submission_email or contact_email)
    ):
        out["decision"] = "review"
        out["accepts_music_submissions"] = True
        out["accepts_new_artists"] = bool(out.get("accepts_new_artists", False) or newcomer_signal)
        reason_codes.append("review_guardrail:gated_submission_path")
        out["reason_codes"] = reason_codes
        return out

    out["reason_codes"] = reason_codes
    return out


def _build_station_features(session: Session, station: Station) -> dict[str, Any]:
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

    active_forms = [f for f in forms if f.status == FormStatus.ACTIVE]
    high_value_forms = [
        f
        for f in active_forms
        if f.form_type in {FormType.MUSIC_SUBMISSION, FormType.NEWCOMER, FormType.ARTIST_UPLOAD}
    ]
    gated_music_forms = [
        f
        for f in forms
        if f.status in {FormStatus.LOGIN_REQUIRED, FormStatus.CAPTCHA_PRESENT}
        and f.form_type in {FormType.MUSIC_SUBMISSION, FormType.NEWCOMER, FormType.ARTIST_UPLOAD}
    ]
    form_methods = [s for s in submissions if s.method == SubmissionMethod.FORM]
    email_methods = [s for s in submissions if s.method == SubmissionMethod.EMAIL]
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
        "priority_tier": int(station.priority_tier or 0),
        "station_confidence_score": float(station.confidence_score or 0.0),
        "genre_samples": genres[:10],
        "same_domain_station_count": domain_count,
        "likely_shared_domain_subchannel": likely_shared_domain_subchannel,
        "topical_exclusion_labels": topical_exclusion_labels,
        "submission_channel_form_count": len(form_methods),
        "submission_channel_email_count": len(email_methods),
        "submission_channel_total": len(submissions),
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
    }
    should_run_deep_pass = bool(station.website_url) and not bool(features["is_aggregator_domain"]) and (
        features["active_music_form_count"] > 0
        or features["gated_music_form_count"] > 0
        or features["submission_channel_email_count"] > 0
        or features["playwright_interesting_url_count"] > 0
    )
    if should_run_deep_pass:
        features = _run_deep_pass(station=station, features=features)
    return features


def _build_prompt(features: dict[str, Any]) -> str:
    return (
        "You are a radio-station quality verifier for music pitching.\n"
        "Classify the station based on the provided FEATURES JSON only.\n"
        "Return STRICT JSON with keys:\n"
        "is_real_station (bool), quality_score (int 0-100), confidence (float 0-1),\n"
        "decision ('promote'|'review'|'reject'), accepts_music_submissions (bool),\n"
        "accepts_new_artists (bool), reason_codes (array of strings), rationale_short (string).\n"
        "Rules:\n"
        "- reject obvious aggregators/directories/proxies.\n"
        "- promote if editorially real and useful for submissions.\n"
        "- if only login/captcha-gated music submission exists, prefer review over reject.\n"
        "- if a valid station email exists but no form, prefer review over reject unless clearly irrelevant.\n"
        "- if FEATURES indicate sport, news/talk, or religious/christian positioning, reject unless there is overwhelming contrary evidence.\n"
        "- if FEATURES indicate a likely shared-domain theme subchannel rather than a standalone station brand, prefer review over promote.\n"
        "- deep_email_context_samples and deep_submission_context_samples may be in any language; interpret them semantically.\n"
        "- if a snippet explicitly says a specific email handles programming, playlisting, promo, or sent music, treat that as a strong submission signal.\n"
        "- keep rationale_short under 220 chars.\n"
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
    data: dict[str, Any] = {}
    retry_statuses = {429, 500, 502, 503, 504}
    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        last_status: int | None = None
        last_error: str | None = None
        for attempt in range(4):
            try:
                resp = client.post(url, params=params, json=payload, headers={"Content-Type": "application/json"})
                last_status = resp.status_code
                if resp.status_code in retry_statuses and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                last_status = exc.response.status_code
                last_error = exc.response.text[:300]
                if last_status in retry_statuses and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"gemini_http_error status={last_status} body={last_error}") from exc
            except httpx.HTTPError as exc:
                last_error = exc.__class__.__name__
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"gemini_transport_error error={last_error}") from exc
        else:
            raise RuntimeError(f"gemini_retry_exhausted status={last_status} error={last_error}")
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
    row.notes = rationale[:500] or f"decision={decision}"
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

    stations = session.scalars(q.order_by(Station.priority_tier.desc(), Station.confidence_score.desc()).limit(max(1, limit))).all()
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
        if not budget.try_reserve_call(estimated_usd):
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

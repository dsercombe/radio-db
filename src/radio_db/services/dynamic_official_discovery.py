from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.models.entities import (
    EnrichmentFinding,
    EnrichmentRun,
    Evidence,
    SourceType,
    Station,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionChannel,
    SubmissionMethod,
)
from radio_db.services.route_discovery import (
    CONTACT_ROUTE_TYPES,
    STRONG_ROUTE_TYPES,
    _route_candidate,
    _call_gemini_route_discovery,
    _compact_discovery_for_gemini,
    _guard_verdict_against_discovery,
    _json_dumps,
    _normalize_verdict,
    _safe_domain,
    _verify_url,
)
from radio_db.services.station_quality import (
    _build_station_features,
    _compact_text,
    _domain_matches,
    _extract_json_object,
    _is_dedicated_submission_email,
)


ASSESSMENT_KIND = "phase3_dynamic_route_v1"
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
SKIP_EXTENSIONS = (
    ".avif",
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
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".wav",
    ".woff",
    ".woff2",
    ".zip",
)


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.headings: list[str] = []
        self.title = ""
        self.meta_description = ""
        self._tag_stack: list[str] = []
        self._current_link: dict[str, str] | None = None
        self._current_text: list[str] = []
        self._title_text: list[str] = []
        self._heading_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        self._tag_stack.append(tag)
        if tag == "a" and attrs_dict.get("href"):
            self._current_link = {"href": attrs_dict["href"], "text": ""}
            self._current_text = []
        elif tag == "meta" and attrs_dict.get("name", "").lower() == "description":
            self.meta_description = attrs_dict.get("content", "")[:500]
        elif tag in {"h1", "h2", "h3"}:
            self._heading_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_link is not None:
            self._current_link["text"] = _compact_text(" ".join(self._current_text))[:180]
            self.links.append(self._current_link)
            self._current_link = None
            self._current_text = []
        elif tag == "title":
            self.title = _compact_text(" ".join(self._title_text))[:300]
        elif tag in {"h1", "h2", "h3"} and self._heading_text is not None:
            value = _compact_text(" ".join(self._heading_text))
            if value:
                self.headings.append(value[:220])
            self._heading_text = None
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._current_link is not None:
            self._current_text.append(data)
        if self._tag_stack and self._tag_stack[-1] == "title":
            self._title_text.append(data)
        if self._heading_text is not None:
            self._heading_text.append(data)


def _load_json(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _same_site_url(base_url: str, href: str) -> str:
    url = urljoin(base_url, str(href or "").strip()).split("#", 1)[0].rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return ""
    lowered = url.lower()
    if any(
        token in lowered
        for token in (
            "contest",
            "concours",
            "gewinnspiel",
            "jeux-concours",
            "reglement-general-des-jeux",
            "sweepstake",
            "giveaway",
            "advertis",
            "werbung",
            "jobs",
            "career",
        )
    ):
        return ""
    path = parsed.path.lower()
    positive_path_terms = (
        "submit",
        "submission",
        "demo",
        "airplay",
        "music",
        "musik",
        "contact",
        "kontakt",
        "staff",
        "team",
        "about",
        "program",
        "playlist",
    )
    if any(token in path for token in ("/news/", "/article/", "/blog/")) and not any(
        term in path for term in positive_path_terms
    ):
        return ""
    base_domain = _safe_domain(base_url)
    if base_domain and not _domain_matches(_safe_domain(url), base_domain):
        return ""
    return url


def _parse_structure(base_url: str, html: str) -> dict[str, Any]:
    parser = _StructureParser()
    parser.feed(html or "")
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.links:
        url = _same_site_url(base_url, item.get("href", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "anchor": item.get("text", "")[:180]})
    emails = sorted(set(e.lower() for e in EMAIL_RE.findall(html or "")))[:30]
    return {
        "url": base_url,
        "title": parser.title,
        "meta_description": parser.meta_description,
        "headings": parser.headings[:25],
        "emails": emails,
        "links": links[:160],
    }


def _sitemap_urls(website_url: str, limit: int) -> list[str]:
    parsed = urlparse(website_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    try:
        raw = get_text(sitemap_url, timeout_seconds=12.0, max_attempts=1)
        root = ElementTree.fromstring(raw.encode("utf-8"))
    except Exception:
        return []
    urls: list[str] = []
    for node in root.iter():
        if node.tag.endswith("loc") and node.text:
            url = _same_site_url(website_url, node.text)
            if url:
                urls.append(url)
        if len(urls) >= limit:
            break
    return list(dict.fromkeys(urls))


def collect_site_structure(station: Station, *, max_links: int = 120) -> dict[str, Any]:
    fetch_error = ""
    try:
        html = get_text(station.website_url or "", timeout_seconds=20.0, max_attempts=1)
        structure = _parse_structure(station.website_url or "", html)
    except Exception as exc:
        fetch_error = f"{exc.__class__.__name__}:{str(exc)[:220]}"
        structure = {
            "url": station.website_url,
            "title": station.canonical_name,
            "meta_description": "",
            "headings": [],
            "emails": [],
            "links": [],
        }
    sitemap = _sitemap_urls(station.website_url or "", limit=max(20, max_links // 2))
    existing_urls = [
        str(channel.url)
        for channel in station.submissions
        if channel.url and _same_site_url(station.website_url or "", str(channel.url))
    ]
    if station.best_submission_route_url and _same_site_url(station.website_url or "", station.best_submission_route_url):
        existing_urls.insert(0, str(station.best_submission_route_url))
    merged: dict[str, dict[str, str]] = {item["url"]: item for item in structure["links"]}
    for url in sitemap + existing_urls:
        merged.setdefault(url, {"url": url, "anchor": ""})
    structure["links"] = list(merged.values())[:max_links]
    structure["sitemap_urls_sample"] = sitemap[:30]
    structure["known_submission_urls"] = existing_urls[:20]
    structure["fetch_error"] = fetch_error
    return structure


def _build_router_prompt(station: Station, structure: dict[str, Any], max_urls: int) -> str:
    compact = {
        "station": station.canonical_name,
        "country": station.country_code,
        "language": station.language,
        "domain": _safe_domain(station.website_url),
        "homepage": station.website_url,
        "title": structure.get("title"),
        "meta_description": structure.get("meta_description"),
        "headings": structure.get("headings", [])[:18],
        "emails": structure.get("emails", [])[:12],
        "links": structure.get("links", [])[:120],
    }
    return (
        "You are a multilingual official-site router for radio music promotion research.\n"
        "Given only site structure, choose official internal pages likely to contain music submission, demo, promo, airplay, artist/newcomer, playlist, music director, programming contact, or FAQ policy evidence.\n"
        "Be language-agnostic and infer branded concepts such as radar, découverte, bemusterung, talent, demo, new music, local artists, playlist, contact, team, FAQ.\n"
        "Avoid events, contests, jobs, ads, listener song requests, newsletter, and store pages unless they also look like artist submission policy pages.\n"
        "Return compact JSON only with keys priority_urls, skip_urls, rationale_short.\n"
        "priority_urls: array of objects {url, reason, expected_signal, confidence}. Use full URLs from input. "
        f"Return at most {max_urls} priority URLs.\n"
        f"SITE_STRUCTURE:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def _rank_candidate_pages(station: Station, structure: dict[str, Any], *, max_urls: int) -> dict[str, Any]:
    try:
        verdict = _call_gemini_route_discovery(_build_router_prompt(station, structure, max_urls=max_urls))
    except Exception as exc:
        fallback = _local_rank_candidate_pages(structure, max_urls=max_urls)
        fallback["rationale_short"] = f"Gemini router fallback after {exc.__class__.__name__}."
        fallback["router_error"] = f"{exc.__class__.__name__}:{str(exc)[:220]}"
        return fallback
    return _normalize_router_result(station, verdict, max_urls=max_urls)


def _normalize_router_result(station: Station, verdict: dict[str, Any], *, max_urls: int) -> dict[str, Any]:
    priority = []
    for item in verdict.get("priority_urls") or []:
        if not isinstance(item, dict):
            continue
        url = _same_site_url(station.website_url or "", str(item.get("url") or ""))
        if url:
            raw_confidence = item.get("confidence")
            try:
                confidence = float(raw_confidence or 0.4)
            except Exception:
                confidence = {"high": 0.75, "medium": 0.55, "low": 0.35}.get(str(raw_confidence or "").lower(), 0.4)
            priority.append(
                {
                    "url": url,
                    "reason": str(item.get("reason") or "")[:220],
                    "expected_signal": str(item.get("expected_signal") or "")[:120],
                    "confidence": confidence,
                }
            )
    return {
        "priority_urls": priority[:max_urls],
        "skip_urls": verdict.get("skip_urls", [])[:30] if isinstance(verdict.get("skip_urls"), list) else [],
        "rationale_short": str(verdict.get("rationale_short") or "")[:500],
    }


def _local_rank_candidate_pages(structure: dict[str, Any], *, max_urls: int) -> dict[str, Any]:
    soft_terms = (
        "submit",
        "submission",
        "music",
        "musik",
        "demo",
        "airplay",
        "promo",
        "artist",
        "newcomer",
        "talent",
        "radar",
        "playlist",
        "program",
        "programme",
        "contact",
        "kontakt",
        "about",
        "team",
        "staff",
        "faq",
        "help",
        "bemuster",
        "envia",
        "enviar",
        "musica",
        "musique",
        "contatti",
        "contato",
        "contacto",
    )
    hard_negative = (
        "event",
        "events",
        "calendar",
        "jobs",
        "career",
        "advertis",
        "contest",
        "concours",
        "gewinnspiel",
        "jeux-concours",
        "giveaway",
        "shop",
    )
    scored: list[tuple[float, str, str]] = []
    for item in structure.get("links") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        anchor = str(item.get("anchor") or "")
        text = f"{url} {anchor}".lower()
        score = 0.0
        for term in soft_terms:
            if term in text:
                score += 1.0
        for term in hard_negative:
            if term in text:
                score -= 2.0
        if score > 0:
            scored.append((score, url, anchor))
    scored.sort(reverse=True)
    return {
        "priority_urls": [
            {
                "url": url,
                "reason": f"Local fallback structure signal from link text/path: {anchor or url}",
                "expected_signal": "possible music submission/contact/FAQ policy page",
                "confidence": min(0.75, 0.25 + score / 10.0),
            }
            for score, url, anchor in scored[:max_urls]
        ],
        "skip_urls": [],
        "rationale_short": "Local structure fallback ranking.",
    }


def _fallback_verdict_from_candidates(candidates: list[dict[str, Any]], error: str) -> dict[str, Any]:
    best = candidates[0] if candidates else {"route_type": "none", "confidence": 0.0, "reason": "No route candidates found."}
    route_type = str(best.get("route_type") or "none")
    confidence = float(best.get("confidence") or 0.4)
    return _normalize_verdict(
        {
            "decision": "promote" if route_type in STRONG_ROUTE_TYPES else "review",
            "quality_score": 82 if route_type in STRONG_ROUTE_TYPES else 60 if route_type in CONTACT_ROUTE_TYPES else 50,
            "confidence": confidence,
            "is_real_station": True,
            "accepts_music_submissions": route_type in STRONG_ROUTE_TYPES,
            "accepts_new_artists": route_type in STRONG_ROUTE_TYPES,
            "outreach_bucket": "verified_submission"
            if route_type in STRONG_ROUTE_TYPES
            else "contact_only"
            if route_type in CONTACT_ROUTE_TYPES
            else "needs_review",
            "best_route": best,
            "secondary_routes": candidates[1:6],
            "manual_review_reason": f"Gemini final verdict fallback after {error}.",
            "rationale_short": f"Fallback selected best verified candidate after Gemini error: {route_type}.",
        },
        candidates,
    )


def _is_generic_email(email: str) -> bool:
    local = str(email or "").split("@", 1)[0].lower()
    return local in {
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
        "sales",
        "studio",
        "webmaster",
    }


def _guard_final_verdict(verdict: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    best = verdict.get("best_route") if isinstance(verdict.get("best_route"), dict) else {}
    route_type = str(best.get("route_type") or "none")
    email = str(best.get("email") or "").strip().lower()
    direct_context_terms = (
        "submit",
        "submission",
        "submissions",
        "send your",
        "send us",
        "send albums",
        "send songs",
        "forward all material",
        "material",
        "mp3",
        "airplay",
        "promo",
        "demo",
        "music inquiries",
        "music submissions can be emailed",
        "musikanfragen",
        "musikeinsend",
        "bemuster",
    )
    has_email_direct_context = False
    if email:
        best_url = str(best.get("url") or "").strip().rstrip("/")
        for candidate in candidates:
            cemail = str(candidate.get("email") or "").strip().lower()
            curl = str(candidate.get("url") or "").strip().rstrip("/")
            snippet = str(candidate.get("evidence_snippet") or "").lower()
            if cemail == email and (not best_url or curl == best_url) and any(term in snippet for term in direct_context_terms):
                has_email_direct_context = True
                break
    if route_type == "explicit_submission_email" and email and not _is_dedicated_submission_email(email):
        if has_email_direct_context:
            return verdict
        fallback = None
        for candidate in candidates:
            ctype = str(candidate.get("route_type") or "")
            cemail = str(candidate.get("email") or "").strip().lower()
            if ctype in STRONG_ROUTE_TYPES and cemail and _is_dedicated_submission_email(cemail):
                fallback = candidate
                break
        out = dict(verdict)
        if fallback is not None:
            out["best_route"] = fallback
            out["outreach_bucket"] = "verified_submission"
            out["manual_review_reason"] = "Generic staff email replaced by a dedicated submission email."
        else:
            best["route_type"] = "contact_email_only"
            best["confidence"] = min(float(best.get("confidence") or 0.42), 0.42)
            best["reason"] = "Downgraded: person/staff email is not a confirmed submission route without dedicated submission context."
            out["best_route"] = best
            out["decision"] = "review"
            out["quality_score"] = min(int(out.get("quality_score") or 50), 50)
            out["outreach_bucket"] = "contact_only"
            out["manual_review_reason"] = str(best["reason"])
        return _normalize_verdict(out, candidates)
    if (
        route_type in {"explicit_submission_email", "music_director_contact"}
        and email
        and _is_generic_email(email)
        and not has_email_direct_context
    ):
        fallback = None
        for candidate in candidates:
            ctype = str(candidate.get("route_type") or "")
            cemail = str(candidate.get("email") or "").strip().lower()
            if ctype in STRONG_ROUTE_TYPES and cemail and not _is_generic_email(cemail):
                fallback = candidate
                break
        out = dict(verdict)
        if fallback is not None:
            out["best_route"] = fallback
            out["outreach_bucket"] = "verified_submission" if fallback.get("route_type") == "explicit_submission_email" else "music_director_contact"
            out["manual_review_reason"] = "Generic strong-route email replaced by a more specific candidate."
        else:
            best["route_type"] = "contact_email_only"
            best["confidence"] = min(float(best.get("confidence") or 0.42), 0.42)
            best["reason"] = "Downgraded: generic email is not a confirmed submission route without direct, specific evidence."
            out["best_route"] = best
            out["decision"] = "review"
            out["quality_score"] = min(int(out.get("quality_score") or 50), 50)
            out["outreach_bucket"] = "contact_only"
            out["manual_review_reason"] = str(best["reason"])
        return _normalize_verdict(out, candidates)
    return verdict


def _supplement_route_candidates(verifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for verification in verifications:
        if not isinstance(verification, dict) or verification.get("status") != "ok":
            continue
        url = str(verification.get("final_url") or verification.get("url") or "")
        lowered_url = url.lower()
        if any(token in lowered_url for token in ("contest", "concours", "gewinnspiel", "jeux-concours", "giveaway")):
            continue
        snippets = " ".join(
            str(candidate.get("evidence_snippet") or "")
            for candidate in verification.get("candidates", []) or []
            if isinstance(candidate, dict)
        ).lower()
        music_context = any(
            token in lowered_url or token in snippets
            for token in (
                "music",
                "song",
                "airplay",
                "playlist",
                "promo",
                "demo",
                "track",
                "musik",
                "musique",
                "bemuster",
            )
        )
        non_music_submission = any(
            token in lowered_url or token in snippets
            for token in (
                "story",
                "stories",
                "podcast",
                "news",
                "community event",
                "what's on",
                "event",
                "psa",
            )
        )
        if non_music_submission and not music_context:
            continue
        existing_types = {
            str(candidate.get("route_type") or "")
            for candidate in verification.get("candidates", [])
            if isinstance(candidate, dict)
        }
        if (
            verification.get("has_direct_submission")
            and not (existing_types & STRONG_ROUTE_TYPES)
            and (
                int(verification.get("form_count") or 0) > 0
                or any(token in lowered_url or token in snippets for token in ("upload-mp3", "upload song", "upload your music"))
            )
        ):
            out.append(
                _route_candidate(
                    route_type="direct_music_form",
                    source=str(verification.get("mode") or "phase3_dynamic"),
                    url=url,
                    evidence_snippet="Official page has direct music upload/submission form language.",
                    confidence=0.82,
                )
            )
        if verification.get("has_direct_submission") and not (existing_types & STRONG_ROUTE_TYPES):
            out.append(
                _route_candidate(
                    route_type="submission_page_signal",
                    source=str(verification.get("mode") or "phase3_dynamic"),
                    url=url,
                    evidence_snippet="Official page has direct music submission language but no email/form was extracted.",
                    confidence=0.62,
                )
            )
        if verification.get("has_direct_submission"):
            for candidate in verification.get("candidates", []) or []:
                if not isinstance(candidate, dict):
                    continue
                email = str(candidate.get("email") or "").strip().lower()
                local = email.split("@", 1)[0] if "@" in email else ""
                dedicated_on_submission_page = _is_dedicated_submission_email(email) or (
                    "music" in local and local not in {"webmaster", "info", "contact", "feedback", "support"}
                )
                if not email or not dedicated_on_submission_page:
                    continue
                out.append(
                    _route_candidate(
                        route_type="explicit_submission_email",
                        source=str(candidate.get("source") or verification.get("mode") or "phase3_dynamic"),
                        url=url,
                        email=email,
                        evidence_snippet=str(candidate.get("evidence_snippet") or "")[:600]
                        or "Dedicated submission email found on official submission page.",
                        confidence=max(0.82, float(candidate.get("confidence") or 0.0)),
                    )
                )
    return out


def _estimate_gemini_cost_usd(*prompts: str) -> float:
    input_tokens = sum(max(1, len(prompt) // 4) for prompt in prompts)
    output_tokens = 2 * max(256, int(settings.station_quality_gemini_max_output_tokens))
    return round(
        (input_tokens / 1_000_000.0) * float(settings.station_quality_gemini_input_price_per_1m)
        + (output_tokens / 1_000_000.0) * float(settings.station_quality_gemini_output_price_per_1m),
        6,
    )


def _build_final_prompt(
    station: Station,
    old_features: dict[str, Any],
    structure: dict[str, Any],
    router: dict[str, Any],
    discovery: dict[str, Any],
) -> str:
    return (
        "You are a conservative multilingual radio submission route verifier.\n"
        "Use old DB data plus the official-site structure and fetched candidate pages.\n"
        "A verified route needs official evidence for music/demo/promo/airplay/new artist submissions, a direct music form, or a music/program director contact. "
        "Do not promote generic contact, event submissions, PSA forms, contests, listener requests, advertising, jobs, or artist directories.\n"
        "Return valid compact JSON only. Required keys: decision, quality_score, confidence, is_real_station, accepts_music_submissions, accepts_new_artists, outreach_bucket, best_route, secondary_routes, style_tags, campaign_fit_tags, editorial_format, pitch_angle_hint, negative_flags, manual_review_reason, rationale_short.\n"
        "decision enum: promote|review|reject|keep. outreach_bucket enum: verified_submission|music_director_contact|contact_only|needs_review|none.\n"
        "best_route object keys: route_type,url,email,person_name,role,confidence,reason. route_type enum: direct_music_form|gated_music_form|explicit_submission_email|music_director_contact|contact_form_only|contact_email_only|submission_page_signal|none.\n"
        f"\nOLD_FEATURES:\n{json.dumps(old_features, ensure_ascii=False)[:5000]}\n"
        f"\nSITE_STRUCTURE_SUMMARY:\n{json.dumps({k: structure.get(k) for k in ('title','meta_description','headings','emails','known_submission_urls','sitemap_urls_sample')}, ensure_ascii=False)[:3000]}\n"
        f"\nROUTER_RESULT:\n{json.dumps(router, ensure_ascii=False)[:3000]}\n"
        f"\nFETCHED_DISCOVERY:\n{json.dumps(_compact_discovery_for_gemini(discovery), ensure_ascii=False)[:9000]}"
    )


def _persist_dynamic_assessment(
    session: Session,
    station: Station,
    *,
    structure: dict[str, Any],
    router: dict[str, Any],
    discovery: dict[str, Any],
    verdict: dict[str, Any],
    apply: bool,
    run: EnrichmentRun | None,
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
    confidence = float(verdict.get("confidence") or best.get("confidence") or 0.5)
    quality_score = int(verdict.get("quality_score") or 50)
    row.is_real_station = bool(verdict.get("is_real_station", True))
    row.has_real_editorial_surface = True
    row.accepts_music_submissions = bool(verdict.get("accepts_music_submissions", route_type in STRONG_ROUTE_TYPES))
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
            "router": router,
            "site_structure": {
                "url": structure.get("url"),
                "title": structure.get("title"),
                "meta_description": structure.get("meta_description"),
                "headings": structure.get("headings", [])[:25],
                "emails": structure.get("emails", [])[:20],
                "link_count": len(structure.get("links") or []),
                "known_submission_urls": structure.get("known_submission_urls", [])[:20],
                "sitemap_urls_sample": structure.get("sitemap_urls_sample", [])[:30],
            },
            "discovery": discovery,
            "verdict": verdict,
            "evaluated_at": datetime.utcnow().isoformat() + "Z",
        }
    )
    if apply and route_type != "none":
        old_score = 0
        if station.best_submission_route_type in STRONG_ROUTE_TYPES:
            old_score = 90
        elif station.best_submission_route_type in CONTACT_ROUTE_TYPES:
            old_score = 30
        new_score = 90 if route_type in STRONG_ROUTE_TYPES else 30 if route_type in CONTACT_ROUTE_TYPES else 15
        if new_score >= old_score:
            station.best_submission_route_type = route_type
            station.best_submission_route_url = str(best.get("url") or "")[:1024] or None
            station.best_submission_route_email = str(best.get("email") or "").lower()[:320] or None
            station.best_submission_route_confidence = round(float(best.get("confidence") or confidence), 4)
            station.best_submission_route_reason = str(best.get("reason") or row.notes or "")[:500] or None
            station.best_submission_route_updated_at = datetime.utcnow()
            session.add(station)
            _upsert_submission_channel(session, station, best, row.notes or "")
    session.add(row)
    if run is not None:
        session.add(
            EnrichmentFinding(
                run_id=run.id,
                station_id=station.id,
                layer="phase3",
                finding_type="dynamic_official_discovery_verdict",
                source_url=str(best.get("url") or station.website_url or "")[:2048],
                source_name="gemini",
                title=station.canonical_name,
                snippet=row.notes,
                payload_json=row.evidence_json,
                confidence=confidence,
                useful=route_type in STRONG_ROUTE_TYPES or route_type == "submission_page_signal",
            )
        )
    return row


def _upsert_submission_channel(session: Session, station: Station, route: dict[str, Any], notes: str) -> None:
    route_type = str(route.get("route_type") or "")
    url = str(route.get("url") or "").strip() or None
    email = str(route.get("email") or "").strip().lower() or None
    if route_type not in STRONG_ROUTE_TYPES | CONTACT_ROUTE_TYPES or not (url or email):
        return
    method = SubmissionMethod.EMAIL if email and route_type != "direct_music_form" else SubmissionMethod.FORM
    existing = session.scalar(
        select(SubmissionChannel).where(
            SubmissionChannel.station_id == station.id,
            SubmissionChannel.method == method,
            SubmissionChannel.url == url,
            SubmissionChannel.email == email,
        )
    )
    if existing is None:
        session.add(
            SubmissionChannel(
                station_id=station.id,
                method=method,
                url=url,
                email=email,
                requirements=f"{ASSESSMENT_KIND}: {notes}"[:1000],
                accepts_newcomers=route_type in STRONG_ROUTE_TYPES,
            )
        )


def _candidate_station_ids(
    session: Session,
    *,
    limit: int,
    min_confidence: float,
    fresh_days: int,
) -> list[int]:
    latest_dynamic = (
        select(func.max(StationSubmissionAssessment.updated_at))
        .where(
            StationSubmissionAssessment.station_id == Station.id,
            StationSubmissionAssessment.assessment_kind == ASSESSMENT_KIND,
        )
        .correlate(Station)
        .scalar_subquery()
    )
    stale_before = datetime.utcnow() - timedelta(days=max(1, fresh_days))
    rows = session.execute(
        select(Station.id)
        .where(
            Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
            Station.website_url.is_not(None),
            Station.confidence_score >= min_confidence,
            or_(latest_dynamic.is_(None), latest_dynamic < stale_before),
            or_(
                Station.best_submission_route_type.is_(None),
                Station.best_submission_route_type.in_(list(CONTACT_ROUTE_TYPES | {"submission_page_signal"})),
            ),
        )
        .order_by(Station.priority_tier.desc(), Station.confidence_score.desc(), Station.updated_at.desc())
        .limit(max(1, limit))
    ).all()
    return [int(row[0]) for row in rows]


def run_dynamic_official_discovery_for_station(
    session: Session,
    station_id: int,
    *,
    max_links: int = 120,
    max_deep_pages: int = 8,
    apply: bool = False,
    create_run: bool = True,
) -> dict[str, Any]:
    station = session.get(Station, int(station_id))
    if station is None or not station.website_url:
        return {"station_id": station_id, "status": "error", "error": "station_missing_or_no_website"}
    run: EnrichmentRun | None = None
    if create_run:
        run = EnrichmentRun(
            station_id=station.id,
            layer="phase3",
            source_pool="dynamic_official_discovery",
            reason_codes_json=_json_dumps(["dynamic_site_structure_router"]),
        )
        session.add(run)
        session.flush()
    try:
        structure = collect_site_structure(station, max_links=max_links)
        router_prompt = _build_router_prompt(station, structure, max_urls=max_deep_pages)
        try:
            raw_router = _call_gemini_route_discovery(router_prompt)
            router = _normalize_router_result(station, raw_router, max_urls=max_deep_pages)
        except Exception as exc:
            router = _local_rank_candidate_pages(structure, max_urls=max_deep_pages)
            router["rationale_short"] = f"Gemini router fallback after {exc.__class__.__name__}."
            router["router_error"] = f"{exc.__class__.__name__}:{str(exc)[:220]}"
        known_urls = [
            str(url)
            for url in structure.get("known_submission_urls", [])
            if isinstance(url, str) and url
        ]
        routed_urls = [item["url"] for item in router.get("priority_urls", []) if isinstance(item, dict) and item.get("url")]
        urls = list(dict.fromkeys(known_urls + routed_urls))
        if station.website_url and station.website_url.rstrip("/") not in {u.rstrip("/") for u in urls}:
            urls.append(station.website_url.rstrip("/"))
        verifications = [_verify_url(url, station) for url in urls[:max_deep_pages]]
        candidates: list[dict[str, Any]] = []
        for verification in verifications:
            candidates.extend([c for c in verification.get("candidates", []) if isinstance(c, dict)])
        candidates.extend(_supplement_route_candidates(verifications))
        candidates = [
            candidate
            for candidate in candidates
            if not (
                str(candidate.get("route_type") or "") == "music_director_contact"
                and not str(candidate.get("email") or "").strip()
            )
        ]
        discovery = {
            "candidate_urls": urls[:max_deep_pages],
            "url_verifications": verifications,
            "route_candidates": sorted(
                {_json_dumps({k: c.get(k) for k in ("route_type", "url", "email")}): c for c in candidates}.values(),
                key=lambda c: (int(c.get("score") or 0), float(c.get("confidence") or 0.0)),
                reverse=True,
            )[:12],
        }
        old_features = _build_station_features(session=session, station=station, include_deep_pass=False)
        final_prompt = _build_final_prompt(station, old_features, structure, router, discovery)
        cost_estimate = _estimate_gemini_cost_usd(router_prompt, final_prompt)
        try:
            raw_verdict = _call_gemini_route_discovery(final_prompt)
            verdict = _guard_verdict_against_discovery(_normalize_verdict(raw_verdict, discovery["route_candidates"]), discovery, discovery["route_candidates"])
        except Exception as exc:
            verdict = _guard_verdict_against_discovery(
                _fallback_verdict_from_candidates(discovery["route_candidates"], f"{exc.__class__.__name__}:{str(exc)[:220]}"),
                discovery,
                discovery["route_candidates"],
            )
        verdict = _guard_final_verdict(verdict, discovery["route_candidates"])
        assessment = _persist_dynamic_assessment(
            session,
            station,
            structure=structure,
            router=router,
            discovery=discovery,
            verdict=verdict,
            apply=apply,
            run=run,
        )
        if run is not None:
            run.status = "done"
            run.outcome = str(verdict.get("best_route", {}).get("route_type") or "none")
            run.stop_reason = "phase3_dynamic_complete"
            run.urls_checked = len(verifications) + 1
            run.findings_count = len(candidates)
            run.useful_findings_count = 1 if str(verdict.get("best_route", {}).get("route_type") or "") in STRONG_ROUTE_TYPES else 0
            run.api_calls_json = _json_dumps({"gemini": 2})
            run.cost_estimate_usd = cost_estimate
            run.metrics_json = _json_dumps({"router": router, "candidate_count": len(candidates)})
            run.finished_at = datetime.utcnow()
        session.commit()
        return {
            "station_id": station.id,
            "name": station.canonical_name,
            "status": "ok",
            "assessment_id": assessment.id,
            "applied": apply,
            "gemini_calls": 2,
            "cost_estimate_usd": cost_estimate,
            "structure_links": len(structure.get("links") or []),
            "deep_pages": len(verifications),
            "route_candidates": discovery["route_candidates"][:5],
            "verdict": {
                "decision": verdict.get("decision"),
                "quality_score": verdict.get("quality_score"),
                "confidence": verdict.get("confidence"),
                "outreach_bucket": verdict.get("outreach_bucket"),
                "best_route": verdict.get("best_route"),
            },
        }
    except Exception as exc:
        session.rollback()
        run = None
        try:
            run = EnrichmentRun(
                station_id=station.id,
                layer="phase3",
                source_pool="dynamic_official_discovery",
                status="error",
                outcome="error",
                stop_reason="phase3_dynamic_error",
                error=f"{exc.__class__.__name__}:{str(exc)[:500]}",
                finished_at=datetime.utcnow(),
            )
            session.add(run)
        except Exception:
            run = None
        if run is not None:
            session.commit()
        return {"station_id": station.id, "name": station.canonical_name, "status": "error", "error": f"{exc.__class__.__name__}:{str(exc)[:300]}"}


def run_dynamic_official_discovery_batch(
    session: Session,
    *,
    limit: int = 5,
    station_id: int | None = None,
    max_links: int = 120,
    max_deep_pages: int = 8,
    max_gemini_calls: int | None = None,
    min_confidence: float = 0.7,
    fresh_days: int = 14,
    apply: bool = False,
) -> dict[str, Any]:
    if max_gemini_calls is None:
        max_gemini_calls = settings.max_llm_calls_per_run
    budget = max(0, int(max_gemini_calls))
    if station_id is not None:
        station_ids = [int(station_id)]
    else:
        station_ids = _candidate_station_ids(session, limit=limit, min_confidence=min_confidence, fresh_days=fresh_days)
    worker_id = uuid.uuid4().hex[:12]
    results: list[dict[str, Any]] = []
    stop_reason = ""
    for sid in station_ids[: max(1, limit)]:
        if budget < 2:
            stop_reason = "gemini_budget_exhausted"
            break
        result = run_dynamic_official_discovery_for_station(
            session,
            sid,
            max_links=max_links,
            max_deep_pages=max_deep_pages,
            apply=apply,
            create_run=True,
        )
        results.append(result)
        if result.get("status") == "ok":
            budget -= int(result.get("gemini_calls") or 2)
    summary: dict[str, int] = {}
    for result in results:
        route = str(((result.get("verdict") or {}).get("best_route") or {}).get("route_type") or result.get("status") or "unknown")
        summary[route] = summary.get(route, 0) + 1
    return {
        "worker_id": worker_id,
        "processed": len(results),
        "apply": apply,
        "stop_reason": stop_reason,
        "gemini_calls_remaining": budget,
        "summary": summary,
        "results": results,
    }


PHASE3_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS phase3_dynamic_route_queue (
    id SERIAL PRIMARY KEY,
    source_pool VARCHAR(64) NOT NULL DEFAULT 'phase3_contact_false_negative',
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    route_type VARCHAR(64),
    result_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    processed_at TIMESTAMP,
    UNIQUE (source_pool, station_id)
);
CREATE INDEX IF NOT EXISTS ix_phase3_dynamic_queue_status_priority
    ON phase3_dynamic_route_queue (status, priority_score DESC, id ASC);
CREATE INDEX IF NOT EXISTS ix_phase3_dynamic_queue_station
    ON phase3_dynamic_route_queue (station_id);
"""


def ensure_phase3_dynamic_queue(session: Session) -> None:
    session.execute(text(PHASE3_QUEUE_SQL))
    session.commit()


def seed_phase3_dynamic_queue(
    session: Session,
    *,
    limit: int = 500,
    min_confidence: float = 0.65,
    reset_pending: bool = False,
) -> dict[str, Any]:
    ensure_phase3_dynamic_queue(session)
    if reset_pending:
        session.execute(text("DELETE FROM phase3_dynamic_route_queue WHERE status IN ('pending','error')"))
        session.commit()
    rows = session.execute(
        text(
            """
            WITH scored AS (
                SELECT
                    s.id AS station_id,
                    (
                        coalesce(s.confidence_score, 0) * 100
                        + coalesce(s.priority_tier, 0) * 8
                        + CASE coalesce(s.best_submission_route_type, '')
                            WHEN 'contact_email_only' THEN 28
                            WHEN 'contact_form_only' THEN 22
                            WHEN 'music_director_contact' THEN 18
                            WHEN 'submission_page_signal' THEN 16
                            ELSE 0
                          END
                        + CASE WHEN bool_or(sc.accepts_newcomers IS TRUE) THEN 15 ELSE 0 END
                        + CASE WHEN bool_or(lower(coalesce(sc.requirements,'')) ~
                            '(submission|submissions|submit|demo|musik|music vorstellen|airplay|bemuster|new artist|newcomer|unsigned)'
                          ) THEN 15 ELSE 0 END
                        + CASE WHEN s.best_submission_route_url IS NOT NULL AND lower(s.best_submission_route_url) ~
                            '(submission|submit|demo|airplay|music|musik|bemuster|newcomer|artist)'
                          THEN 12 ELSE 0 END
                        + CASE WHEN s.best_submission_route_email IS NOT NULL AND lower(s.best_submission_route_email) ~
                            '(submission|submit|demo|airplay|music|musik|playlist|newmusic|localmusic|musicdept)'
                          THEN 12 ELSE 0 END
                    ) AS priority_score
                FROM stations s
                LEFT JOIN submission_channels sc ON sc.station_id = s.id
                WHERE s.status IN ('CANDIDATE','VERIFIED')
                  AND s.website_url IS NOT NULL
                  AND coalesce(s.confidence_score, 0) >= :min_confidence
                  AND coalesce(s.best_submission_route_type, '') IN (
                    '', 'contact_email_only', 'contact_form_only', 'music_director_contact', 'submission_page_signal'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM station_submission_assessments a
                    WHERE a.station_id = s.id
                      AND a.assessment_kind = :assessment_kind
                      AND a.updated_at > now() - interval '14 days'
                  )
                GROUP BY s.id
            )
            SELECT station_id, priority_score
            FROM scored
            WHERE priority_score >= 80
            ORDER BY priority_score DESC, station_id ASC
            LIMIT :limit
            """
        ),
        {"limit": int(limit), "min_confidence": float(min_confidence), "assessment_kind": ASSESSMENT_KIND},
    ).mappings().all()
    inserted = 0
    updated = 0
    for row in rows:
        result = session.execute(
            text(
                """
                INSERT INTO phase3_dynamic_route_queue (source_pool, station_id, priority_score, status)
                VALUES ('phase3_contact_false_negative', :station_id, :priority_score, 'pending')
                ON CONFLICT (source_pool, station_id) DO UPDATE
                SET priority_score = greatest(phase3_dynamic_route_queue.priority_score, excluded.priority_score),
                    status = CASE
                        WHEN phase3_dynamic_route_queue.status IN ('done','running') THEN phase3_dynamic_route_queue.status
                        ELSE 'pending'
                    END,
                    updated_at = now()
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {"station_id": int(row["station_id"]), "priority_score": float(row["priority_score"] or 0)},
        ).mappings().first()
        if result and result["inserted"]:
            inserted += 1
        else:
            updated += 1
    session.commit()
    return phase3_dynamic_queue_status(session) | {"selected": len(rows), "inserted": inserted, "updated": updated}


def phase3_dynamic_queue_status(session: Session) -> dict[str, Any]:
    ensure_phase3_dynamic_queue(session)
    by_status = {
        str(row["status"]): int(row["n"])
        for row in session.execute(
            text(
                """
                SELECT status, count(*) AS n
                FROM phase3_dynamic_route_queue
                GROUP BY status
                ORDER BY status
                """
            )
        ).mappings()
    }
    routes = {
        str(row["route_type"]): int(row["n"])
        for row in session.execute(
            text(
                """
                SELECT coalesce(route_type, 'unknown') AS route_type, count(*) AS n
                FROM phase3_dynamic_route_queue
                WHERE status = 'done'
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings()
    }
    return {"by_status": by_status, "done_route_types": routes}


def run_phase3_dynamic_queue(
    session: Session,
    *,
    limit: int = 20,
    max_gemini_calls: int = 40,
    max_links: int = 120,
    max_deep_pages: int = 8,
    apply: bool = True,
) -> dict[str, Any]:
    ensure_phase3_dynamic_queue(session)
    budget = max(0, int(max_gemini_calls))
    processed = 0
    results: list[dict[str, Any]] = []
    stop_reason = ""
    while processed < limit:
        if budget < 2:
            stop_reason = "gemini_budget_exhausted"
            break
        row = session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT id
                    FROM phase3_dynamic_route_queue
                    WHERE status IN ('pending','error')
                      AND attempt_count < max_attempts
                    ORDER BY priority_score DESC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE phase3_dynamic_route_queue q
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    updated_at = now()
                FROM candidate
                WHERE q.id = candidate.id
                RETURNING q.id, q.station_id
                """
            )
        ).mappings().first()
        session.commit()
        if row is None:
            stop_reason = "queue_empty"
            break
        result = run_dynamic_official_discovery_for_station(
            session,
            int(row["station_id"]),
            max_links=max_links,
            max_deep_pages=max_deep_pages,
            apply=apply,
        )
        best = ((result.get("verdict") or {}).get("best_route") or {})
        route_type = str(best.get("route_type") or result.get("status") or "unknown")
        status = "done" if result.get("status") == "ok" else "error"
        session.execute(
            text(
                """
                UPDATE phase3_dynamic_route_queue
                SET status = CAST(:status AS VARCHAR),
                    route_type = CAST(:route_type AS VARCHAR),
                    result_json = CAST(:result_json AS TEXT),
                    last_error = CAST(:last_error AS TEXT),
                    updated_at = now(),
                    processed_at = CASE WHEN CAST(:status AS VARCHAR) = 'done' THEN now() ELSE processed_at END
                WHERE id = :id
                """
            ),
            {
                "id": int(row["id"]),
                "status": status,
                "route_type": route_type,
                "result_json": _json_dumps(result)[:200000],
                "last_error": str(result.get("error") or "")[:1000] or None,
            },
        )
        session.commit()
        results.append(result)
        processed += 1
        if result.get("status") == "ok":
            budget -= int(result.get("gemini_calls") or 2)
    summary: dict[str, int] = {}
    for result in results:
        best = ((result.get("verdict") or {}).get("best_route") or {})
        route = str(best.get("route_type") or result.get("status") or "unknown")
        summary[route] = summary.get(route, 0) + 1
    return {
        "processed": processed,
        "apply": apply,
        "stop_reason": stop_reason,
        "gemini_calls_remaining": budget,
        "summary": summary,
        "queue": phase3_dynamic_queue_status(session),
        "results": results[:20],
    }

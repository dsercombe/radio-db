from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from radio_db.config import settings
from radio_db.models.entities import (
    FormRecipe,
    FormStatus,
    FormType,
    Station,
    StationAlias,
    StationContact,
    StationGenre,
    StationPerson,
    StationProgram,
    StationStatus,
    StationSubmissionAssessment,
    SubmissionChannel,
    SubmissionMethod,
    SubmissionAgentIssue,
    SubmissionAgentRun,
    SubmissionAgentStep,
    SubmissionForm,
    SubmissionFormField,
)
from radio_db.services.forms import (
    DEEP_SUBMISSION_HINTS,
    _candidate_urls_for_station,
    _email_contexts,
    _extract_forms_fallback,
    _extract_forms_playwright,
    _is_valid_discovered_email,
    _persist_email_channels,
    _persist_form,
    _snippet_contexts,
    is_supported_station_target_url,
    station_matches_entrypoint_only,
    station_matches_excluded_meta,
    station_matches_excluded_focus,
)
from radio_db.services.station_quality import run_station_quality_verification

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


NEW_ARTIST_HINTS = ("new artist", "unsigned", "emerging", "newcomer", "demo")
BLOCKED_SUBMISSION_HINTS = (
    "not for music submissions",
    "no music submissions",
    "not for submissions",
    "do not send",
)
MAIN_SCAN_HIGH_VALUE_BLOCKED_REASON = "blocked_high_value_followup"
REJECTED_SCAN_CLAIM_LEASE_SECONDS = 900
REJECTED_SCAN_CLAIM_BATCH_MULTIPLIER = 1
REJECTED_SCAN_RETRYABLE_OUTCOMES = {"retry", "scan_error"}
REJECTED_SCAN_WRITE_LOCK = threading.Lock()


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_loads(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def _load_exception_registry() -> list[dict]:
    path = Path(settings.submission_exception_registry_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if isinstance(rule, dict)]


def _rule_active(rule: dict) -> bool:
    if rule.get("active") is False:
        return False
    expires_on = str(rule.get("expires_on") or "").strip()
    if not expires_on:
        return True
    try:
        return datetime.utcnow().date().isoformat() <= expires_on
    except Exception:
        return True


def _rule_matches_station(rule: dict, station: Station) -> bool:
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    station_ids = {int(x) for x in (match.get("station_ids") or []) if str(x).isdigit()}
    if station_ids and station.id in station_ids:
        return True
    domain = _domain(station.website_url)
    domains = {str(x).strip().lower().replace("www.", "") for x in (match.get("domains") or []) if str(x).strip()}
    if domains and domain and domain in domains:
        return True
    name_lower = (station.canonical_name or "").lower()
    for needle in (match.get("name_contains") or []):
        if needle and str(needle).lower() in name_lower:
            return True
    return False


def _apply_assessment_exceptions(station: Station, computed: dict, evidence_payload: dict, notes: list[str]) -> None:
    matched_rules: list[str] = []
    for rule in _load_exception_registry():
        if not _rule_active(rule):
            continue
        if not _rule_matches_station(rule, station):
            continue
        override = rule.get("override") if isinstance(rule.get("override"), dict) else {}
        if "accepts_music_submissions" in override:
            computed["accepts_music_submissions"] = bool(override.get("accepts_music_submissions"))
        if "accepts_new_artists" in override:
            computed["accepts_new_artists"] = bool(override.get("accepts_new_artists"))
        if "is_pitchable" in override:
            computed["is_pitchable"] = bool(override.get("is_pitchable"))
        if "submission_access" in override:
            computed["submission_access"] = str(override.get("submission_access") or computed["submission_access"])
        if "has_real_editorial_surface" in override:
            computed["has_real_editorial_surface"] = bool(override.get("has_real_editorial_surface"))
        rule_id = str(rule.get("id") or "unnamed_rule")
        matched_rules.append(rule_id)
        note = str(rule.get("note") or "").strip()
        notes.append(f"exception_rule={rule_id}" + (f": {note}" if note else ""))
    evidence_payload["exception_rules_applied"] = matched_rules


def build_station_submission_assessment(session: Session, station: Station) -> StationSubmissionAssessment:
    submissions = list(station.submissions or [])
    forms = list(station.forms or [])
    people = list(station.people or [])
    programs = list(station.programs or [])

    is_real_station = bool(
        station.website_url
        and (
            station.status.value == "verified"
            or station.confidence_score >= 0.55
            or bool(programs)
            or bool(people)
            or bool(submissions)
        )
    )
    has_real_editorial_surface = bool(station.website_url and (people or programs or submissions))
    def _is_music_relevant_channel(channel) -> bool:
        requirements = (channel.requirements or "").lower()
        url = (channel.url or "").lower()
        email = (channel.email or "").lower()
        method = channel.method.value if channel.method is not None else "unknown"
        if channel.accepts_newcomers:
            return True
        if method in ("form", "portal"):
            return True
        hints = ("music", "submit", "submission", "demo", "playlist", "airplay", "track", "artist")
        hay = f"{requirements} {url} {email}"
        return any(h in hay for h in hints)

    music_relevant_submissions = [ch for ch in submissions if _is_music_relevant_channel(ch)]
    strong_forms = [
        form
        for form in forms
        if form.form_type in (FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER)
        and not form.requires_login
        and not form.has_captcha
        and len(form.fields or []) >= 3
    ]
    gated_forms = [
        form
        for form in forms
        if form.form_type in (FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER)
        and (form.requires_login or form.has_captcha)
    ]
    accepts_music_submissions = bool(music_relevant_submissions or strong_forms or gated_forms)
    accepts_new_artists = any(ch.accepts_newcomers for ch in submissions) or any(
        any(hint in (ch.requirements or "").lower() for hint in NEW_ARTIST_HINTS) for ch in submissions
    )
    has_restriction = any(
        any(block in ((ch.requirements or "").lower()) for block in BLOCKED_SUBMISSION_HINTS) for ch in submissions
    ) or any(form.requires_login or form.has_captcha for form in forms)
    discovered_email_count = len(
        {str(ch.email).strip().lower() for ch in submissions if ch.email and str(ch.email).strip()}
    )
    discovered_submission_url_count = len(
        {str(ch.url).strip() for ch in submissions if ch.url and str(ch.url).strip()}
    )
    if accepts_music_submissions and not has_restriction:
        submission_access = "open"
    elif accepts_music_submissions and has_restriction:
        submission_access = "restricted"
    elif has_real_editorial_surface:
        submission_access = "none"
    else:
        submission_access = "unknown"
    is_pitchable = bool(is_real_station and (submission_access in ("open", "restricted")))

    known_form_count = len([form for form in forms if len(form.fields or []) >= 3])
    ready_points = 0.0
    ready_points += 0.35 if is_real_station else 0.0
    ready_points += 0.25 if has_real_editorial_surface else 0.0
    ready_points += 0.25 if accepts_music_submissions else 0.0
    ready_points += 0.15 if station.website_url else 0.0
    automation_readiness = min(1.0, ready_points + min(0.1, known_form_count * 0.02))

    risk_score = 1.0 - automation_readiness
    if not station.website_url:
        risk_score = min(1.0, risk_score + 0.2)
    if station.status.value == "candidate":
        risk_score = min(1.0, risk_score + 0.05)

    computed = {
        "is_real_station": is_real_station,
        "has_real_editorial_surface": has_real_editorial_surface,
        "accepts_music_submissions": accepts_music_submissions,
        "accepts_new_artists": accepts_new_artists,
        "submission_access": submission_access,
        "is_pitchable": is_pitchable,
    }
    notes_parts = []
    evidence_payload = {
        "station_status": station.status.value,
        "station_confidence": station.confidence_score,
        "website_url": station.website_url,
        "submission_channel_count": len(submissions),
        "music_relevant_submission_channel_count": len(music_relevant_submissions),
        "known_form_count": known_form_count,
        "gated_form_count": len(gated_forms),
        "people_count": len(people),
        "program_count": len(programs),
        "discovered_email_count": discovered_email_count,
        "discovered_submission_url_count": discovered_submission_url_count,
        "has_restriction_signal": has_restriction,
    }
    _apply_assessment_exceptions(
        station=station,
        computed=computed,
        evidence_payload=evidence_payload,
        notes=notes_parts,
    )
    notes_parts.insert(
        0,
        (
            f"real_station={'yes' if computed['is_real_station'] else 'no'}; "
            f"editorial_surface={'yes' if computed['has_real_editorial_surface'] else 'no'}; "
            f"music_submission={'yes' if computed['accepts_music_submissions'] else 'no'}; "
            f"new_artists={'yes' if computed['accepts_new_artists'] else 'no'}; "
            f"submission_access={computed['submission_access']}; "
            f"is_pitchable={'yes' if computed['is_pitchable'] else 'no'}"
        ),
    )
    notes = " | ".join(notes_parts)
    evidence_payload["submission_access"] = computed["submission_access"]
    evidence_payload["is_pitchable"] = computed["is_pitchable"]

    row = session.scalar(
        select(StationSubmissionAssessment).where(
            StationSubmissionAssessment.station_id == station.id,
            StationSubmissionAssessment.assessment_kind == "manual_scan",
        )
    )
    if row is None:
        row = StationSubmissionAssessment(station_id=station.id, assessment_kind="manual_scan")
        session.add(row)

    row.status = "active"
    row.is_real_station = bool(computed["is_real_station"])
    row.has_real_editorial_surface = bool(computed["has_real_editorial_surface"])
    row.accepts_music_submissions = bool(computed["accepts_music_submissions"])
    row.accepts_new_artists = bool(computed["accepts_new_artists"])
    row.automation_readiness = automation_readiness
    row.risk_score = risk_score
    row.notes = notes
    row.evidence_json = _json_dumps(evidence_payload)
    session.flush()
    return row


def _record_issue(
    session: Session,
    run: SubmissionAgentRun,
    step: SubmissionAgentStep | None,
    issue_type: str,
    severity: str,
    title: str,
    details: str,
    payload: dict | None = None,
) -> SubmissionAgentIssue:
    issue = SubmissionAgentIssue(
        run_id=run.id,
        step_id=step.id if step else None,
        issue_type=issue_type,
        severity=severity,
        status="open",
        title=title,
        details=details,
        payload_json=_json_dumps(payload or {}),
    )
    session.add(issue)
    session.flush()
    return issue


def _record_step(
    session: Session,
    run: SubmissionAgentRun,
    step_index: int,
    state_before: str,
    state_after: str,
    observation: dict,
    proposed_action: dict,
    execution_result: dict,
    executed_action: dict | None = None,
    screenshot_path: str | None = None,
    dom_snapshot_path: str | None = None,
    confidence: float = 0.0,
    latency_ms: int = 0,
) -> SubmissionAgentStep:
    step = SubmissionAgentStep(
        run_id=run.id,
        step_index=step_index,
        state_before=state_before,
        state_after=state_after,
        screenshot_path=screenshot_path,
        dom_snapshot_path=dom_snapshot_path,
        agent_observation_json=_json_dumps(observation),
        proposed_action_json=_json_dumps(proposed_action),
        executed_action_json=_json_dumps(executed_action or {"type": "none", "mode": "manual_phase_1"}),
        execution_result_json=_json_dumps(execution_result),
        confidence=confidence,
        latency_ms=latency_ms,
    )
    session.add(step)
    session.flush()
    return step


def _page_evidence_from_meta(url: str, entry_path: list, meta: dict, visible_forms: list) -> dict:
    effective_url = str(meta.get("url") or url)
    emails = [str(x).strip().lower() for x in (meta.get("emails") or []) if str(x).strip()]
    email_contexts = [
        {
            "email": str(ctx.get("email") or "")[:320],
            "near_submission_signal": bool(ctx.get("near_submission_signal")),
            "snippet": str(ctx.get("snippet") or "")[:360],
        }
        for ctx in (meta.get("email_contexts") or [])[:8]
        if isinstance(ctx, dict)
    ]
    submission_contexts = [
        {
            "keyword": str(ctx.get("keyword") or "")[:80],
            "snippet": str(ctx.get("snippet") or "")[:360],
        }
        for ctx in (meta.get("submission_keyword_contexts") or [])[:8]
        if isinstance(ctx, dict)
    ]
    ranked_links = [
        {
            "href": str(link.get("href") or "")[:1024],
            "text": str(link.get("text") or "")[:160],
            "score": float(link.get("score") or 0.0),
        }
        for link in (meta.get("links") or [])[:20]
        if isinstance(link, dict)
    ]
    form_blob = _json_dumps(visible_forms).lower()
    return {
        "url": effective_url,
        "requested_url": url,
        "entry_path": entry_path,
        "page_title": str(meta.get("title") or "")[:300],
        "language": str(meta.get("language") or "")[:32],
        "form_count": len(visible_forms),
        "music_form_hint": any(h in form_blob for h in ("music", "demo", "artist", "track", "song")),
        "requires_login": bool(meta.get("requires_login")),
        "has_captcha": bool(meta.get("has_captcha")),
        "emails": emails[:10],
        "email_contexts": email_contexts,
        "submission_keyword_contexts": submission_contexts,
        "top_ranked_links": ranked_links,
    }


def _append_ranked_scan_links(candidate_urls: list[tuple[str, list[str]]], meta: dict, max_candidates: int) -> None:
    if len(candidate_urls) >= max(1, max_candidates):
        return
    seen = {url for url, _entry_path in candidate_urls}
    links = [link for link in (meta.get("links") or []) if isinstance(link, dict)]
    links.sort(key=lambda link: float(link.get("score") or 0.0), reverse=True)
    for link in links:
        href = str(link.get("href") or "").strip()
        if not href or href in seen:
            continue
        score = float(link.get("score") or 0.0)
        if score <= 0:
            continue
        if not is_supported_station_target_url(href):
            continue
        seen.add(href)
        label = str(link.get("text") or "").strip()[:120] or "anchor"
        candidate_urls.append((href, ["ranked_scan_link", f"{score:.2f}", label]))
        if len(candidate_urls) >= max(1, max_candidates):
            break


def _rank_candidate_scan_urls(candidate_urls: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    def score(item: tuple[str, list[str]]) -> float:
        url, entry_path = item
        kind = str(entry_path[0] if entry_path else "")
        path_text = " ".join(str(x) for x in entry_path).lower()
        url_lower = url.lower()
        value = 0.0
        if kind == "manual_target":
            value += 100.0
        elif kind == "submission_channel":
            value += 72.0
        elif kind == "homepage":
            value += 80.0 if len(entry_path) == 1 else 35.0
        elif kind == "ranked_homepage_link":
            value += 70.0
            if len(entry_path) > 1:
                try:
                    value += float(entry_path[1])
                except Exception:
                    pass
        elif kind == "sitemap":
            value += 60.0
            if len(entry_path) > 1:
                try:
                    value += float(entry_path[1])
                except Exception:
                    pass
        elif kind == "ranked_scan_link":
            value += 55.0
            if len(entry_path) > 1:
                try:
                    value += float(entry_path[1])
                except Exception:
                    pass
        elif kind == "deep_hint":
            value += 30.0
        if any(h in url_lower for h in ("/submit-music", "/music-submission", "/music-submissions", "/submission-guidelines")):
            value += 35.0
        if any(h in url_lower for h in ("/submit", "/submission", "/demo")):
            value += 12.0
        if any(h in url_lower or h in path_text for h in ("music-submission", "submit-music", "music submissions")):
            value += 12.0
        if any(h in url_lower or h in path_text for h in ("contact", "kontakt", "team", "staff")):
            value += 5.0
        if any(h in url_lower for h in ("/contact", "/kontakt", "/team", "/staff")):
            value -= 18.0
        if any(h in url_lower for h in ("/request-a-song", "/wake-up-song", "/song-challenge", "songchallenge.", "/program/")):
            value -= 30.0
        if any(h in url_lower for h in ("privacy", "terms", "advertis", "shop", "event-submission")):
            value -= 20.0
        return value

    return sorted(candidate_urls, key=score, reverse=True)


def _has_sufficient_submission_evidence(
    *,
    page_evidence: list[dict],
    strong_form_count: int,
    min_pages_before_stop: int = 2,
) -> bool:
    if len(page_evidence) < max(1, min_pages_before_stop):
        return False
    if strong_form_count > 0:
        return True
    contextual_emails = sum(
        1
        for page in page_evidence
        for ctx in (page.get("email_contexts") or [])
        if isinstance(ctx, dict) and ctx.get("near_submission_signal")
    )
    strong_page_signal = any(
        any(
            str(ctx.get("keyword") or "").strip().lower()
            in {"submit", "submission", "submit music", "music submission", "demo", "airplay"}
            for ctx in (page.get("submission_keyword_contexts") or [])
            if isinstance(ctx, dict)
        )
        or any(h in str(page.get("url") or "").lower() for h in ("music-submission", "submit-music"))
        for page in page_evidence
    )
    return contextual_emails > 0 and strong_page_signal


def _stale_missing_force_rescan_forms(
    session: Session,
    *,
    station_id: int,
    page_evidence: list[dict],
) -> None:
    scanned_no_form_urls = {
        str(page.get("url") or "").strip()
        for page in page_evidence
        if isinstance(page, dict) and str(page.get("url") or "").strip() and int(page.get("form_count") or 0) == 0
    }
    if not scanned_no_form_urls:
        return
    stale_forms = session.scalars(
        select(SubmissionForm).where(
            SubmissionForm.station_id == station_id,
            SubmissionForm.status == FormStatus.ACTIVE,
            SubmissionForm.url.in_(scanned_no_form_urls),
        )
    ).all()
    for form in stale_forms:
        form.status = FormStatus.STALE
        form.updated_at = datetime.utcnow()
        session.add(form)

    stale_channels = session.scalars(
        select(SubmissionChannel).where(
            SubmissionChannel.station_id == station_id,
            SubmissionChannel.method == SubmissionMethod.FORM,
            SubmissionChannel.url.in_(scanned_no_form_urls),
        )
    ).all()
    for channel in stale_channels:
        session.delete(channel)


def _safe_artifact_path(path_value: str | None) -> str | None:
    if not path_value:
        return None
    try:
        resolved = Path(path_value).resolve()
    except Exception:
        return None
    state_root = Path(".radio_db_state").resolve()
    if resolved.is_file() and (resolved == state_root or state_root in resolved.parents):
        return str(resolved)
    return None


def _submit_profile() -> dict[str, str]:
    contact_email = settings.contact_execute_email.strip() or settings.smtp_from_email.strip()
    return {
        "artist_name": settings.contact_execute_name.strip() or settings.smtp_from_name.strip() or "Radio DB",
        "contact_email": contact_email,
        "email": contact_email,
        "company": settings.contact_execute_company.strip() or "Radio DB",
        "website": settings.contact_execute_website.strip(),
        "streaming_link": settings.contact_execute_website.strip(),
        "phone": settings.contact_execute_phone.strip(),
        "city": settings.contact_execute_city.strip(),
        "message": "Music submission from Radio DB automation.",
        "subject": "Music submission",
    }


def _resolve_execute_form(
    session: Session,
    station: Station,
    form_id: int | None,
    target_url: str | None,
) -> SubmissionForm | None:
    if form_id is not None:
        return session.scalar(
            select(SubmissionForm).where(
                SubmissionForm.id == form_id,
                SubmissionForm.station_id == station.id,
            )
        )
    if target_url:
        exact = session.scalar(
            select(SubmissionForm).where(
                SubmissionForm.station_id == station.id,
                SubmissionForm.url == target_url,
            )
        )
        if exact is not None:
            return exact
    forms = list(station.forms or [])
    if not forms:
        return None
    forms.sort(
        key=lambda item: (
            0 if item.status == FormStatus.ACTIVE else 1,
            0 if not item.has_captcha else 1,
            0 if not item.requires_login else 1,
            -(item.id or 0),
        )
    )
    return forms[0]


def _latest_recipe(session: Session, form_id: int) -> FormRecipe | None:
    return session.scalar(
        select(FormRecipe)
        .where(FormRecipe.form_id == form_id)
        .order_by(FormRecipe.version.desc(), FormRecipe.id.desc())
    )


def _field_selector(field: SubmissionFormField) -> str | None:
    if field.name:
        return f'[name="{field.name}"]'
    if field.field_key:
        return f'[name="{field.field_key}"], #{field.field_key}'
    return None


def _field_value_candidates(field: SubmissionFormField, mapping: dict[str, str], profile: dict[str, str]) -> list[str]:
    hay = f"{field.label or ''} {field.name or ''} {field.field_key or ''}".lower()
    candidates: list[str] = []

    reverse_mapping = {value: key for key, value in mapping.items()}
    logical = reverse_mapping.get(field.field_key)
    if logical:
        mapped_value = profile.get(logical, "")
        if mapped_value:
            candidates.append(mapped_value)

    if "email" in hay:
        candidates.append(profile["contact_email"])
    if any(token in hay for token in ("name", "artist", "band", "act", "performer")):
        candidates.append(profile["artist_name"])
    if any(token in hay for token in ("message", "bio", "description", "comments", "about")):
        candidates.append(profile["message"])
    if any(token in hay for token in ("subject", "title", "release", "track", "song")):
        candidates.append(profile["subject"])
    if any(token in hay for token in ("website", "url", "spotify", "youtube", "soundcloud", "link", "stream")):
        if profile["website"]:
            candidates.append(profile["website"])
    if "phone" in hay or "mobile" in hay:
        if profile["phone"]:
            candidates.append(profile["phone"])
    if "company" in hay or "label" in hay:
        candidates.append(profile["company"])
    if "city" in hay:
        if profile["city"]:
            candidates.append(profile["city"])

    cleaned: list[str] = []
    for value in candidates:
        value = (value or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _artifact_dir_for_run(run_id: int) -> Path:
    path = Path(settings.browser_snapshot_dir).resolve() / "agent_runs" / f"run-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clone_recipe_with_feedback(
    session: Session,
    form: SubmissionForm,
    recipe: FormRecipe | None,
    notes: str,
    status: str = "needs_review",
) -> FormRecipe | None:
    if recipe is None:
        return None
    latest_version = session.scalar(select(FormRecipe.version).where(FormRecipe.form_id == form.id).order_by(FormRecipe.version.desc()).limit(1))
    latest_version = latest_version or recipe.version or 1
    cloned = FormRecipe(
        form_id=form.id,
        version=int(latest_version) + 1,
        mode="execute-feedback",
        confidence_score=max(0.0, float(recipe.confidence_score or 0.0) - 0.05),
        status=status,
        instructions_text=recipe.instructions_text,
        machine_mapping_json=recipe.machine_mapping_json,
        field_order_json=recipe.field_order_json,
        upload_strategy_json=recipe.upload_strategy_json,
        submit_strategy_json=recipe.submit_strategy_json,
        success_detection_rules_json=recipe.success_detection_rules_json,
        error_detection_rules_json=recipe.error_detection_rules_json,
        retry_rules_json=recipe.retry_rules_json,
        notes_for_future_runs=((recipe.notes_for_future_runs or "").strip() + "\n" + notes).strip(),
        discovered_at=recipe.discovered_at,
        last_verified_at=datetime.utcnow(),
    )
    session.add(cloned)
    session.flush()
    return cloned


def _classify_execution_outcome(
    html: str,
    form: SubmissionForm,
    missing_required: list[str],
    selector_missing: list[str],
    required_file_fields: list[str],
    success_rules: list[str],
    error_rules: list[str],
) -> tuple[str, str, str]:
    lowered = html.lower()
    if "agent_exception:" in lowered:
        return "failed", "agent_exception", "Browser execution raised an exception."
    generic_success = any(token in lowered for token in ("thank you", "thanks for", "successfully", "submitted"))
    generic_error = any(token in lowered for token in ("error", "invalid", "required field", "try again"))
    if form.has_captcha or "captcha" in lowered:
        return "blocked", "captcha", "Captcha detected during execution."
    if form.requires_login or ("password" in lowered and "login" in lowered):
        return "blocked", "login_required", "Login required during execution."
    if required_file_fields:
        return "blocked", "file_upload_required", "Required file upload field not automated."
    if missing_required:
        return "failed", "missing_required_field", "Required fields could not be filled."
    if selector_missing:
        return "failed", "field_selector_missing", "Tracked selectors were not present on the page."
    if error_rules and any(rule in lowered for rule in error_rules):
        return "failed", "validation_error", "Submission page indicates validation or server error."
    if success_rules and any(rule in lowered for rule in success_rules):
        return "completed", "submitted", "Submission completed with matching success signal."
    if generic_success and not generic_error:
        return "completed", "submitted_generic", "Submission completed with generic success signal."
    return "failed", "success_unverified", "Submission executed but no success marker matched."


def start_execute_submission_run(
    session: Session,
    station_id: int,
    form_id: int | None = None,
    target_url: str | None = None,
    mode: str = "execute",
    max_retries: int = 1,
) -> SubmissionAgentRun:
    station = session.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise ValueError(f"station_not_found:{station_id}")

    selected_form = _resolve_execute_form(session, station, form_id=form_id, target_url=target_url)
    run = SubmissionAgentRun(
        station_id=station.id,
        form_id=selected_form.id if selected_form is not None else None,
        mode=mode,
        goal="submission_execute",
        status="running",
        current_state="prepare",
        confidence=max(0.0, min(1.0, float(selected_form.confidence if selected_form is not None else 0.0))),
        requires_approval=(mode != "execute"),
        target_url=target_url or (selected_form.url if selected_form is not None else station.website_url),
        summary_json=_json_dumps({"max_retries": max(0, int(max_retries)), "attempts": []}),
    )
    session.add(run)
    session.flush()

    if sync_playwright is None:
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="playwright_not_installed",
            severity="high",
            title="Playwright fehlt",
            details="Der Execute-Agent kann ohne Playwright nicht laufen.",
        )
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "playwright_not_installed"
        run.finished_at = datetime.utcnow()
        session.commit()
        session.refresh(run)
        return run

    if selected_form is None:
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="form_not_found",
            severity="high",
            title="Kein Formular gefunden",
            details="Fuer diesen Sender ist kein trackbares Submission-Formular vorhanden.",
        )
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "form_not_found"
        run.finished_at = datetime.utcnow()
        session.commit()
        session.refresh(run)
        return run

    if selected_form.status in {FormStatus.CAPTCHA_PRESENT, FormStatus.LOGIN_REQUIRED}:
        reason = selected_form.status.value
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type=reason,
            severity="high",
            title="Formular geblockt",
            details=f"Formular {selected_form.id} ist wegen {reason} nicht automatisierbar.",
            payload={"form_id": selected_form.id, "url": selected_form.url},
        )
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = reason
        run.finished_at = datetime.utcnow()
        session.commit()
        session.refresh(run)
        return run

    recipe = _latest_recipe(session, selected_form.id)
    fields = session.scalars(
        select(SubmissionFormField)
        .where(SubmissionFormField.form_id == selected_form.id)
        .order_by(SubmissionFormField.id.asc())
    ).all()
    mapping = dict(_json_loads(recipe.machine_mapping_json if recipe else "{}", {}))
    success_rules = [str(item).lower() for item in _json_loads(recipe.success_detection_rules_json if recipe else "[]", [])]
    error_rules = [str(item).lower() for item in _json_loads(recipe.error_detection_rules_json if recipe else "[]", [])]
    submit_strategy = dict(_json_loads(recipe.submit_strategy_json if recipe else "{}", {}))
    profile = _submit_profile()

    step_index = 1
    prepare_step = _record_step(
        session=session,
        run=run,
        step_index=step_index,
        state_before="prepare",
        state_after="fill",
        observation={
            "station_name": station.canonical_name,
            "form_id": selected_form.id,
            "form_url": selected_form.url,
            "field_count": len(fields),
            "recipe_id": recipe.id if recipe else None,
            "mode": mode,
        },
        proposed_action={
            "type": "execute_form_recipe",
            "mode": mode,
            "target_url": selected_form.url,
        },
        executed_action={"type": "prepare", "mode": mode},
        execution_result={
            "allow_live_submit": bool(submit_strategy.get("allow_live_submit", False)),
            "max_retries": max(0, int(max_retries)),
        },
        confidence=run.confidence,
    )

    candidate_forms = [selected_form]
    if selected_form.url != target_url:
        alternate_forms = [
            form
            for form in sorted(
                list(station.forms or []),
                key=lambda item: (0 if item.status == FormStatus.ACTIVE else 1, -(item.id or 0)),
            )
            if form.id != selected_form.id and form.status == FormStatus.ACTIVE and not form.has_captcha and not form.requires_login
        ]
        candidate_forms.extend(alternate_forms[: max(0, int(max_retries))])

    attempts: list[dict[str, object]] = []
    final_status = "failed"
    final_reason = "execution_failed"

    for attempt_index, form in enumerate(candidate_forms[: max(1, int(max_retries) + 1)], start=1):
        recipe = _latest_recipe(session, form.id)
        fields = session.scalars(
            select(SubmissionFormField)
            .where(SubmissionFormField.form_id == form.id)
            .order_by(SubmissionFormField.id.asc())
        ).all()
        mapping = dict(_json_loads(recipe.machine_mapping_json if recipe else "{}", {}))
        success_rules = [str(item).lower() for item in _json_loads(recipe.success_detection_rules_json if recipe else "[]", [])]
        error_rules = [str(item).lower() for item in _json_loads(recipe.error_detection_rules_json if recipe else "[]", [])]
        artifact_dir = _artifact_dir_for_run(run.id)
        screenshot_path = artifact_dir / f"attempt-{attempt_index}.png"
        html_path = artifact_dir / f"attempt-{attempt_index}.html"

        missing_required: list[str] = []
        selector_missing: list[str] = []
        skipped_optional: list[str] = []
        required_file_fields: list[str] = []
        filled_fields: list[str] = []
        attempt_started = time.perf_counter()
        html = ""
        try:
            with sync_playwright() as pw:
                launch_kwargs: dict[str, object] = {"headless": settings.browser_headless}
                if settings.browser_proxy_url:
                    launch_kwargs["proxy"] = {"server": settings.browser_proxy_url}
                browser = pw.chromium.launch(**launch_kwargs)
                page = browser.new_page()
                page.set_default_timeout(settings.browser_timeout_ms)
                try:
                    page.goto(form.url, wait_until="domcontentloaded")
                    for field in fields:
                        selector = _field_selector(field)
                        field_label = field.label or field.name or field.field_key or f"field-{field.id}"
                        if not selector:
                            if field.required:
                                missing_required.append(field_label)
                            continue
                        locator = page.locator(selector).first
                        try:
                            if locator.count() == 0:
                                if field.required:
                                    selector_missing.append(field_label)
                                continue
                        except Exception:
                            if field.required:
                                selector_missing.append(field_label)
                            continue
                        values = _field_value_candidates(field, mapping, profile)
                        field_type = (field.input_type or "text").lower()
                        if field_type == "file":
                            if field.required:
                                required_file_fields.append(field_label)
                            else:
                                skipped_optional.append(field_label)
                            continue
                        if field_type in {"checkbox", "radio"}:
                            if field.required:
                                missing_required.append(field_label)
                            else:
                                skipped_optional.append(field_label)
                            continue
                        if not values:
                            if field.required:
                                missing_required.append(field_label)
                            else:
                                skipped_optional.append(field_label)
                            continue
                        try:
                            if field_type == "select":
                                locator.select_option(label=values[0])
                            else:
                                locator.fill(values[0])
                            filled_fields.append(field_label)
                        except Exception:
                            if field.required:
                                missing_required.append(field_label)
                            else:
                                skipped_optional.append(field_label)

                    if mode == "execute" and not missing_required and not selector_missing and not required_file_fields:
                        submit_locator = page.locator(
                            'form button[type="submit"], form input[type="submit"], button[type="submit"], input[type="submit"]'
                        ).first
                        submit_locator.click()
                        page.wait_for_timeout(2500)
                    else:
                        page.wait_for_timeout(250)
                    html = page.content()
                    html_path.write_text(html, encoding="utf-8")
                    page.screenshot(path=str(screenshot_path), full_page=True)
                finally:
                    browser.close()
        except Exception as exc:
            html = f"<html><body>agent_exception:{exc.__class__.__name__}:{str(exc)}</body></html>"
            html_path.write_text(html, encoding="utf-8")

        status, reason, details = _classify_execution_outcome(
            html=html,
            form=form,
            missing_required=missing_required,
            selector_missing=selector_missing,
            required_file_fields=required_file_fields,
            success_rules=success_rules,
            error_rules=error_rules,
        )
        if mode != "execute" and not missing_required and not selector_missing and not required_file_fields:
            status, reason, details = "completed", "dry_run_ready", "Dry-run completed without blocking field issues."

        execution_result = {
            "attempt": attempt_index,
            "form_id": form.id,
            "form_url": form.url,
            "recipe_id": recipe.id if recipe else None,
            "filled_fields": filled_fields,
            "missing_required": missing_required,
            "selector_missing": selector_missing,
            "required_file_fields": required_file_fields,
            "skipped_optional": skipped_optional,
            "reason": reason,
            "details": details,
            "screenshot_path": _safe_artifact_path(str(screenshot_path)),
            "html_path": _safe_artifact_path(str(html_path)),
        }
        attempts.append(execution_result)
        step_index += 1
        step = _record_step(
            session=session,
            run=run,
            step_index=step_index,
            state_before="fill" if attempt_index == 1 else "fallback_retry",
            state_after="verify" if status == "completed" else "fallback_retry" if attempt_index < len(candidate_forms) else "failed",
            observation={
                "attempt": attempt_index,
                "form_id": form.id,
                "field_count": len(fields),
                "mode": mode,
            },
            proposed_action={
                "type": "submit" if mode == "execute" else "dry_run_validate",
                "form_url": form.url,
            },
            executed_action={
                "type": "submit_form" if mode == "execute" else "validate_form",
                "form_id": form.id,
                "attempt": attempt_index,
            },
            execution_result=execution_result,
            screenshot_path=execution_result["screenshot_path"],
            dom_snapshot_path=execution_result["html_path"],
            confidence=max(0.0, min(1.0, float(recipe.confidence_score if recipe else run.confidence))),
            latency_ms=int((time.perf_counter() - attempt_started) * 1000),
        )

        if status != "completed":
            _record_issue(
                session=session,
                run=run,
                step=step,
                issue_type=reason,
                severity="high" if status == "blocked" else "medium",
                title="Submission-Ausfuehrung fehlgeschlagen" if status != "blocked" else "Submission blockiert",
                details=details,
                payload=execution_result,
            )
            feedback = (
                f"[{datetime.utcnow().isoformat()}] execute_run={run.id} attempt={attempt_index} "
                f"reason={reason}; missing_required={missing_required}; selector_missing={selector_missing}; "
                f"required_file_fields={required_file_fields}"
            )
            _clone_recipe_with_feedback(session=session, form=form, recipe=recipe, notes=feedback)
            final_status = status
            final_reason = reason
            if attempt_index < len(candidate_forms) and status == "failed":
                continue
        else:
            final_status = "completed"
            final_reason = reason
            run.form_id = form.id
            run.current_state = "completed"
            break

    run.summary_json = _json_dumps(
        {
            "attempts": attempts,
            "profile": {
                "artist_name": profile["artist_name"],
                "contact_email": profile["contact_email"],
                "company": profile["company"],
            },
            "selected_form_id": selected_form.id,
            "final_reason": final_reason,
        }
    )
    run.status = final_status
    run.current_state = "completed" if final_status == "completed" else "blocked" if final_status == "blocked" else "failed"
    run.blocked_reason = final_reason if final_status == "blocked" else None
    run.confidence = max(0.0, min(1.0, float(selected_form.confidence or 0.0)))
    run.finished_at = datetime.utcnow()
    session.commit()
    session.refresh(run)
    return run


def _rejected_scan_checkpoint_path() -> Path:
    return Path(".radio_db_state") / "rejected_scan_checkpoint.json"

def _load_rejected_scan_checkpoint() -> dict:
    path = _rejected_scan_checkpoint_path()
    if not path.exists():
        return {"last_station_id": 0, "updated_at": datetime.utcnow().isoformat()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "last_station_id": int(payload.get("last_station_id", 0)),
            "updated_at": str(payload.get("updated_at") or datetime.utcnow().isoformat()),
        }
    except Exception:
        return {"last_station_id": 0, "updated_at": datetime.utcnow().isoformat()}


def _save_rejected_scan_checkpoint(last_station_id: int) -> None:
    path = _rejected_scan_checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_station_id": int(last_station_id), "updated_at": datetime.utcnow().isoformat()}, indent=2),
        encoding="utf-8",
    )


def _finalize_rejected_scan_station(
    session: Session,
    station_id: int,
    *,
    outcome: str | None = None,
    run_status: str | None = None,
    scanned_at: datetime | None = None,
    error: str | None = None,
    release_claim: bool = True,
) -> None:
    params = {
        "station_id": int(station_id),
        "outcome": (outcome or "")[:64] or None,
        "run_status": (run_status or "")[:32] or None,
        "scanned_at": scanned_at or datetime.utcnow(),
        "error": (error or "")[:500] or None,
    }
    set_clauses = [
        "scan_outcome = COALESCE(:outcome, scan_outcome)",
        "scan_last_run_status = COALESCE(:run_status, scan_last_run_status)",
        "scan_last_scanned_at = COALESCE(:scanned_at, scan_last_scanned_at)",
        "scan_last_error = :error",
    ]
    if release_claim:
        set_clauses.extend(["scan_claim_token = NULL", "scan_claimed_at = NULL"])
    stmt = text(
        f"""
        UPDATE stations
        SET {", ".join(set_clauses)}
        WHERE id = :station_id
        """
    )
    for attempt in range(8):
        try:
            session.execute(stmt, params)
            return
        except OperationalError:
            session.rollback()
            if attempt >= 7:
                raise
            time.sleep(0.5 * (attempt + 1))


def release_stale_rejected_scan_claims(session: Session, older_than_seconds: int = REJECTED_SCAN_CLAIM_LEASE_SECONDS) -> int:
    lease_cutoff = datetime.utcfromtimestamp(time.time() - max(60, int(older_than_seconds))).isoformat(sep=" ")
    stmt = text(
        """
        UPDATE stations
        SET scan_claim_token = NULL,
            scan_claimed_at = NULL
        WHERE scan_claim_token IS NOT NULL
          AND scan_claimed_at IS NOT NULL
          AND scan_claimed_at < :lease_cutoff
        """
    )
    for attempt in range(5):
        try:
            result = session.execute(stmt, {"lease_cutoff": lease_cutoff})
            session.commit()
            return int(result.rowcount or 0)
        except OperationalError:
            session.rollback()
            if attempt >= 4:
                raise
            time.sleep(0.5 * (attempt + 1))
    return 0


def _claim_rejected_scan_batch(session: Session, station_limit: int, use_checkpoint: bool) -> tuple[list[int], int, bool]:
    checkpoint = _load_rejected_scan_checkpoint() if use_checkpoint else {"last_station_id": 0}
    last_station_id = int(checkpoint.get("last_station_id", 0))
    batch_size = max(1, station_limit * REJECTED_SCAN_CLAIM_BATCH_MULTIPLIER)
    claim_token = uuid.uuid4().hex
    lease_cutoff = datetime.utcfromtimestamp(time.time() - REJECTED_SCAN_CLAIM_LEASE_SECONDS).isoformat(sep=" ")
    select_sql = """
        SELECT id
        FROM stations
        WHERE status = 'REJECTED'
          AND website_url IS NOT NULL
          AND id > :from_id
          AND (scan_outcome IS NULL OR scan_outcome = '' OR scan_outcome IN ('retry', 'scan_error'))
          AND (scan_claim_token IS NULL OR scan_claimed_at IS NULL OR scan_claimed_at < :lease_cutoff)
        ORDER BY id ASC
        LIMIT :limit
    """
    wrapped = False
    for attempt in range(5):
        try:
            with session.begin():
                ids = [
                    int(row[0])
                    for row in session.execute(
                        text(select_sql),
                        {"from_id": last_station_id, "lease_cutoff": lease_cutoff, "limit": batch_size},
                    ).all()
                ]
                if not ids and use_checkpoint and last_station_id > 0:
                    wrapped = True
                    ids = [
                        int(row[0])
                        for row in session.execute(
                            text(
                                """
                                SELECT id
                                FROM stations
                                WHERE status = 'REJECTED'
                                  AND website_url IS NOT NULL
                                  AND (scan_outcome IS NULL OR scan_outcome = '' OR scan_outcome IN ('retry', 'scan_error'))
                                  AND (scan_claim_token IS NULL OR scan_claimed_at IS NULL OR scan_claimed_at < :lease_cutoff)
                                ORDER BY id ASC
                                LIMIT :limit
                                """
                            ),
                            {"lease_cutoff": lease_cutoff, "limit": batch_size},
                        ).all()
                    ]
                    last_station_id = 0
                if not ids:
                    return [], last_station_id, wrapped

                placeholders = ",".join(f":id_{index}" for index, _ in enumerate(ids))
                params: dict[str, object] = {"claim_token": claim_token}
                for index, station_id in enumerate(ids):
                    params[f"id_{index}"] = station_id
                session.execute(
                    text(
                        f"""
                        UPDATE stations
                        SET scan_claim_token = :claim_token,
                            scan_claimed_at = CURRENT_TIMESTAMP,
                            scan_attempt_count = COALESCE(scan_attempt_count, 0) + 1
                        WHERE id IN ({placeholders})
                        """
                    ),
                    params,
                )
                _save_rejected_scan_checkpoint(last_station_id=max(ids))
                return ids, int(max(ids)), wrapped
        except OperationalError:
            session.rollback()
            if attempt >= 4:
                raise
            time.sleep(0.5 * (attempt + 1))
    return [], last_station_id, wrapped


def _release_rejected_scan_claim(session: Session, station_id: int, error: str | None = None) -> None:
    _finalize_rejected_scan_station(
        session=session,
        station_id=station_id,
        error=error,
        release_claim=True,
        scanned_at=None,
    )


def _claim_rejected_quality_review_batch(session: Session, limit: int, claim_token: str) -> list[int]:
    lease_cutoff = datetime.utcfromtimestamp(time.time() - REJECTED_SCAN_CLAIM_LEASE_SECONDS).isoformat(sep=" ")
    # Prefer stations with stronger editorial/submission signals first (likely promote candidates),
    # without relaxing scan-side extraction — breaks ties by oldest scan timestamp (FIFO among peers).
    select_sql = """
        SELECT s.id
        FROM stations s
        LEFT JOIN (
            SELECT station_id, COUNT(*) AS channel_count
            FROM submission_channels
            GROUP BY station_id
        ) ch ON ch.station_id = s.id
        LEFT JOIN (
            SELECT station_id, COUNT(*) AS form_count
            FROM forms
            GROUP BY station_id
        ) fm ON fm.station_id = s.id
        WHERE s.status = 'REJECTED'
          AND s.scan_outcome = 'awaiting_quality_review'
          AND (s.scan_claim_token IS NULL OR s.scan_claimed_at IS NULL OR s.scan_claimed_at < :lease_cutoff)
        ORDER BY (COALESCE(ch.channel_count, 0) + COALESCE(fm.form_count, 0)) DESC,
                 s.scan_last_scanned_at ASC NULLS FIRST,
                 s.id ASC
        LIMIT :limit
    """
    for attempt in range(5):
        try:
            with session.begin():
                ids = [
                    int(row[0])
                    for row in session.execute(
                        text(select_sql),
                        {"lease_cutoff": lease_cutoff, "limit": max(1, int(limit))},
                    ).all()
                ]
                if not ids:
                    return []
                placeholders = ",".join(f":id_{index}" for index, _ in enumerate(ids))
                params: dict[str, object] = {"claim_token": claim_token}
                for index, station_id in enumerate(ids):
                    params[f"id_{index}"] = station_id
                session.execute(
                    text(
                        f"""
                        UPDATE stations
                        SET scan_claim_token = :claim_token,
                            scan_claimed_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})
                        """
                    ),
                    params,
                )
                return ids
        except OperationalError:
            session.rollback()
            if attempt >= 4:
                raise
            time.sleep(0.5 * (attempt + 1))
    return []


def backfill_rejected_scan_markers(session: Session, limit: int | None = None) -> dict:
    limit_clause = ""
    params: dict[str, object] = {}
    if limit is not None:
        limit_clause = "LIMIT :limit"
        params["limit"] = max(1, int(limit))
    count_stmt = text(
        f"""
        WITH latest AS (
            SELECT station_id, MAX(id) AS latest_run_id
            FROM submission_agent_runs
            GROUP BY station_id
        ),
        resolved AS (
            SELECT
                s.id AS station_id
            FROM stations s
            JOIN latest l ON l.station_id = s.id
            WHERE s.scan_last_scanned_at IS NULL
            ORDER BY s.id ASC
            {limit_clause}
        )
        SELECT COUNT(*) FROM resolved
        """
    )
    updated = int(session.execute(count_stmt, params).scalar() or 0)
    if updated <= 0:
        return {"updated": 0, "limit": limit}

    update_stmt = text(
        f"""
        WITH latest AS (
            SELECT station_id, MAX(id) AS latest_run_id
            FROM submission_agent_runs
            GROUP BY station_id
        ),
        resolved AS (
            SELECT
                s.id AS station_id,
                LOWER(COALESCE(s.status, '')) AS station_status,
                LOWER(COALESCE(r.status, '')) AS run_status,
                COALESCE(r.finished_at, r.updated_at, r.created_at, CURRENT_TIMESTAMP) AS scanned_at
            FROM stations s
            JOIN latest l ON l.station_id = s.id
            JOIN submission_agent_runs r ON r.id = l.latest_run_id
            WHERE s.scan_last_scanned_at IS NULL
            ORDER BY s.id ASC
            {limit_clause}
        )
        UPDATE stations
        SET
            scan_outcome = (
                SELECT CASE
                    WHEN resolved.station_status = 'verified' THEN 'reviewed_verified'
                    WHEN resolved.station_status = 'candidate' THEN 'reviewed_candidate'
                    WHEN resolved.run_status = 'awaiting_review' THEN 'needs_review'
                    ELSE 'reviewed_rejected'
                END
                FROM resolved
                WHERE resolved.station_id = stations.id
            ),
            scan_last_run_status = (
                SELECT resolved.run_status
                FROM resolved
                WHERE resolved.station_id = stations.id
            ),
            scan_last_scanned_at = (
                SELECT resolved.scanned_at
                FROM resolved
                WHERE resolved.station_id = stations.id
            )
        WHERE id IN (SELECT station_id FROM resolved)
        """
    )
    session.execute(update_stmt, params)
    session.commit()
    return {"updated": updated, "limit": limit}


def _main_scan_checkpoint_path() -> Path:
    return Path(".radio_db_state") / "main_scan_checkpoint.json"


def _load_main_scan_checkpoint() -> dict:
    path = _main_scan_checkpoint_path()
    if not path.exists():
        return {"last_station_id": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"last_station_id": 0}
    except Exception:
        return {"last_station_id": 0}


def _save_main_scan_checkpoint(last_station_id: int) -> None:
    path = _main_scan_checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_station_id": int(last_station_id),
                "updated_at": datetime.utcnow().isoformat() + "Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _latest_quality_decision(session: Session, station_id: int) -> str | None:
    row = session.scalar(
        select(StationSubmissionAssessment)
        .where(
            StationSubmissionAssessment.station_id == station_id,
            StationSubmissionAssessment.assessment_kind == settings.station_quality_assessment_kind,
        )
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )
    if row is None:
        return None
    payload = _json_loads(row.evidence_json, {})
    if not isinstance(payload, dict):
        return None
    decision = str(payload.get("decision") or "").strip().lower()
    return decision or None


def _same_database_url(left: str | None, right: str | None) -> bool:
    return (left or "").strip().rstrip("/") == (right or "").strip().rstrip("/")


def _is_high_value_followup_station(station: Station) -> bool:
    if station.status == StationStatus.VERIFIED:
        return True
    if int(station.priority_tier or 0) > 0:
        return True
    if float(station.confidence_score or 0.0) >= 0.75:
        return True
    return False


def _transfer_promoted_station_to_main(session: Session, station: Station) -> dict:
    return {
        "transferred": False,
        "reason": "legacy_rejected_review_transfer_disabled_use_candidate_rescan_queue",
    }
    main_url = settings.rejected_scan_main_database_url
    if not main_url:
        return {"transferred": False, "reason": "main_database_url_missing"}
    if _same_database_url(main_url, settings.database_url):
        return {"transferred": False, "reason": "main_database_url_matches_queue_database_url"}

    aliases = [a.alias for a in station.aliases or [] if a.alias]
    genres = [g.genre for g in station.genres or [] if g.genre]
    programs = list(station.programs or [])
    submissions = list(station.submissions or [])
    contacts = list(station.contacts or [])
    people = list(station.people or [])
    forms = list(station.forms or [])
    assessments = session.scalars(
        select(StationSubmissionAssessment)
        .where(StationSubmissionAssessment.station_id == station.id)
        .order_by(StationSubmissionAssessment.updated_at.desc())
    ).all()

    main_engine = create_engine(main_url, future=True)
    MainSession = sessionmaker(bind=main_engine, autoflush=False, autocommit=False, future=True)
    stats = {
        "transferred": False,
        "created_station": False,
        "updated_station": False,
        "station_id": None,
        "aliases": 0,
        "genres": 0,
        "programs": 0,
        "submission_channels": 0,
        "contacts": 0,
        "people": 0,
        "forms": 0,
        "form_fields": 0,
        "form_recipes": 0,
        "assessments": 0,
    }
    try:
        with MainSession() as main:
            conditions = []
            if station.fingerprint:
                conditions.append(Station.fingerprint == station.fingerprint)
            if station.canonical_name and station.country_code is not None:
                conditions.append(
                    (Station.canonical_name == station.canonical_name)
                    & (Station.country_code == (station.country_code or ""))
                )
            main_station = main.scalars(select(Station).where(or_(*conditions)).limit(1)).first() if conditions else None
            if main_station is None:
                main_station = Station(
                    canonical_name=station.canonical_name,
                    normalized_name=station.normalized_name,
                    country_code=station.country_code or "",
                    language=station.language or "",
                    website_url=station.website_url,
                    stream_url=station.stream_url,
                    city=station.city,
                    status=StationStatus.VERIFIED,
                    confidence_score=max(float(station.confidence_score or 0.0), 0.75),
                    priority_tier=max(1, int(station.priority_tier or 0)),
                    manual_confirmed=bool(station.manual_confirmed),
                    manual_confirmed_at=station.manual_confirmed_at,
                    fingerprint=station.fingerprint,
                )
                main.add(main_station)
                main.flush()
                stats["created_station"] = True
            else:
                main_station.status = StationStatus.VERIFIED
                main_station.priority_tier = max(int(main_station.priority_tier or 0), int(station.priority_tier or 0), 1)
                main_station.confidence_score = max(
                    float(main_station.confidence_score or 0.0),
                    float(station.confidence_score or 0.0),
                    0.75,
                )
                main_station.website_url = main_station.website_url or station.website_url
                main_station.stream_url = main_station.stream_url or station.stream_url
                main_station.city = main_station.city or station.city
                main_station.language = main_station.language or station.language or ""
                main_station.updated_at = datetime.utcnow()
                stats["updated_station"] = True
            stats["station_id"] = int(main_station.id)

            existing_aliases = {
                a.alias for a in main.scalars(select(StationAlias).where(StationAlias.station_id == main_station.id)).all()
            }
            for alias in aliases:
                if alias not in existing_aliases:
                    main.add(StationAlias(station_id=main_station.id, alias=alias))
                    existing_aliases.add(alias)
                    stats["aliases"] += 1

            existing_genres = {
                g.genre for g in main.scalars(select(StationGenre).where(StationGenre.station_id == main_station.id)).all()
            }
            for genre in genres:
                if genre not in existing_genres:
                    main.add(StationGenre(station_id=main_station.id, genre=genre))
                    existing_genres.add(genre)
                    stats["genres"] += 1

            for program in programs:
                exists = main.scalars(
                    select(StationProgram)
                    .where(
                        StationProgram.station_id == main_station.id,
                        StationProgram.name == program.name,
                        StationProgram.schedule == program.schedule,
                    )
                    .limit(1)
                ).first()
                if not exists:
                    main.add(
                        StationProgram(
                            station_id=main_station.id,
                            name=program.name,
                            description=program.description,
                            schedule=program.schedule,
                        )
                    )
                    stats["programs"] += 1

            for channel in submissions:
                exists = main.scalars(
                    select(SubmissionChannel)
                    .where(
                        SubmissionChannel.station_id == main_station.id,
                        SubmissionChannel.method == channel.method,
                        SubmissionChannel.url == channel.url,
                        SubmissionChannel.email == channel.email,
                    )
                    .limit(1)
                ).first()
                if exists:
                    exists.requirements = exists.requirements or channel.requirements
                    exists.accepts_newcomers = bool(exists.accepts_newcomers or channel.accepts_newcomers)
                    continue
                main.add(
                    SubmissionChannel(
                        station_id=main_station.id,
                        method=channel.method,
                        url=channel.url,
                        email=channel.email,
                        requirements=channel.requirements,
                        accepts_newcomers=bool(channel.accepts_newcomers),
                        manual_confirmed=bool(channel.manual_confirmed),
                        manual_confirmed_at=channel.manual_confirmed_at,
                    )
                )
                stats["submission_channels"] += 1

            for contact in contacts:
                exists = main.scalars(
                    select(StationContact)
                    .where(
                        StationContact.station_id == main_station.id,
                        StationContact.name == contact.name,
                        StationContact.role == contact.role,
                        StationContact.show_name == contact.show_name,
                        StationContact.email == contact.email,
                    )
                    .limit(1)
                ).first()
                if not exists:
                    main.add(
                        StationContact(
                            station_id=main_station.id,
                            name=contact.name,
                            role=contact.role,
                            show_name=contact.show_name,
                            email=contact.email,
                            contact_url=contact.contact_url,
                            notes=contact.notes,
                            confidence=float(contact.confidence or 0.0),
                            manual_confirmed=bool(contact.manual_confirmed),
                            manual_confirmed_at=contact.manual_confirmed_at,
                        )
                    )
                    stats["contacts"] += 1

            for person in people:
                exists = main.scalars(
                    select(StationPerson)
                    .where(
                        StationPerson.station_id == main_station.id,
                        StationPerson.name == person.name,
                        StationPerson.role == person.role,
                        StationPerson.show_name == person.show_name,
                        StationPerson.email == person.email,
                    )
                    .limit(1)
                ).first()
                if not exists:
                    main.add(
                        StationPerson(
                            station_id=main_station.id,
                            name=person.name,
                            role=person.role,
                            show_name=person.show_name,
                            email=person.email,
                            contact_url=person.contact_url,
                            linkedin_url=person.linkedin_url,
                            musical_preferences=person.musical_preferences,
                            genre_affinities_json=person.genre_affinities_json,
                            source_count=int(person.source_count or 0),
                            confidence=float(person.confidence or 0.0),
                            notes=person.notes,
                            manual_confirmed=bool(person.manual_confirmed),
                            manual_confirmed_at=person.manual_confirmed_at,
                            last_seen_at=person.last_seen_at,
                        )
                    )
                    stats["people"] += 1

            main.flush()
            for form in forms:
                main_form = main.scalars(
                    select(SubmissionForm)
                    .where(SubmissionForm.station_id == main_station.id, SubmissionForm.url == form.url)
                    .limit(1)
                ).first()
                if main_form is None:
                    main_form = SubmissionForm(
                        station_id=main_station.id,
                        url=form.url,
                        page_title=form.page_title,
                        language=form.language,
                        form_type=form.form_type,
                        status=form.status,
                        requires_login=bool(form.requires_login),
                        has_captcha=bool(form.has_captcha),
                        confidence=float(form.confidence or 0.0),
                        entry_path_json=form.entry_path_json,
                        snapshot_path=form.snapshot_path,
                        dom_snapshot_path=form.dom_snapshot_path,
                        discovered_at=form.discovered_at,
                        last_verified_at=form.last_verified_at,
                    )
                    main.add(main_form)
                    main.flush()
                    stats["forms"] += 1
                else:
                    main_form.confidence = max(float(main_form.confidence or 0.0), float(form.confidence or 0.0))
                    main_form.last_verified_at = form.last_verified_at or main_form.last_verified_at
                    main_form.updated_at = datetime.utcnow()

                existing_fields = {
                    f.field_key
                    for f in main.scalars(
                        select(SubmissionFormField).where(SubmissionFormField.form_id == main_form.id)
                    ).all()
                }
                for field in form.fields or []:
                    if field.field_key in existing_fields:
                        continue
                    main.add(
                        SubmissionFormField(
                            form_id=main_form.id,
                            field_key=field.field_key,
                            name=field.name,
                            label=field.label,
                            input_type=field.input_type,
                            required=bool(field.required),
                            placeholder=field.placeholder,
                            options_json=field.options_json,
                            validation_hint=field.validation_hint,
                            max_length=field.max_length,
                            accept_types=field.accept_types,
                            upload_max_mb=field.upload_max_mb,
                            source_snapshot_path=field.source_snapshot_path,
                        )
                    )
                    existing_fields.add(field.field_key)
                    stats["form_fields"] += 1

                existing_recipe_versions = {
                    r.version for r in main.scalars(select(FormRecipe).where(FormRecipe.form_id == main_form.id)).all()
                }
                for recipe in form.recipes or []:
                    if recipe.version in existing_recipe_versions:
                        continue
                    main.add(
                        FormRecipe(
                            form_id=main_form.id,
                            version=int(recipe.version or 1),
                            mode=recipe.mode,
                            confidence_score=float(recipe.confidence_score or 0.0),
                            status=recipe.status,
                            instructions_text=recipe.instructions_text,
                            machine_mapping_json=recipe.machine_mapping_json,
                            field_order_json=recipe.field_order_json,
                            upload_strategy_json=recipe.upload_strategy_json,
                            submit_strategy_json=recipe.submit_strategy_json,
                            success_detection_rules_json=recipe.success_detection_rules_json,
                            error_detection_rules_json=recipe.error_detection_rules_json,
                            retry_rules_json=recipe.retry_rules_json,
                            notes_for_future_runs=recipe.notes_for_future_runs,
                            discovered_at=recipe.discovered_at,
                            last_verified_at=recipe.last_verified_at,
                        )
                    )
                    existing_recipe_versions.add(recipe.version)
                    stats["form_recipes"] += 1

            for assessment in assessments:
                existing = main.scalars(
                    select(StationSubmissionAssessment)
                    .where(
                        StationSubmissionAssessment.station_id == main_station.id,
                        StationSubmissionAssessment.assessment_kind == assessment.assessment_kind,
                    )
                    .order_by(StationSubmissionAssessment.updated_at.desc())
                    .limit(1)
                ).first()
                target = existing or StationSubmissionAssessment(
                    station_id=main_station.id,
                    assessment_kind=assessment.assessment_kind,
                )
                target.status = assessment.status
                target.is_real_station = bool(assessment.is_real_station)
                target.has_real_editorial_surface = bool(assessment.has_real_editorial_surface)
                target.accepts_music_submissions = bool(assessment.accepts_music_submissions)
                target.accepts_new_artists = bool(assessment.accepts_new_artists)
                target.automation_readiness = float(assessment.automation_readiness or 0.0)
                target.risk_score = float(assessment.risk_score or 1.0)
                target.notes = assessment.notes
                target.evidence_json = assessment.evidence_json
                target.updated_at = datetime.utcnow()
                if existing is None:
                    main.add(target)
                stats["assessments"] += 1

            main.commit()
            stats["transferred"] = True
            return stats
    finally:
        main_engine.dispose()


def start_manual_scan_run(
    session: Session,
    station_id: int,
    mode: str = "scan",
    target_url: str | None = None,
    max_pages: int = 1,
    force_rescan: bool = False,
) -> SubmissionAgentRun:
    station = session.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise ValueError(f"station_not_found:{station_id}")
    if station_matches_excluded_focus(station):
        run = SubmissionAgentRun(
            station_id=station.id,
            mode=mode,
            goal="manual_scan_review",
            status="blocked",
            current_state="blocked",
            confidence=0.0,
            requires_approval=True,
            blocked_reason="excluded_focus_keyword",
            target_url=target_url or station.website_url,
            summary_json=_json_dumps(
                {
                    "reason": "excluded_focus_keyword",
                    "target_url": target_url or station.website_url,
                }
            ),
            finished_at=datetime.utcnow(),
        )
        session.add(run)
        session.flush()
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="excluded_focus_keyword",
            severity="high",
            title="Station per Fokus-Keyword ausgeschlossen",
            details="Station wurde wegen ausgeschlossenen Genre/Fokus nicht gescannt.",
            payload={"station_name": station.canonical_name},
        )
        session.commit()
        session.refresh(run)
        return run
    if station_matches_excluded_meta(station):
        run = SubmissionAgentRun(
            station_id=station.id,
            mode=mode,
            goal="manual_scan_review",
            status="blocked",
            current_state="blocked",
            confidence=0.0,
            requires_approval=True,
            blocked_reason="excluded_meta_station",
            target_url=target_url or station.website_url,
            summary_json=_json_dumps(
                {
                    "reason": "excluded_meta_station",
                    "target_url": target_url or station.website_url,
                }
            ),
            finished_at=datetime.utcnow(),
        )
        session.add(run)
        session.flush()
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="excluded_meta_station",
            severity="high",
            title="Meta-/Verzeichnis-Eintrag ausgeschlossen",
            details="Station wurde als Liste/Verzeichnis/Artikel erkannt und nicht gescannt.",
            payload={"station_name": station.canonical_name, "website": station.website_url},
        )
        session.commit()
        session.refresh(run)
        return run
    if station_matches_entrypoint_only(station):
        run = SubmissionAgentRun(
            station_id=station.id,
            mode=mode,
            goal="manual_scan_review",
            status="blocked",
            current_state="blocked",
            confidence=0.0,
            requires_approval=True,
            blocked_reason="excluded_entrypoint_station",
            target_url=target_url or station.website_url,
            summary_json=_json_dumps(
                {
                    "reason": "excluded_entrypoint_station",
                    "target_url": target_url or station.website_url,
                }
            ),
            finished_at=datetime.utcnow(),
        )
        session.add(run)
        session.flush()
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="excluded_entrypoint_station",
            severity="high",
            title="Submission-Entrypoint statt Sender",
            details="Eintrag ist ein Submission-Entrypoint ohne klaren Senderknoten und wird nicht als Station gescannt.",
            payload={"station_name": station.canonical_name, "website": station.website_url},
        )
        session.commit()
        session.refresh(run)
        return run
    if not is_supported_station_target_url(target_url or station.website_url):
        run = SubmissionAgentRun(
            station_id=station.id,
            mode=mode,
            goal="manual_scan_review",
            status="blocked",
            current_state="blocked",
            confidence=0.0,
            requires_approval=True,
            blocked_reason="unsupported_domain",
            target_url=target_url or station.website_url,
            summary_json=_json_dumps(
                {
                    "reason": "unsupported_domain",
                    "target_url": target_url or station.website_url,
                }
            ),
            finished_at=datetime.utcnow(),
        )
        session.add(run)
        session.flush()
        _record_issue(
            session=session,
            run=run,
            step=None,
            issue_type="unsupported_domain",
            severity="high",
            title="Nicht unterstützte Station-URL",
            details="Community-, Aggregator- oder Verzeichnis-Domain wird im Qualitäts-Scan ausgeschlossen.",
            payload={"url": target_url or station.website_url},
        )
        session.commit()
        session.refresh(run)
        return run

    assessment = build_station_submission_assessment(session, station)
    candidate_urls = _candidate_urls_for_station(
        station,
        max_urls=max(1, max_pages * 3, settings.browser_deep_max_urls_per_station),
    )
    if target_url:
        candidate_urls = [(target_url, ["manual_target"])] + [
            (url, entry_path) for url, entry_path in candidate_urls if url != target_url
        ]
    candidate_urls = _rank_candidate_scan_urls(candidate_urls)

    run = SubmissionAgentRun(
        station_id=station.id,
        mode=mode,
        goal="manual_scan_review",
        status="running",
        current_state="station_review",
        confidence=assessment.automation_readiness,
        requires_approval=True,
        target_url=target_url or (candidate_urls[0][0] if candidate_urls else station.website_url),
        summary_json=_json_dumps(
            {
                "candidate_urls": [url for url, _ in candidate_urls[:max(1, max_pages)]],
                "force_rescan": force_rescan,
            }
        ),
    )
    session.add(run)
    session.flush()

    step_index = 1
    review_observation = {
        "station_name": station.canonical_name,
        "website_url": station.website_url,
        "assessment": _json_loads(assessment.evidence_json, {}),
        "existing_submissions": len(station.submissions),
        "existing_forms": len(station.forms),
    }
    review_action = {
        "type": "review_candidates",
        "manual_only": True,
        "candidate_urls": [url for url, _ in candidate_urls[:max(1, max_pages)]],
    }
    review_result = {
        "assessment_notes": assessment.notes,
        "automation_readiness": assessment.automation_readiness,
        "risk_score": assessment.risk_score,
    }
    review_step = _record_step(
        session=session,
        run=run,
        step_index=step_index,
        state_before="station_review",
        state_after="entry_navigation",
        observation=review_observation,
        proposed_action=review_action,
        execution_result=review_result,
        confidence=assessment.automation_readiness,
    )
    if not candidate_urls:
        _record_issue(
            session=session,
            run=run,
            step=review_step,
            issue_type="no_candidate_url",
            severity="high",
            title="Keine Kandidat-URL gefunden",
            details="Fuer diesen Sender liegt weder eine geeignete Submission-URL noch eine Website vor.",
        )
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "no_candidate_url"
        run.finished_at = datetime.utcnow()
        session.commit()
        return run

    forms_saved = 0
    pages_scanned = 0
    emails_saved = 0
    strong_form_count = 0
    primary_form_id: int | None = None
    page_evidence: list[dict] = []

    scan_errors = 0
    scan_limit = max(1, int(max_pages))
    max_candidate_pool = max(
        scan_limit,
        scan_limit * 4,
        int(settings.browser_deep_max_urls_per_station or 0),
    )
    scan_index = 0
    while scan_index < len(candidate_urls) and pages_scanned < scan_limit:
        url, entry_path = candidate_urls[scan_index]
        scan_index += 1
        state_before = "entry_navigation"
        t0 = time.perf_counter()
        try:
            try:
                meta = _extract_forms_playwright(url)
                extraction_mode = "playwright"
            except Exception as exc:
                meta = _extract_forms_fallback(url)
                extraction_mode = f"fallback:{type(exc).__name__}"
        except Exception as exc:
            step_index += 1
            failed_step = _record_step(
                session=session,
                run=run,
                step_index=step_index,
                state_before=state_before,
                state_after="entry_navigation",
                observation={"url": url, "entry_path": entry_path},
                proposed_action={"type": "continue_scan", "manual_only": True},
                execution_result={"error": str(exc)},
                confidence=0.0,
            )
            _record_issue(
                session=session,
                run=run,
                step=failed_step,
                issue_type="scan_error",
                severity="high",
                title="Seite konnte nicht gescannt werden",
                details=str(exc),
                payload={"url": url},
            )
            scan_errors += 1
            continue

        effective_url = str(meta.get("url") or url)
        pages_scanned += 1
        _append_ranked_scan_links(candidate_urls, meta, max_candidates=max_candidate_pool)
        visible_forms = meta.get("forms") or []
        discovered_form_ids: list[int] = []
        scan_state_after = "form_understanding" if visible_forms else "blocked"
        observation = {
            "url": effective_url,
            "requested_url": url,
            "entry_path": entry_path,
            "page_title": meta.get("title") or "",
            "language": meta.get("language") or "",
            "form_count": len(visible_forms),
            "has_captcha": bool(meta.get("has_captcha")),
            "requires_login": bool(meta.get("requires_login")),
            "extraction_mode": extraction_mode,
        }
        proposed_action = {
            "type": "manual_review",
            "manual_only": True,
            "suggested_next_step": "Inspect detected fields and decide whether this page is a real submission surface.",
        }
        execution_result = {
            "snapshot_path": _safe_artifact_path(meta.get("snapshot_path")),
            "dom_snapshot_path": _safe_artifact_path(meta.get("dom_snapshot_path")),
        }

        if meta.get("has_captcha"):
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="captcha",
                severity="high",
                title="Captcha erkannt",
                details=f"Die Seite {url} enthaelt CAPTCHA-Signale und bleibt manuell.",
                payload={"url": effective_url, "requested_url": url},
            )
        if meta.get("requires_login"):
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="login_required",
                severity="high",
                title="Login erforderlich",
                details=f"Die Seite {effective_url} scheint einen Login zu verlangen.",
                payload={"url": effective_url, "requested_url": url},
            )
        if not visible_forms:
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="no_form_detected",
                severity="medium",
                title="Kein Formular erkannt",
                details=f"Auf {effective_url} wurde kein Formular gefunden.",
                payload={"url": effective_url, "requested_url": url},
            )
        page_emails = [
            str(x).strip().lower()
            for x in (meta.get("emails") or [])
            if str(x).strip() and _is_valid_discovered_email(str(x).strip())
        ]
        if page_emails:
            emails_saved += _persist_email_channels(session=session, station=station, source_url=effective_url, emails=page_emails)

        for form_payload in visible_forms:
            form_row = session.scalar(
                select(SubmissionForm).where(
                    SubmissionForm.station_id == station.id,
                    SubmissionForm.url == effective_url,
                )
            )
            if form_row is None or force_rescan:
                form_row = _persist_form(
                    session=session,
                    station=station,
                    page_url=effective_url,
                    entry_path=entry_path,
                    form_payload=form_payload,
                    mode="manual_scan" if force_rescan else "read",
                    scan_meta=meta,
                )
            forms_saved += 1
            if form_row.form_type in {FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER}:
                strong_form_count += 1
            discovered_form_ids.append(form_row.id)
            if primary_form_id is None:
                primary_form_id = form_row.id
            if form_row.has_captcha:
                _record_issue(
                    session=session,
                    run=run,
                    step=None,
                    issue_type="restricted_form",
                    severity="high",
                    title="Formular eingeschraenkt",
                    details=f"Form {form_row.id} ist wegen CAPTCHA oder Login nicht automatisierbar.",
                    payload={"form_id": form_row.id, "status": form_row.status.value},
                )

        page_evidence.append(_page_evidence_from_meta(url=url, entry_path=entry_path, meta=meta, visible_forms=visible_forms))
        execution_result["discovered_form_ids"] = discovered_form_ids
        execution_result["saved_form_count"] = len(discovered_form_ids)
        execution_result["page_evidence"] = page_evidence[-1]
        execution_result["page_domain_matches_station"] = _domain(effective_url) == _domain(station.website_url)
        step_index += 1
        step = _record_step(
            session=session,
            run=run,
            step_index=step_index,
            state_before=state_before,
            state_after=scan_state_after,
            observation=observation,
            proposed_action=proposed_action,
            execution_result=execution_result,
            screenshot_path=execution_result["snapshot_path"],
            dom_snapshot_path=execution_result["dom_snapshot_path"],
            confidence=0.85 if visible_forms else 0.35,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        if not visible_forms:
            _record_issue(
                session=session,
                run=run,
                step=step,
                issue_type="manual_followup",
                severity="medium",
                title="Manuelle Nachpruefung empfohlen",
                details="Ohne erkanntes Formular bleibt der Sender im Scan-Lab zur Sichtpruefung.",
                payload={"url": url},
            )
        if _has_sufficient_submission_evidence(page_evidence=page_evidence, strong_form_count=strong_form_count):
            break

    if force_rescan:
        _stale_missing_force_rescan_forms(session=session, station_id=int(station.id), page_evidence=page_evidence)

    run.form_id = primary_form_id
    summary = {
        "pages_scanned": pages_scanned,
        "forms_saved": forms_saved,
        "emails_saved": emails_saved,
        "contact_only_review": bool(forms_saved == 0 and emails_saved > 0),
        "assessment_id": assessment.id,
        "primary_form_id": primary_form_id,
        "target_url": run.target_url,
        "candidate_urls": [url for url, _ in candidate_urls[: max(1, max_pages)]],
        "discovered_candidate_urls": [url for url, _ in candidate_urls[:max_candidate_pool]],
        "page_evidence": page_evidence,
        "submission_evidence_summary": {
            "pages_with_forms": sum(1 for p in page_evidence if int(p.get("form_count") or 0) > 0),
            "pages_with_emails": sum(1 for p in page_evidence if p.get("emails")),
            "pages_with_submission_keywords": sum(1 for p in page_evidence if p.get("submission_keyword_contexts")),
            "emails_near_submission_signal": sum(
                1
                for p in page_evidence
                for ctx in (p.get("email_contexts") or [])
                if ctx.get("near_submission_signal")
            ),
        },
    }
    run.summary_json = _json_dumps(summary)
    if run.status == "running":
        run.status = "awaiting_review"
        run.current_state = "review"
    if forms_saved == 0 and pages_scanned > 0 and emails_saved == 0:
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "no_form_detected"
    elif forms_saved == 0 and emails_saved > 0:
        run.status = "awaiting_review"
        run.current_state = "review"
        run.blocked_reason = None
    if pages_scanned == 0 and scan_errors > 0:
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "scan_error"
    run.confidence = assessment.automation_readiness if forms_saved else min(assessment.automation_readiness, 0.45)
    run.finished_at = datetime.utcnow()
    session.commit()
    session.refresh(run)
    return run


def _extract_rejected_scan_payload(payload: dict) -> dict:
    pages: list[dict] = []
    max_pages = max(1, int(payload.get("max_pages") or 1))
    candidate_urls = [
        (str(url), entry_path if isinstance(entry_path, list) else [])
        for url, entry_path in payload.get("candidate_urls", [])
    ]
    max_candidate_pool = max(max_pages, max_pages * 4, int(settings.browser_deep_max_urls_per_station or 0))
    scan_index = 0
    while scan_index < len(candidate_urls) and len(pages) < max_pages:
        url, entry_path = candidate_urls[scan_index]
        scan_index += 1
        t0 = time.perf_counter()
        try:
            if not is_supported_station_target_url(url):
                raise RuntimeError("unsupported_stream_or_non_page_url")
            try:
                meta = _extract_forms_playwright(url)
                extraction_mode = "playwright"
            except Exception as exc:
                try:
                    final_url = url
                    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                        with client.stream("GET", url, headers={"User-Agent": settings.crawl_user_agent}) as response:
                            response.raise_for_status()
                            final_url = str(response.url)
                            content_type = response.headers.get("content-type", "").lower()
                            if not any(token in content_type for token in ("text/html", "application/xhtml", "text/plain", "")):
                                raise RuntimeError(f"unsupported_content_type:{content_type[:80]}")
                            chunks: list[bytes] = []
                            total = 0
                            for chunk in response.iter_bytes():
                                if not chunk:
                                    continue
                                chunks.append(chunk)
                                total += len(chunk)
                                if total >= 1_000_000:
                                    break
                    html = b"".join(chunks).decode("utf-8", errors="replace")
                    meta = _extract_forms_fallback_payload(url=url, html=html)
                    meta["url"] = final_url
                    meta["requested_url"] = url
                    extraction_mode = f"fast_fallback:{type(exc).__name__}"
                except Exception:
                    raise exc
            _append_ranked_scan_links(candidate_urls, meta, max_candidates=max_candidate_pool)
            pages.append(
                {
                    "url": url,
                    "entry_path": entry_path,
                    "meta": meta,
                    "extraction_mode": extraction_mode,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                }
            )
        except Exception as exc:
            pages.append(
                {
                    "url": url,
                    "entry_path": entry_path,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                }
            )
    return {"station_id": int(payload["station_id"]), "pages": pages}


def _extract_forms_fallback_payload(url: str, html: str) -> dict:
    from radio_db.services.forms import _extract_emails, _extract_links_from_html

    lowered = html.lower()
    emails = _extract_emails(html)
    return {
        "url": url,
        "title": "",
        "language": "",
        "requires_login": ("type=\"password\"" in lowered) or ("name=\"password\"" in lowered),
        "has_captcha": ("captcha" in lowered),
        "forms": [],
        "links": [
            {"href": href, "text": text, "score": round(float(score), 3)}
            for href, text, score in _extract_links_from_html(html, base_url=url, station_domain=_domain(url))[:300]
        ],
        "emails": emails,
        "email_contexts": _email_contexts(html, emails),
        "submission_keyword_contexts": _snippet_contexts(html, DEEP_SUBMISSION_HINTS),
        "snapshot_path": "",
        "dom_snapshot_path": "",
    }


def _persist_extracted_scan_run(
    session: Session,
    *,
    station_id: int,
    candidate_urls: list[tuple[str, list[str]]],
    pages: list[dict],
    max_pages: int,
    force_rescan: bool = False,
) -> SubmissionAgentRun:
    station = session.scalar(select(Station).where(Station.id == station_id))
    if station is None:
        raise ValueError(f"station_not_found:{station_id}")

    assessment = build_station_submission_assessment(session, station)
    run = SubmissionAgentRun(
        station_id=station.id,
        mode="scan",
        goal="manual_scan_review",
        status="running",
        current_state="station_review",
        confidence=assessment.automation_readiness,
        requires_approval=True,
        target_url=(candidate_urls[0][0] if candidate_urls else station.website_url),
        summary_json=_json_dumps(
            {
                "candidate_urls": [url for url, _ in candidate_urls[: max(1, max_pages)]],
                "dispatch_mode": True,
                "force_rescan": force_rescan,
            }
        ),
    )
    session.add(run)
    session.flush()

    step_index = 1
    review_step = _record_step(
        session=session,
        run=run,
        step_index=step_index,
        state_before="station_review",
        state_after="entry_navigation",
        observation={
            "station_name": station.canonical_name,
            "website_url": station.website_url,
            "assessment": _json_loads(assessment.evidence_json, {}),
            "existing_submissions": len(station.submissions),
            "existing_forms": len(station.forms),
            "dispatch_mode": True,
        },
        proposed_action={
            "type": "review_candidates",
            "manual_only": True,
            "candidate_urls": [url for url, _ in candidate_urls[: max(1, max_pages)]],
        },
        execution_result={
            "assessment_notes": assessment.notes,
            "automation_readiness": assessment.automation_readiness,
            "risk_score": assessment.risk_score,
        },
        confidence=assessment.automation_readiness,
    )
    if not candidate_urls:
        _record_issue(
            session=session,
            run=run,
            step=review_step,
            issue_type="no_candidate_url",
            severity="high",
            title="Keine Kandidat-URL gefunden",
            details="Fuer diesen Sender liegt weder eine geeignete Submission-URL noch eine Website vor.",
        )
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "no_candidate_url"
        run.finished_at = datetime.utcnow()
        session.commit()
        return run

    forms_saved = 0
    pages_scanned = 0
    emails_saved = 0
    scan_errors = 0
    primary_form_id: int | None = None
    page_evidence: list[dict] = []

    strong_form_count = 0
    for page_payload in pages:
        url = str(page_payload.get("url") or "")
        entry_path = page_payload.get("entry_path") or []
        if page_payload.get("error"):
            step_index += 1
            failed_step = _record_step(
                session=session,
                run=run,
                step_index=step_index,
                state_before="entry_navigation",
                state_after="entry_navigation",
                observation={"url": url, "entry_path": entry_path},
                proposed_action={"type": "continue_scan", "manual_only": True},
                execution_result={"error": str(page_payload.get("error"))},
                confidence=0.0,
                latency_ms=int(page_payload.get("latency_ms") or 0),
            )
            _record_issue(
                session=session,
                run=run,
                step=failed_step,
                issue_type="scan_error",
                severity="high",
                title="Seite konnte nicht gescannt werden",
                details=str(page_payload.get("error")),
                payload={"url": url},
            )
            scan_errors += 1
            continue

        meta = page_payload.get("meta") if isinstance(page_payload.get("meta"), dict) else {}
        effective_url = str(meta.get("url") or url)
        pages_scanned += 1
        visible_forms = meta.get("forms") or []
        discovered_form_ids: list[int] = []
        scan_state_after = "form_understanding" if visible_forms else "blocked"
        observation = {
            "url": effective_url,
            "requested_url": url,
            "entry_path": entry_path,
            "page_title": meta.get("title") or "",
            "language": meta.get("language") or "",
            "form_count": len(visible_forms),
            "has_captcha": bool(meta.get("has_captcha")),
            "requires_login": bool(meta.get("requires_login")),
            "extraction_mode": page_payload.get("extraction_mode") or "",
        }
        execution_result = {
            "snapshot_path": _safe_artifact_path(meta.get("snapshot_path")),
            "dom_snapshot_path": _safe_artifact_path(meta.get("dom_snapshot_path")),
        }

        if meta.get("has_captcha"):
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="captcha",
                severity="high",
                title="Captcha erkannt",
                details=f"Die Seite {effective_url} enthaelt CAPTCHA-Signale und bleibt manuell.",
                payload={"url": effective_url, "requested_url": url},
            )
        if meta.get("requires_login"):
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="login_required",
                severity="high",
                title="Login erforderlich",
                details=f"Die Seite {effective_url} scheint einen Login zu verlangen.",
                payload={"url": effective_url, "requested_url": url},
            )
        if not visible_forms:
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="no_form_detected",
                severity="medium",
                title="Kein Formular erkannt",
                details=f"Auf {effective_url} wurde kein Formular gefunden.",
                payload={"url": effective_url, "requested_url": url},
            )

        page_emails = [
            str(x).strip().lower()
            for x in (meta.get("emails") or [])
            if str(x).strip() and _is_valid_discovered_email(str(x).strip())
        ]
        if page_emails:
            emails_saved += _persist_email_channels(session=session, station=station, source_url=effective_url, emails=page_emails)

        for form_payload in visible_forms:
            form_row = session.scalar(
                select(SubmissionForm).where(
                    SubmissionForm.station_id == station.id,
                    SubmissionForm.url == effective_url,
                )
            )
            if form_row is None or force_rescan:
                form_row = _persist_form(
                    session=session,
                    station=station,
                    page_url=effective_url,
                    entry_path=entry_path,
                    form_payload=form_payload,
                    mode="read",
                    scan_meta=meta,
                )
            forms_saved += 1
            if form_row.form_type in {FormType.MUSIC_SUBMISSION, FormType.ARTIST_UPLOAD, FormType.NEWCOMER}:
                strong_form_count += 1
            discovered_form_ids.append(form_row.id)
            if primary_form_id is None:
                primary_form_id = form_row.id
            if form_row.has_captcha:
                _record_issue(
                    session=session,
                    run=run,
                    step=None,
                    issue_type="restricted_form",
                    severity="high",
                    title="Formular eingeschraenkt",
                    details=f"Form {form_row.id} ist wegen CAPTCHA oder Login nicht automatisierbar.",
                    payload={"form_id": form_row.id, "status": form_row.status.value},
                )

        page_evidence.append(_page_evidence_from_meta(url=url, entry_path=entry_path, meta=meta, visible_forms=visible_forms))
        execution_result["discovered_form_ids"] = discovered_form_ids
        execution_result["saved_form_count"] = len(discovered_form_ids)
        execution_result["page_evidence"] = page_evidence[-1]
        execution_result["page_domain_matches_station"] = _domain(effective_url) == _domain(station.website_url)
        step_index += 1
        step = _record_step(
            session=session,
            run=run,
            step_index=step_index,
            state_before="entry_navigation",
            state_after=scan_state_after,
            observation=observation,
            proposed_action={
                "type": "manual_review",
                "manual_only": True,
                "suggested_next_step": "Inspect detected fields and decide whether this page is a real submission surface.",
            },
            execution_result=execution_result,
            screenshot_path=execution_result["snapshot_path"],
            dom_snapshot_path=execution_result["dom_snapshot_path"],
            confidence=0.85 if visible_forms else 0.35,
            latency_ms=int(page_payload.get("latency_ms") or 0),
        )
        if not visible_forms:
            _record_issue(
                session=session,
                run=run,
                step=step,
                issue_type="manual_followup",
                severity="medium",
                title="Manuelle Nachpruefung empfohlen",
                details="Ohne erkanntes Formular bleibt der Sender im Scan-Lab zur Sichtpruefung.",
                payload={"url": url},
            )

    if force_rescan:
        _stale_missing_force_rescan_forms(session=session, station_id=int(station.id), page_evidence=page_evidence)

    run.form_id = primary_form_id
    run.summary_json = _json_dumps(
        {
            "pages_scanned": pages_scanned,
            "forms_saved": forms_saved,
            "emails_saved": emails_saved,
            "contact_only_review": bool(forms_saved == 0 and emails_saved > 0),
            "assessment_id": assessment.id,
            "primary_form_id": primary_form_id,
            "target_url": run.target_url,
            "candidate_urls": [url for url, _ in candidate_urls[: max(1, max_pages)]],
            "page_evidence": page_evidence,
            "submission_evidence_summary": {
                "pages_with_forms": sum(1 for p in page_evidence if int(p.get("form_count") or 0) > 0),
                "pages_with_emails": sum(1 for p in page_evidence if p.get("emails")),
                "pages_with_submission_keywords": sum(1 for p in page_evidence if p.get("submission_keyword_contexts")),
                "emails_near_submission_signal": sum(
                    1
                    for p in page_evidence
                    for ctx in (p.get("email_contexts") or [])
                    if ctx.get("near_submission_signal")
                ),
            },
            "dispatch_mode": True,
        }
    )
    if run.status == "running":
        run.status = "awaiting_review"
        run.current_state = "review"
    if forms_saved == 0 and pages_scanned > 0 and emails_saved == 0:
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "no_form_detected"
    elif forms_saved == 0 and emails_saved > 0:
        run.status = "awaiting_review"
        run.current_state = "review"
        run.blocked_reason = None
    if pages_scanned == 0 and scan_errors > 0:
        run.status = "blocked"
        run.current_state = "blocked"
        run.blocked_reason = "scan_error"
    run.confidence = assessment.automation_readiness if forms_saved else min(assessment.automation_readiness, 0.45)
    run.finished_at = datetime.utcnow()
    session.commit()
    session.refresh(run)
    return run


def run_rejected_scan_cycle(
    session: Session,
    station_limit: int = 20,
    max_pages: int = 3,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
) -> dict:
    if reset_checkpoint:
        _save_rejected_scan_checkpoint(last_station_id=0)
    checkpoint = _load_rejected_scan_checkpoint() if use_checkpoint else {"last_station_id": 0}
    checkpoint_before = int(checkpoint.get("last_station_id", 0))
    claimed_ids, max_seen_id, wrapped = _claim_rejected_scan_batch(
        session=session,
        station_limit=station_limit,
        use_checkpoint=use_checkpoint,
    )
    stations = (
        session.scalars(select(Station).where(Station.id.in_(claimed_ids)).order_by(Station.id.asc())).all()
        if claimed_ids
        else []
    )

    processed = 0
    run_created = 0
    blocked = 0
    awaiting_review = 0
    errors = 0
    skipped_prefilter = 0
    llm_processed = 0
    llm_saved = 0
    llm_skipped_budget = 0
    llm_errors = 0
    llm_promoted = 0
    llm_review = 0
    llm_rejected = 0
    llm_transferred = 0
    llm_transfer_errors = 0
    last_runs: list[dict] = []

    for st in stations:
        if processed >= station_limit:
            break
        if (
            not is_supported_station_target_url(st.website_url)
            or station_matches_excluded_focus(st)
            or station_matches_excluded_meta(st)
            or station_matches_entrypoint_only(st)
        ):
            skipped_prefilter += 1
            _finalize_rejected_scan_station(
                session=session,
                station_id=int(st.id),
                outcome="prefilter_rejected",
                run_status="prefilter_skipped",
                release_claim=True,
            )
            session.commit()
            continue
        processed += 1
        try:
            run = start_manual_scan_run(
                session=session,
                station_id=int(st.id),
                max_pages=max(1, max_pages),
                force_rescan=False,
            )
            run_created += 1
            if run.status == "blocked":
                blocked += 1
            if run.status == "awaiting_review":
                awaiting_review += 1
                try:
                    llm_result = run_station_quality_verification(
                        session=session,
                        limit=1,
                        station_id=int(st.id),
                        provider=settings.station_quality_provider,
                        only_unassessed=True,
                        apply=True,
                    )
                    llm_processed += int(llm_result.get("processed", 0) or 0)
                    llm_saved += int(llm_result.get("saved", 0) or 0)
                    llm_skipped_budget += int(llm_result.get("skipped_budget", 0) or 0)
                    llm_errors += int(llm_result.get("errors", 0) or 0)
                    decision = _latest_quality_decision(session=session, station_id=int(st.id))
                    if decision == "promote":
                        transfer_result = _transfer_promoted_station_to_main(session=session, station=st)
                        if transfer_result.get("transferred"):
                            st.status = StationStatus.VERIFIED
                            st.priority_tier = max(1, int(st.priority_tier or 0))
                            st.confidence_score = max(float(st.confidence_score or 0.0), 0.75)
                            session.add(st)
                            session.commit()
                            llm_promoted += 1
                            llm_transferred += 1
                            _finalize_rejected_scan_station(
                                session=session,
                                station_id=int(st.id),
                                outcome="reviewed_verified",
                                run_status=run.status,
                                scanned_at=run.finished_at,
                                release_claim=True,
                            )
                        else:
                            llm_transfer_errors += 1
                            _finalize_rejected_scan_station(
                                session=session,
                                station_id=int(st.id),
                                outcome="needs_review",
                                run_status=run.status,
                                scanned_at=run.finished_at,
                                release_claim=True,
                            )
                    elif decision == "reject":
                        st.status = StationStatus.REJECTED
                        st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
                        session.add(st)
                        session.commit()
                        llm_rejected += 1
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=int(st.id),
                            outcome="reviewed_rejected",
                            run_status=run.status,
                            scanned_at=run.finished_at,
                            release_claim=True,
                        )
                    elif decision == "review":
                        llm_review += 1
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=int(st.id),
                            outcome="needs_review",
                            run_status=run.status,
                            scanned_at=run.finished_at,
                            release_claim=True,
                        )
                except Exception:
                    session.rollback()
                    llm_errors += 1
                    _finalize_rejected_scan_station(
                        session=session,
                        station_id=int(st.id),
                        outcome="retry",
                        run_status=run.status,
                        scanned_at=run.finished_at,
                        release_claim=True,
                        error="llm_exception",
                    )
            elif run.status == "blocked":
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=int(st.id),
                    outcome="reviewed_rejected",
                    run_status=run.status,
                    scanned_at=run.finished_at,
                    release_claim=True,
                )
            else:
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=int(st.id),
                    outcome="needs_review",
                    run_status=run.status,
                    scanned_at=run.finished_at,
                    release_claim=True,
                )
            session.commit()
            if len(last_runs) < 20:
                last_runs.append(
                    {
                        "station_id": st.id,
                        "station_name": st.canonical_name,
                        "run_id": run.id,
                        "run_status": run.status,
                        "blocked_reason": run.blocked_reason,
                    }
                )
        except Exception:
            session.rollback()
            errors += 1
            _finalize_rejected_scan_station(
                session=session,
                station_id=int(st.id),
                outcome="scan_error",
                run_status="worker_exception",
                release_claim=True,
                error="worker_exception",
            )
            session.commit()

    return {
        "stations_considered": len(stations),
        "stations_processed": processed,
        "runs_created": run_created,
        "awaiting_review": awaiting_review,
        "blocked": blocked,
        "errors": errors,
        "skipped_prefilter": skipped_prefilter,
        "llm_processed": llm_processed,
        "llm_saved": llm_saved,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_errors": llm_errors,
        "llm_promoted": llm_promoted,
        "llm_review": llm_review,
        "llm_rejected": llm_rejected,
        "llm_transferred": llm_transferred,
        "llm_transfer_errors": llm_transfer_errors,
        "checkpoint_before": checkpoint_before,
        "checkpoint_after": int(max_seen_id),
        "wrapped": wrapped,
        "last_runs": last_runs,
    }


def run_rejected_scan_dispatch(
    session: Session,
    station_limit: int = 45,
    max_pages: int = 3,
    workers: int = 3,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
) -> dict:
    if reset_checkpoint:
        _save_rejected_scan_checkpoint(last_station_id=0)
    checkpoint = _load_rejected_scan_checkpoint() if use_checkpoint else {"last_station_id": 0}
    checkpoint_before = int(checkpoint.get("last_station_id", 0))
    claimed_ids, max_seen_id, wrapped = _claim_rejected_scan_batch(
        session=session,
        station_limit=station_limit,
        use_checkpoint=use_checkpoint,
    )
    stations = (
        session.scalars(select(Station).where(Station.id.in_(claimed_ids)).order_by(Station.id.asc())).all()
        if claimed_ids
        else []
    )

    processed = 0
    run_created = 0
    blocked = 0
    awaiting_review = 0
    errors = 0
    skipped_prefilter = 0
    llm_processed = 0
    llm_saved = 0
    llm_skipped_budget = 0
    llm_errors = 0
    llm_promoted = 0
    llm_review = 0
    llm_rejected = 0
    llm_transferred = 0
    llm_transfer_errors = 0
    last_runs: list[dict] = []
    worker_payloads: list[dict] = []
    candidate_by_station: dict[int, list[tuple[str, list[str]]]] = {}

    for st in stations:
        if processed >= station_limit:
            break
        station_id = int(st.id)
        if (
            not is_supported_station_target_url(st.website_url)
            or station_matches_excluded_focus(st)
            or station_matches_excluded_meta(st)
            or station_matches_entrypoint_only(st)
        ):
            skipped_prefilter += 1
            _finalize_rejected_scan_station(
                session=session,
                station_id=station_id,
                outcome="prefilter_rejected",
                run_status="prefilter_skipped",
                release_claim=True,
            )
            session.commit()
            continue
        candidate_urls = _candidate_urls_for_station(
            st,
            max_urls=max(1, max_pages * 3, settings.browser_deep_max_urls_per_station),
            include_deep_pass=False,
        )
        candidate_urls = _rank_candidate_scan_urls(candidate_urls)
        selected_urls = candidate_urls[: max(1, max_pages)]
        candidate_by_station[station_id] = selected_urls
        worker_payloads.append(
            {
                "station_id": station_id,
                "station_name": st.canonical_name,
                "website_url": st.website_url,
                "candidate_urls": selected_urls,
                "max_pages": max(1, max_pages),
            }
        )
        processed += 1

    max_workers = max(1, int(workers or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_extract_rejected_scan_payload, payload): payload for payload in worker_payloads}
        for future in as_completed(future_map):
            payload = future_map[future]
            station_id = int(payload["station_id"])
            try:
                extraction = future.result()
                run = _persist_extracted_scan_run(
                    session=session,
                    station_id=station_id,
                    candidate_urls=candidate_by_station.get(station_id, []),
                    pages=extraction.get("pages") or [],
                    max_pages=max(1, max_pages),
                )
                run_created += 1
                if run.status == "blocked":
                    blocked += 1
                if run.status == "awaiting_review":
                    awaiting_review += 1
                    try:
                        llm_result = run_station_quality_verification(
                            session=session,
                            limit=1,
                            station_id=station_id,
                            provider=settings.station_quality_provider,
                            only_unassessed=True,
                            apply=True,
                        )
                        llm_processed += int(llm_result.get("processed", 0) or 0)
                        llm_saved += int(llm_result.get("saved", 0) or 0)
                        llm_skipped_budget += int(llm_result.get("skipped_budget", 0) or 0)
                        llm_errors += int(llm_result.get("errors", 0) or 0)
                        decision = _latest_quality_decision(session=session, station_id=station_id)
                        station = session.scalar(select(Station).where(Station.id == station_id))
                        if station is None:
                            raise ValueError(f"station_not_found:{station_id}")
                        if decision == "promote":
                            transfer_result = _transfer_promoted_station_to_main(session=session, station=station)
                            if transfer_result.get("transferred"):
                                station.status = StationStatus.VERIFIED
                                station.priority_tier = max(1, int(station.priority_tier or 0))
                                station.confidence_score = max(float(station.confidence_score or 0.0), 0.75)
                                session.add(station)
                                session.commit()
                                llm_promoted += 1
                                llm_transferred += 1
                                _finalize_rejected_scan_station(
                                    session=session,
                                    station_id=station_id,
                                    outcome="reviewed_verified",
                                    run_status=run.status,
                                    scanned_at=run.finished_at,
                                    release_claim=True,
                                )
                            else:
                                llm_transfer_errors += 1
                                _finalize_rejected_scan_station(
                                    session=session,
                                    station_id=station_id,
                                    outcome="needs_review",
                                    run_status=run.status,
                                    scanned_at=run.finished_at,
                                    release_claim=True,
                                )
                        elif decision == "reject":
                            station.status = StationStatus.REJECTED
                            station.confidence_score = min(float(station.confidence_score or 0.0), 0.2)
                            session.add(station)
                            session.commit()
                            llm_rejected += 1
                            _finalize_rejected_scan_station(
                                session=session,
                                station_id=station_id,
                                outcome="reviewed_rejected",
                                run_status=run.status,
                                scanned_at=run.finished_at,
                                release_claim=True,
                            )
                        elif decision == "review":
                            llm_review += 1
                            _finalize_rejected_scan_station(
                                session=session,
                                station_id=station_id,
                                outcome="needs_review",
                                run_status=run.status,
                                scanned_at=run.finished_at,
                                release_claim=True,
                            )
                    except Exception:
                        session.rollback()
                        llm_errors += 1
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=station_id,
                            outcome="retry",
                            run_status=run.status,
                            scanned_at=run.finished_at,
                            release_claim=True,
                            error="llm_exception",
                        )
                elif run.status == "blocked":
                    _finalize_rejected_scan_station(
                        session=session,
                        station_id=station_id,
                        outcome="reviewed_rejected",
                        run_status=run.status,
                        scanned_at=run.finished_at,
                        release_claim=True,
                    )
                else:
                    _finalize_rejected_scan_station(
                        session=session,
                        station_id=station_id,
                        outcome="needs_review",
                        run_status=run.status,
                        scanned_at=run.finished_at,
                        release_claim=True,
                    )
                session.commit()
                if len(last_runs) < 20:
                    last_runs.append(
                        {
                            "station_id": station_id,
                            "station_name": payload.get("station_name"),
                            "run_id": run.id,
                            "run_status": run.status,
                            "blocked_reason": run.blocked_reason,
                        }
                    )
            except Exception:
                session.rollback()
                errors += 1
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome="scan_error",
                    run_status="worker_exception",
                    release_claim=True,
                    error="worker_exception",
                )
                session.commit()

    return {
        "mode": "dispatch",
        "workers": max_workers,
        "stations_considered": len(stations),
        "stations_processed": processed,
        "runs_created": run_created,
        "awaiting_review": awaiting_review,
        "blocked": blocked,
        "errors": errors,
        "skipped_prefilter": skipped_prefilter,
        "llm_processed": llm_processed,
        "llm_saved": llm_saved,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_errors": llm_errors,
        "llm_promoted": llm_promoted,
        "llm_review": llm_review,
        "llm_rejected": llm_rejected,
        "llm_transferred": llm_transferred,
        "llm_transfer_errors": llm_transfer_errors,
        "checkpoint_before": checkpoint_before,
        "checkpoint_after": int(max_seen_id),
        "wrapped": wrapped,
        "last_runs": last_runs,
    }


def run_rejected_scan_discovery(
    session: Session,
    station_limit: int = 45,
    max_pages: int = 3,
    workers: int = 3,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
) -> dict:
    if reset_checkpoint:
        _save_rejected_scan_checkpoint(last_station_id=0)
    checkpoint = _load_rejected_scan_checkpoint() if use_checkpoint else {"last_station_id": 0}
    checkpoint_before = int(checkpoint.get("last_station_id", 0))
    claimed_ids, max_seen_id, wrapped = _claim_rejected_scan_batch(
        session=session,
        station_limit=station_limit,
        use_checkpoint=use_checkpoint,
    )
    stations = (
        session.scalars(select(Station).where(Station.id.in_(claimed_ids)).order_by(Station.id.asc())).all()
        if claimed_ids
        else []
    )

    processed = 0
    run_created = 0
    blocked = 0
    awaiting_quality_review = 0
    errors = 0
    skipped_prefilter = 0
    last_runs: list[dict] = []
    worker_payloads: list[dict] = []
    candidate_by_station: dict[int, list[tuple[str, list[str]]]] = {}

    for st in stations:
        if processed >= station_limit:
            break
        station_id = int(st.id)
        if (
            not is_supported_station_target_url(st.website_url)
            or station_matches_excluded_focus(st)
            or station_matches_excluded_meta(st)
            or station_matches_entrypoint_only(st)
        ):
            skipped_prefilter += 1
            _finalize_rejected_scan_station(
                session=session,
                station_id=station_id,
                outcome="prefilter_rejected",
                run_status="prefilter_skipped",
                release_claim=True,
            )
            session.commit()
            continue
        candidate_urls = _candidate_urls_for_station(
            st,
            max_urls=max(1, max_pages * 3, settings.browser_deep_max_urls_per_station),
            include_deep_pass=False,
        )
        candidate_urls = _rank_candidate_scan_urls(candidate_urls)
        selected_urls = candidate_urls[: max(1, max_pages)]
        candidate_by_station[station_id] = selected_urls
        worker_payloads.append(
            {
                "station_id": station_id,
                "station_name": st.canonical_name,
                "website_url": st.website_url,
                "candidate_urls": selected_urls,
                "max_pages": max(1, max_pages),
            }
        )
        processed += 1

    max_workers = max(1, int(workers or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_extract_rejected_scan_payload, payload): payload for payload in worker_payloads}
        for future in as_completed(future_map):
            payload = future_map[future]
            station_id = int(payload["station_id"])
            try:
                extraction = future.result()
                run = _persist_extracted_scan_run(
                    session=session,
                    station_id=station_id,
                    candidate_urls=candidate_by_station.get(station_id, []),
                    pages=extraction.get("pages") or [],
                    max_pages=max(1, max_pages),
                )
                run_created += 1
                if run.status == "blocked":
                    blocked += 1
                    outcome = "reviewed_rejected"
                elif run.status == "awaiting_review":
                    awaiting_quality_review += 1
                    outcome = "awaiting_quality_review"
                else:
                    outcome = "needs_review"
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome=outcome,
                    run_status=run.status,
                    scanned_at=run.finished_at,
                    release_claim=True,
                )
                session.commit()
                if len(last_runs) < 20:
                    last_runs.append(
                        {
                            "station_id": station_id,
                            "station_name": payload.get("station_name"),
                            "run_id": run.id,
                            "run_status": run.status,
                            "scan_outcome": outcome,
                            "blocked_reason": run.blocked_reason,
                        }
                    )
            except Exception:
                session.rollback()
                errors += 1
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome="scan_error",
                    run_status="worker_exception",
                    release_claim=True,
                    error="worker_exception",
                )
                session.commit()

    return {
        "mode": "discovery",
        "workers": max_workers,
        "stations_considered": len(stations),
        "stations_processed": processed,
        "runs_created": run_created,
        "awaiting_quality_review": awaiting_quality_review,
        "blocked": blocked,
        "errors": errors,
        "skipped_prefilter": skipped_prefilter,
        "checkpoint_before": checkpoint_before,
        "checkpoint_after": int(max_seen_id),
        "wrapped": wrapped,
        "last_runs": last_runs,
    }


def run_rejected_scan_continuous(
    session_factory: sessionmaker[Session],
    station_limit: int = 45,
    max_pages: int = 3,
    workers: int = 4,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
) -> dict:
    if reset_checkpoint:
        with REJECTED_SCAN_WRITE_LOCK:
            _save_rejected_scan_checkpoint(last_station_id=0)

    max_workers = max(1, int(workers or 1))
    target_limit = max(1, int(station_limit or 1))
    max_pages = max(1, int(max_pages or 1))
    state = {"claimed": 0}

    def _worker(worker_index: int) -> dict:
        stats = {
            "worker": worker_index,
            "claimed": 0,
            "runs_created": 0,
            "awaiting_quality_review": 0,
            "blocked": 0,
            "errors": 0,
            "skipped_prefilter": 0,
            "last_runs": [],
        }
        while True:
            payload: dict | None = None
            candidate_urls: list[tuple[str, list[str]]] = []
            station_id: int | None = None
            with REJECTED_SCAN_WRITE_LOCK:
                if state["claimed"] >= target_limit:
                    break
                with session_factory() as session:
                    claimed_ids, _max_seen_id, _wrapped = _claim_rejected_scan_batch(
                        session=session,
                        station_limit=1,
                        use_checkpoint=use_checkpoint,
                    )
                    if not claimed_ids:
                        break
                    state["claimed"] += 1
                    stats["claimed"] += 1
                    station_id = int(claimed_ids[0])
                    st = session.get(Station, station_id)
                    if st is None:
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=station_id,
                            outcome="scan_error",
                            run_status="missing_station",
                            release_claim=True,
                            error="missing_station",
                        )
                        session.commit()
                        stats["errors"] += 1
                        continue
                    if (
                        not is_supported_station_target_url(st.website_url)
                        or station_matches_excluded_focus(st)
                        or station_matches_excluded_meta(st)
                        or station_matches_entrypoint_only(st)
                    ):
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=station_id,
                            outcome="prefilter_rejected",
                            run_status="prefilter_skipped",
                            release_claim=True,
                        )
                        session.commit()
                        stats["skipped_prefilter"] += 1
                        continue
                    candidate_urls = _candidate_urls_for_station(
                        st,
                        max_urls=max(1, max_pages * 3, settings.browser_deep_max_urls_per_station),
                        include_deep_pass=False,
                    )
                    candidate_urls = _rank_candidate_scan_urls(candidate_urls)[:max_pages]
                    payload = {
                        "station_id": station_id,
                        "station_name": st.canonical_name,
                        "website_url": st.website_url,
                        "candidate_urls": candidate_urls,
                        "max_pages": max_pages,
                    }

            if not payload or station_id is None:
                continue

            try:
                extraction = _extract_rejected_scan_payload(payload)
                with REJECTED_SCAN_WRITE_LOCK:
                    with session_factory() as session:
                        run = _persist_extracted_scan_run(
                            session=session,
                            station_id=station_id,
                            candidate_urls=candidate_urls,
                            pages=extraction.get("pages") or [],
                            max_pages=max_pages,
                        )
                        stats["runs_created"] += 1
                        if run.status == "blocked":
                            stats["blocked"] += 1
                            outcome = "reviewed_rejected"
                        elif run.status == "awaiting_review":
                            stats["awaiting_quality_review"] += 1
                            outcome = "awaiting_quality_review"
                        else:
                            outcome = "needs_review"
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=station_id,
                            outcome=outcome,
                            run_status=run.status,
                            scanned_at=run.finished_at,
                            release_claim=True,
                        )
                        session.commit()
                        if len(stats["last_runs"]) < 10:
                            stats["last_runs"].append(
                                {
                                    "station_id": station_id,
                                    "station_name": payload.get("station_name"),
                                    "run_id": run.id,
                                    "run_status": run.status,
                                    "scan_outcome": outcome,
                                    "blocked_reason": run.blocked_reason,
                                }
                            )
            except Exception as exc:
                with REJECTED_SCAN_WRITE_LOCK:
                    with session_factory() as session:
                        stats["errors"] += 1
                        _finalize_rejected_scan_station(
                            session=session,
                            station_id=station_id,
                            outcome="scan_error",
                            run_status="worker_exception",
                            release_claim=True,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        session.commit()
        return stats

    worker_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, index) for index in range(1, max_workers + 1)]
        for future in as_completed(futures):
            worker_results.append(future.result())

    totals = {
        "mode": "continuous_discovery",
        "workers": max_workers,
        "stations_claimed": sum(int(item.get("claimed", 0) or 0) for item in worker_results),
        "runs_created": sum(int(item.get("runs_created", 0) or 0) for item in worker_results),
        "awaiting_quality_review": sum(int(item.get("awaiting_quality_review", 0) or 0) for item in worker_results),
        "blocked": sum(int(item.get("blocked", 0) or 0) for item in worker_results),
        "errors": sum(int(item.get("errors", 0) or 0) for item in worker_results),
        "skipped_prefilter": sum(int(item.get("skipped_prefilter", 0) or 0) for item in worker_results),
        "worker_results": worker_results,
    }
    totals["last_runs"] = [
        run
        for item in worker_results
        for run in (item.get("last_runs") or [])
    ][:20]
    return totals


def run_rejected_quality_review(
    session: Session,
    limit: int = 30,
    provider: str | None = None,
) -> dict:
    review_limit = max(1, int(limit))
    claim_token = uuid.uuid4().hex
    claimed_ids = _claim_rejected_quality_review_batch(session=session, limit=review_limit, claim_token=claim_token)
    if not claimed_ids:
        return {
            "mode": "quality_review",
            "stations_considered": 0,
            "claimed": 0,
            "processed": 0,
            "saved": 0,
            "skipped_budget": 0,
            "errors": 0,
            "promoted": 0,
            "review": 0,
            "rejected": 0,
            "transferred": 0,
            "transfer_errors": 0,
            "last_reviews": [],
            "exception_samples": [],
        }
    stations = session.scalars(
        select(Station).where(Station.id.in_(claimed_ids)).order_by(Station.scan_last_scanned_at.asc().nullsfirst(), Station.id.asc())
    ).all()

    processed = 0
    saved = 0
    skipped_budget = 0
    errors = 0
    promoted = 0
    review = 0
    rejected = 0
    transferred = 0
    transfer_errors = 0
    last_reviews: list[dict] = []
    exception_samples: list[dict] = []
    logged_full_traceback = False

    for st in stations:
        station_id = int(st.id)
        latest_run = session.scalar(
            select(SubmissionAgentRun)
            .where(SubmissionAgentRun.station_id == station_id)
            .order_by(SubmissionAgentRun.finished_at.desc().nulls_last(), SubmissionAgentRun.id.desc())
        )
        try:
            # If Gemini/OpenAI already wrote an assessment but promotion failed mid-flight (e.g. transfer exception),
            # `only_unassessed=True` skips re-verification forever and the station stays stuck in awaiting_quality_review.
            # Trust an existing verdict on this queue row and finalize transfer/outcome without another LLM call.
            skip_llm = (
                st.status == StationStatus.REJECTED
                and (st.scan_outcome or "") == "awaiting_quality_review"
                and (_latest_quality_decision(session=session, station_id=station_id) or "") in {"promote", "reject", "review"}
            )
            llm_result: dict = {}
            if skip_llm:
                llm_result = {
                    "processed": 0,
                    "saved": 0,
                    "skipped_budget": 0,
                    "errors": 0,
                    "reason": "reuse_existing_quality_verdict",
                }
            else:
                llm_result = run_station_quality_verification(
                    session=session,
                    limit=1,
                    station_id=station_id,
                    provider=provider or settings.station_quality_provider,
                    only_unassessed=True,
                    apply=True,
                )
            processed += int(llm_result.get("processed", 0) or 0)
            saved += int(llm_result.get("saved", 0) or 0)
            skipped_budget += int(llm_result.get("skipped_budget", 0) or 0)
            errors += int(llm_result.get("errors", 0) or 0)
            decision = _latest_quality_decision(session=session, station_id=station_id)
            if decision == "promote":
                transfer_result = _transfer_promoted_station_to_main(session=session, station=st)
                if transfer_result.get("transferred"):
                    st.status = StationStatus.VERIFIED
                    st.priority_tier = max(1, int(st.priority_tier or 0))
                    st.confidence_score = max(float(st.confidence_score or 0.0), 0.75)
                    session.add(st)
                    session.commit()
                    promoted += 1
                    transferred += 1
                    _finalize_rejected_scan_station(
                        session=session,
                        station_id=station_id,
                        outcome="reviewed_verified",
                        run_status=latest_run.status if latest_run else "awaiting_review",
                        scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                        release_claim=True,
                    )
                else:
                    transfer_errors += 1
                    _finalize_rejected_scan_station(
                        session=session,
                        station_id=station_id,
                        outcome="needs_review",
                        run_status=latest_run.status if latest_run else "awaiting_review",
                        scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                        release_claim=True,
                        error="transfer_failed",
                    )
            elif decision == "reject":
                st.status = StationStatus.REJECTED
                st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
                session.add(st)
                session.commit()
                rejected += 1
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome="reviewed_rejected",
                    run_status=latest_run.status if latest_run else "awaiting_review",
                    scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                    release_claim=True,
                )
            elif decision == "review":
                review += 1
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome="needs_review",
                    run_status=latest_run.status if latest_run else "awaiting_review",
                    scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                    release_claim=True,
                )
            else:
                _finalize_rejected_scan_station(
                    session=session,
                    station_id=station_id,
                    outcome="awaiting_quality_review",
                    run_status=latest_run.status if latest_run else "awaiting_review",
                    scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                    release_claim=True,
                    error="quality_no_decision",
                )
            session.commit()
            if len(last_reviews) < 20:
                last_reviews.append(
                    {
                        "station_id": station_id,
                        "station_name": st.canonical_name,
                        "decision": decision,
                    }
                )
        except Exception as exc:
            session.rollback()
            errors += 1
            err_type = type(exc).__name__
            err_msg = str(exc).strip() or repr(exc)
            detail = f"{err_type}: {err_msg}"[:480]
            print(
                f"[rejected_quality_review] station_id={station_id} name={st.canonical_name!r} {detail}",
                file=sys.stderr,
                flush=True,
            )
            if not logged_full_traceback:
                traceback.print_exc(file=sys.stderr)
                logged_full_traceback = True
            if len(exception_samples) < 8:
                exception_samples.append(
                    {
                        "station_id": station_id,
                        "error_type": err_type,
                        "message": err_msg[:300],
                    }
                )
            _finalize_rejected_scan_station(
                session=session,
                station_id=station_id,
                outcome="awaiting_quality_review",
                run_status=latest_run.status if latest_run else "awaiting_review",
                scanned_at=latest_run.finished_at if latest_run else datetime.utcnow(),
                release_claim=True,
                error=f"quality_exception:{detail}",
            )
            session.commit()

    return {
        "mode": "quality_review",
        "stations_considered": len(stations),
        "claimed": len(claimed_ids),
        "processed": processed,
        "saved": saved,
        "skipped_budget": skipped_budget,
        "errors": errors,
        "promoted": promoted,
        "review": review,
        "rejected": rejected,
        "transferred": transferred,
        "transfer_errors": transfer_errors,
        "last_reviews": last_reviews,
        "exception_samples": exception_samples,
    }


def run_main_scan_cycle(
    session: Session,
    station_limit: int = 100,
    max_pages: int = 3,
    min_confidence: float = 0.0,
    use_checkpoint: bool = True,
    reset_checkpoint: bool = False,
) -> dict:
    if reset_checkpoint:
        _save_main_scan_checkpoint(last_station_id=0)
    checkpoint = _load_main_scan_checkpoint() if use_checkpoint else {"last_station_id": 0}
    last_station_id = int(checkpoint.get("last_station_id", 0))

    def _fetch_batch(from_id: int, limit: int):
        stmt = (
            select(Station)
            .where(
                Station.id > from_id,
                Station.status.in_([StationStatus.CANDIDATE, StationStatus.VERIFIED]),
                Station.website_url.is_not(None),
                Station.confidence_score >= float(min_confidence),
            )
            .order_by(Station.id.asc())
            .limit(max(1, limit))
        )
        return session.scalars(stmt).all()

    stations = _fetch_batch(last_station_id, max(1, station_limit * 3))
    wrapped = False
    if not stations and use_checkpoint and last_station_id > 0:
        wrapped = True
        last_station_id = 0
        stations = _fetch_batch(0, max(1, station_limit * 3))

    processed = 0
    run_created = 0
    blocked = 0
    blocked_high_value_followup = 0
    awaiting_review = 0
    errors = 0
    llm_processed = 0
    llm_saved = 0
    llm_skipped_budget = 0
    llm_errors = 0
    llm_promoted = 0
    llm_review = 0
    llm_rejected = 0
    candidate_promoted_to_verified = 0
    candidate_rejected_to_rejected = 0
    forms_saved_total = 0
    emails_saved_total = 0
    max_seen_id = last_station_id
    last_runs: list[dict] = []

    for st in stations:
        if processed >= station_limit:
            break
        max_seen_id = max(max_seen_id, int(st.id))
        if (
            not is_supported_station_target_url(st.website_url)
            or station_matches_excluded_focus(st)
            or station_matches_excluded_meta(st)
            or station_matches_entrypoint_only(st)
        ):
            continue
        processed += 1
        try:
            run = start_manual_scan_run(
                session=session,
                station_id=int(st.id),
                max_pages=max(1, max_pages),
                force_rescan=False,
            )
            run_created += 1

            run_summary = _json_loads(run.summary_json, {}) if run.summary_json else {}
            if isinstance(run_summary, dict):
                forms_saved_total += int(run_summary.get("forms_saved", 0) or 0)
                emails_saved_total += int(run_summary.get("emails_saved", 0) or 0)

            if run.status == "blocked":
                if _is_high_value_followup_station(st):
                    run.blocked_reason = MAIN_SCAN_HIGH_VALUE_BLOCKED_REASON
                    run.summary_json = _json_dumps(
                        {
                            **(run_summary if isinstance(run_summary, dict) else {}),
                            "high_value_followup": True,
                        }
                    )
                    session.add(run)
                    session.commit()
                    blocked_high_value_followup += 1
                blocked += 1

            if run.status == "awaiting_review":
                awaiting_review += 1
                llm_result = run_station_quality_verification(
                    session=session,
                    limit=1,
                    station_id=int(st.id),
                    provider=settings.station_quality_provider,
                    only_unassessed=False,
                    apply=True,
                )
                llm_processed += int(llm_result.get("processed", 0) or 0)
                llm_saved += int(llm_result.get("saved", 0) or 0)
                llm_skipped_budget += int(llm_result.get("skipped_budget", 0) or 0)
                llm_errors += int(llm_result.get("errors", 0) or 0)
                decision = _latest_quality_decision(session=session, station_id=int(st.id))
                if decision == "promote":
                    llm_promoted += 1
                    # Legacy main scan now writes evidence/assessment only. Status
                    # changes are applied by candidate_rescan after dedupe/path guards.
                elif decision == "reject":
                    llm_rejected += 1
                    # Keep status stable here; candidate_rescan owns final apply.
                elif decision == "review":
                    llm_review += 1

            if len(last_runs) < 25:
                last_runs.append(
                    {
                        "station_id": st.id,
                        "station_name": st.canonical_name,
                        "station_status": st.status.value if isinstance(st.status, StationStatus) else str(st.status),
                        "run_id": run.id,
                        "run_status": run.status,
                        "blocked_reason": run.blocked_reason,
                        "forms_saved": int(run_summary.get("forms_saved", 0) or 0) if isinstance(run_summary, dict) else 0,
                        "emails_saved": int(run_summary.get("emails_saved", 0) or 0) if isinstance(run_summary, dict) else 0,
                        "llm_decision": _latest_quality_decision(session=session, station_id=int(st.id)),
                    }
                )
        except Exception:
            session.rollback()
            errors += 1

    if use_checkpoint:
        _save_main_scan_checkpoint(last_station_id=max_seen_id)

    return {
        "stations_considered": len(stations),
        "stations_processed": processed,
        "runs_created": run_created,
        "awaiting_review": awaiting_review,
        "blocked": blocked,
        "blocked_high_value_followup": blocked_high_value_followup,
        "errors": errors,
        "llm_processed": llm_processed,
        "llm_saved": llm_saved,
        "llm_skipped_budget": llm_skipped_budget,
        "llm_errors": llm_errors,
        "llm_promoted": llm_promoted,
        "llm_review": llm_review,
        "llm_rejected": llm_rejected,
        "candidate_promoted_to_verified": candidate_promoted_to_verified,
        "candidate_rejected_to_rejected": candidate_rejected_to_rejected,
        "forms_saved_total": forms_saved_total,
        "emails_saved_total": emails_saved_total,
        "checkpoint_before": int(checkpoint.get("last_station_id", 0)),
        "checkpoint_after": int(max_seen_id),
        "wrapped": wrapped,
        "last_runs": last_runs,
    }


def agent_run_snapshot(session: Session, run_id: int) -> dict | None:
    run = session.scalar(select(SubmissionAgentRun).where(SubmissionAgentRun.id == run_id))
    if run is None:
        return None

    station = run.station
    form = run.form
    steps = list(run.steps)
    issues = list(run.issues)
    assessment = session.scalar(
        select(StationSubmissionAssessment)
        .where(
            StationSubmissionAssessment.station_id == run.station_id,
            StationSubmissionAssessment.assessment_kind == "manual_scan",
        )
        .order_by(StationSubmissionAssessment.updated_at.desc())
    )
    return {
        "run": run,
        "station": station,
        "form": form,
        "steps": steps,
        "issues": issues,
        "assessment": assessment,
    }

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session, sessionmaker

from radio_db.config import settings
from radio_db.models.entities import (
    FormRecipe,
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
    SubmissionAgentIssue,
    SubmissionAgentRun,
    SubmissionAgentStep,
    SubmissionForm,
    SubmissionFormField,
)
from radio_db.services.forms import (
    _candidate_urls_for_station,
    _extract_forms_fallback,
    _extract_forms_playwright,
    _is_valid_discovered_email,
    _persist_email_channels,
    _persist_form,
    is_supported_station_target_url,
    station_matches_entrypoint_only,
    station_matches_excluded_meta,
    station_matches_excluded_focus,
)
from radio_db.services.station_quality import run_station_quality_verification


NEW_ARTIST_HINTS = ("new artist", "unsigned", "emerging", "newcomer", "demo")
BLOCKED_SUBMISSION_HINTS = (
    "not for music submissions",
    "no music submissions",
    "not for submissions",
    "do not send",
)
MAIN_SCAN_HIGH_VALUE_BLOCKED_REASON = "blocked_high_value_followup"


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
        executed_action_json=_json_dumps({"type": "none", "mode": "manual_phase_1"}),
        execution_result_json=_json_dumps(execution_result),
        confidence=confidence,
        latency_ms=latency_ms,
    )
    session.add(step)
    session.flush()
    return step


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
    primary_form_id: int | None = None

    scan_errors = 0
    for url, entry_path in candidate_urls[: max(1, max_pages)]:
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

        pages_scanned += 1
        visible_forms = meta.get("forms") or []
        discovered_form_ids: list[int] = []
        scan_state_after = "form_understanding" if visible_forms else "blocked"
        observation = {
            "url": url,
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
                payload={"url": url},
            )
        if meta.get("requires_login"):
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="login_required",
                severity="high",
                title="Login erforderlich",
                details=f"Die Seite {url} scheint einen Login zu verlangen.",
                payload={"url": url},
            )
        if not visible_forms:
            _record_issue(
                session=session,
                run=run,
                step=None,
                issue_type="no_form_detected",
                severity="medium",
                title="Kein Formular erkannt",
                details=f"Auf {url} wurde kein Formular gefunden.",
                payload={"url": url},
            )
        page_emails = [
            str(x).strip().lower()
            for x in (meta.get("emails") or [])
            if str(x).strip() and _is_valid_discovered_email(str(x).strip())
        ]
        if page_emails:
            emails_saved += _persist_email_channels(session=session, station=station, source_url=url, emails=page_emails)

        for form_payload in visible_forms:
            form_row = session.scalar(
                select(SubmissionForm).where(
                    SubmissionForm.station_id == station.id,
                    SubmissionForm.url == url,
                )
            )
            if form_row is None or force_rescan:
                form_row = _persist_form(
                    session=session,
                    station=station,
                    page_url=url,
                    entry_path=entry_path,
                    form_payload=form_payload,
                    mode="manual_scan" if force_rescan else "read",
                    scan_meta=meta,
                )
            forms_saved += 1
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

        execution_result["discovered_form_ids"] = discovered_form_ids
        execution_result["saved_form_count"] = len(discovered_form_ids)
        execution_result["page_domain_matches_station"] = _domain(url) == _domain(station.website_url)
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
    last_station_id = int(checkpoint.get("last_station_id", 0))

    def _fetch_batch(from_id: int, limit: int):
        return session.scalars(
            select(Station)
            .where(
                Station.id > from_id,
                Station.status == StationStatus.REJECTED,
                Station.website_url.is_not(None),
            )
            .order_by(Station.id.asc())
            .limit(max(1, limit))
        ).all()

    stations = _fetch_batch(last_station_id, max(1, station_limit * 6))
    wrapped = False
    if not stations and use_checkpoint and last_station_id > 0:
        wrapped = True
        last_station_id = 0
        stations = _fetch_batch(0, max(1, station_limit * 6))

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
            skipped_prefilter += 1
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
                        else:
                            llm_transfer_errors += 1
                    elif decision == "reject":
                        st.status = StationStatus.REJECTED
                        st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
                        session.add(st)
                        session.commit()
                        llm_rejected += 1
                    elif decision == "review":
                        llm_review += 1
                except Exception:
                    session.rollback()
                    llm_errors += 1
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

    if use_checkpoint:
        _save_rejected_scan_checkpoint(last_station_id=max_seen_id)

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
        "checkpoint_before": int(checkpoint.get("last_station_id", 0)),
        "checkpoint_after": int(max_seen_id),
        "wrapped": wrapped,
        "last_runs": last_runs,
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
                    if st.status == StationStatus.CANDIDATE:
                        st.status = StationStatus.VERIFIED
                        st.priority_tier = max(1, int(st.priority_tier or 0))
                        st.confidence_score = max(float(st.confidence_score or 0.0), 0.75)
                        session.add(st)
                        session.commit()
                        candidate_promoted_to_verified += 1
                elif decision == "reject":
                    llm_rejected += 1
                    if st.status == StationStatus.CANDIDATE:
                        st.status = StationStatus.REJECTED
                        st.confidence_score = min(float(st.confidence_score or 0.0), 0.2)
                        session.add(st)
                        session.commit()
                        candidate_rejected_to_rejected += 1
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

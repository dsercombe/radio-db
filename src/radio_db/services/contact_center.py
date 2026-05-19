from __future__ import annotations

import json
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.models.entities import (
    ContactDraft,
    ContactOutcome,
    ContactSend,
    ContactTemplate,
    ContactTemplateLocale,
    FormRecipe,
    FormStatus,
    Station,
    SubmissionForm,
    SubmissionFormField,
)
from radio_db.services.forms_agent import start_execute_submission_run

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


@dataclass
class ContactDraftPayload:
    station_id: int
    station_name: str
    locale: str
    language: str
    country_code: str
    city: str | None
    subject: str
    body: str
    locale_hint: str
    recommended_channel: str
    available_routes: list[dict[str, str]]
    evidence_summary: list[str]
    template_id: int | None = None
    campaign_id: int | None = None
    draft_id: int | None = None
    status: str = "draft"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ContactSendPayload:
    id: int
    draft_id: int
    station_id: int
    channel: str
    target_value: str | None
    mode: str
    status: str
    payload: dict[str, object]
    created_at: str
    updated_at: str
    outcome_count: int


@dataclass
class ContactOutcomePayload:
    id: int
    send_id: int
    outcome_type: str
    status: str
    details: str | None
    payload: dict[str, object]
    created_at: str


def _parse_json(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _first_language(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "en"
    for separator in (",", "/", ";"):
        raw = raw.replace(separator, " ")
    token = raw.split()[0].strip()
    return token or "en"


def _greeting(language: str) -> str:
    language_key = language[:2]
    return {
        "de": "Hallo",
        "fr": "Bonjour",
        "es": "Hola",
        "it": "Ciao",
        "nl": "Hallo",
        "pt": "Olá",
        "sv": "Hej",
    }.get(language_key, "Hello")


def _recommended_channel(station: Station) -> str:
    if station.submissions:
        first_submission = station.submissions[0]
        if first_submission.email:
            return "email"
        if first_submission.url:
            return "form"
    for contact in station.contacts:
        if contact.email:
            return "contact_email"
        if contact.contact_url:
            return "contact_url"
    for person in station.people:
        if person.email:
            return "person_email"
        if person.contact_url:
            return "person_contact_url"
    if station.forms:
        return "form"
    return "manual_research"


def _available_routes(station: Station) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for submission in station.submissions:
        if submission.email:
            routes.append({"kind": "submission_email", "label": submission.email, "value": submission.email})
        if submission.url:
            routes.append({"kind": "submission_form", "label": submission.url, "value": submission.url})
    for contact in station.contacts:
        if contact.email:
            routes.append({"kind": "contact_email", "label": contact.name or contact.role.value, "value": contact.email})
        elif contact.contact_url:
            routes.append({"kind": "contact_url", "label": contact.name or contact.role.value, "value": contact.contact_url})
    for person in station.people:
        if person.email:
            routes.append({"kind": "person_email", "label": person.name or person.role.value, "value": person.email})
        elif person.contact_url:
            routes.append({"kind": "person_contact_url", "label": person.name or person.role.value, "value": person.contact_url})
    for form in station.forms:
        routes.append({"kind": "form", "label": form.page_title or form.url, "value": form.url})
    return routes[:12]


def _template_variables(station: Station) -> dict[str, str]:
    genres = [genre.genre for genre in station.genres[:3]]
    city_label = station.city or station.country_code or "your market"
    return {
        "station_name": station.canonical_name,
        "city": city_label,
        "country_code": station.country_code or "",
        "language": station.language or "",
        "genre_list": ", ".join(genres) if genres else "your editorial lane",
        "greeting": _greeting(_first_language(station.language)),
    }


def _render_template(text: str, values: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", _replace, text)


def _default_template_payload(station: Station) -> ContactDraftPayload:
    language = _first_language(station.language)
    locale = f"{station.country_code or '--'} / {station.language or '--'}"
    city_label = station.city or station.country_code or "your market"
    genres = [genre.genre for genre in station.genres[:3]]
    genre_label = ", ".join(genres) if genres else "your editorial lane"
    recommended_channel = _recommended_channel(station)
    routes = _available_routes(station)
    evidence_summary = [
        f"{len(station.submissions)} submission routes",
        f"{len(station.contacts)} direct contacts",
        f"{len(station.people)} people records",
        f"{len(station.forms)} tracked forms",
    ]

    return ContactDraftPayload(
        station_id=station.id,
        station_name=station.canonical_name,
        locale=locale,
        language=language,
        country_code=station.country_code or "",
        city=station.city,
        subject=f"Music submission for {station.canonical_name}",
        body="\n".join(
            [
                f"{_greeting(language)} {station.canonical_name} team,",
                "",
                f"I am reaching out with a release that feels relevant for {city_label}.",
                f"The current station profile suggests a fit for {genre_label}.",
                "",
                "If there is a preferred submission route, I am happy to follow it.",
                "",
                "Best regards,",
                "Radio DB automation",
            ]
        ),
        locale_hint=f"Draft context: {locale} / confidence {round(float(station.confidence_score or 0.0) * 100)}%",
        recommended_channel=recommended_channel,
        available_routes=routes,
        evidence_summary=evidence_summary,
    )


def ensure_default_contact_template(session: Session) -> ContactTemplate:
    existing = session.scalar(select(ContactTemplate).where(ContactTemplate.template_key == "default_pitch_v1"))
    if existing is not None:
        return existing

    template = ContactTemplate(
        template_key="default_pitch_v1",
        name="Default Pitch",
        description="Baseline station outreach draft for music submissions.",
        channel="email",
        variables_json=json.dumps(
            ["station_name", "city", "country_code", "language", "genre_list", "greeting"],
            ensure_ascii=False,
        ),
        is_active=True,
    )
    session.add(template)
    session.flush()
    session.add(
        ContactTemplateLocale(
            template_id=template.id,
            locale_key="default",
            language_code="en",
            subject_template="Music submission for {{station_name}}",
            body_template="\n".join(
                [
                    "{{greeting}} {{station_name}} team,",
                    "",
                    "I am reaching out with a release that feels relevant for {{city}}.",
                    "The current station profile suggests a fit for {{genre_list}}.",
                    "",
                    "If there is a preferred submission route, I am happy to follow it.",
                    "",
                    "Best regards,",
                    "Radio DB automation",
                ]
            ),
        )
    )
    session.commit()
    session.refresh(template)
    return template


def list_contact_templates(session: Session) -> list[ContactTemplate]:
    ensure_default_contact_template(session)
    return session.scalars(
        select(ContactTemplate).order_by(ContactTemplate.is_active.desc(), ContactTemplate.name.asc(), ContactTemplate.id.asc())
    ).all()


def create_contact_template(
    session: Session,
    template_key: str,
    name: str,
    description: str | None,
    channel: str,
    locale_key: str,
    language_code: str,
    subject_template: str,
    body_template: str,
    variables: list[str] | None = None,
) -> ContactTemplate:
    template = ContactTemplate(
        template_key=template_key,
        name=name,
        description=description,
        channel=channel,
        variables_json=json.dumps(variables or [], ensure_ascii=False),
        is_active=True,
    )
    session.add(template)
    session.flush()
    session.add(
        ContactTemplateLocale(
            template_id=template.id,
            locale_key=locale_key,
            language_code=language_code,
            subject_template=subject_template,
            body_template=body_template,
        )
    )
    session.commit()
    session.refresh(template)
    return template


def _resolve_template_locale(template: ContactTemplate, station: Station) -> ContactTemplateLocale | None:
    station_language = _first_language(station.language)
    preferred_locales = [
        f"{station.country_code.lower()}-{station_language}" if station.country_code else "",
        station_language,
        "default",
    ]
    by_locale = {locale.locale_key.lower(): locale for locale in template.locales}
    for locale_key in preferred_locales:
        if locale_key and locale_key.lower() in by_locale:
            return by_locale[locale_key.lower()]
    if template.locales:
        return template.locales[0]
    return None


def _payload_from_draft_row(draft: ContactDraft) -> ContactDraftPayload:
    station = draft.station
    return ContactDraftPayload(
        station_id=draft.station_id,
        station_name=station.canonical_name,
        locale=draft.locale,
        language=draft.language,
        country_code=draft.country_code,
        city=station.city,
        subject=draft.subject,
        body=draft.body,
        locale_hint=f"Draft context: {draft.locale} / confidence {round(float(station.confidence_score or 0.0) * 100)}%",
        recommended_channel=draft.recommended_channel,
        available_routes=list(_parse_json(draft.available_routes_json, [])),
        evidence_summary=list(_parse_json(draft.evidence_summary_json, [])),
        template_id=draft.template_id,
        campaign_id=draft.campaign_id,
        draft_id=draft.id,
        status=draft.status,
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )


def build_contact_draft(session: Session, station_id: int) -> ContactDraftPayload | None:
    station = session.get(Station, station_id)
    if station is None:
        return None
    return _default_template_payload(station)


def create_persisted_contact_draft(
    session: Session,
    station_id: int,
    template_id: int | None = None,
    subject: str | None = None,
    body: str | None = None,
    campaign_id: int | None = None,
) -> ContactDraftPayload | None:
    station = session.get(Station, station_id)
    if station is None:
        return None

    base_payload = _default_template_payload(station)
    resolved_template: ContactTemplate | None = None
    if template_id is not None:
        resolved_template = session.get(ContactTemplate, template_id)
    else:
        resolved_template = ensure_default_contact_template(session)

    if resolved_template is not None:
        locale = _resolve_template_locale(resolved_template, station)
        if locale is not None:
            rendered_values = _template_variables(station)
            base_payload.subject = _render_template(locale.subject_template, rendered_values)
            base_payload.body = _render_template(locale.body_template, rendered_values)
            base_payload.template_id = resolved_template.id

    if subject is not None:
        base_payload.subject = subject
    if body is not None:
        base_payload.body = body

    draft = ContactDraft(
        station_id=station.id,
        template_id=base_payload.template_id,
        campaign_id=campaign_id,
        locale=base_payload.locale,
        language=base_payload.language,
        country_code=base_payload.country_code,
        subject=base_payload.subject,
        body=base_payload.body,
        recommended_channel=base_payload.recommended_channel,
        status="draft",
        available_routes_json=json.dumps(base_payload.available_routes, ensure_ascii=False),
        evidence_summary_json=json.dumps(base_payload.evidence_summary, ensure_ascii=False),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return _payload_from_draft_row(draft)


def list_station_drafts(session: Session, station_id: int, limit: int = 20) -> list[ContactDraftPayload]:
    drafts = session.scalars(
        select(ContactDraft)
        .where(ContactDraft.station_id == station_id)
        .order_by(ContactDraft.updated_at.desc(), ContactDraft.id.desc())
        .limit(limit)
    ).all()
    return [_payload_from_draft_row(draft) for draft in drafts]


def get_persisted_contact_draft(session: Session, draft_id: int) -> ContactDraftPayload | None:
    draft = session.get(ContactDraft, draft_id)
    if draft is None:
        return None
    return _payload_from_draft_row(draft)


def preview_persisted_contact_draft(
    session: Session,
    draft_id: int,
    subject: str | None = None,
    body: str | None = None,
) -> ContactDraftPayload | None:
    draft = session.get(ContactDraft, draft_id)
    if draft is None:
        return None
    if subject is not None:
        draft.subject = subject
    if body is not None:
        draft.body = body
    draft.status = "previewed"
    session.commit()
    session.refresh(draft)
    return _payload_from_draft_row(draft)


def _serialize_send(send: ContactSend) -> ContactSendPayload:
    return ContactSendPayload(
        id=send.id,
        draft_id=send.draft_id,
        station_id=send.station_id,
        channel=send.channel,
        target_value=send.target_value,
        mode=send.mode,
        status=send.status,
        payload=dict(_parse_json(send.payload_json, {})),
        created_at=send.created_at.isoformat(),
        updated_at=send.updated_at.isoformat(),
        outcome_count=len(send.outcomes),
    )


def _serialize_outcome(outcome: ContactOutcome) -> ContactOutcomePayload:
    return ContactOutcomePayload(
        id=outcome.id,
        send_id=outcome.send_id,
        outcome_type=outcome.outcome_type,
        status=outcome.status,
        details=outcome.details,
        payload=dict(_parse_json(outcome.payload_json, {})),
        created_at=outcome.created_at.isoformat(),
    )


def create_dry_run_send(
    session: Session,
    draft_id: int,
    target_value: str | None = None,
    channel: str | None = None,
) -> ContactSendPayload | None:
    draft = session.get(ContactDraft, draft_id)
    if draft is None:
        return None

    routes = list(_parse_json(draft.available_routes_json, []))
    resolved_channel = (channel or draft.recommended_channel or "manual_research").strip()
    resolved_target = (target_value or "").strip() or None
    if resolved_target is None:
        for route in routes:
            if not isinstance(route, dict):
                continue
            route_kind = str(route.get("kind") or "")
            route_value = str(route.get("value") or "").strip() or None
            if route_value is None:
                continue
            if resolved_channel == draft.recommended_channel or resolved_channel in route_kind:
                resolved_target = route_value
                if resolved_channel in {"manual_research", ""}:
                    resolved_channel = route_kind
                break
        if resolved_target is None and routes:
            first_route = routes[0]
            if isinstance(first_route, dict):
                resolved_target = str(first_route.get("value") or "").strip() or None
                resolved_channel = str(first_route.get("kind") or resolved_channel)

    send = ContactSend(
        draft_id=draft.id,
        station_id=draft.station_id,
        channel=resolved_channel,
        target_value=resolved_target,
        mode="dry-run",
        status="simulated",
        payload_json=json.dumps(
            {
                "subject": draft.subject,
                "body": draft.body,
                "recommended_channel": draft.recommended_channel,
                "target_value": resolved_target,
                "available_routes": routes,
            },
            ensure_ascii=False,
        ),
    )
    session.add(send)
    session.flush()

    outcome = ContactOutcome(
        send_id=send.id,
        outcome_type="dry-run-preview",
        status="simulated",
        details="Dry-run only. No external dispatch executed.",
        payload_json=json.dumps(
            {
                "channel": resolved_channel,
                "target_value": resolved_target,
                "draft_status_before": draft.status,
            },
            ensure_ascii=False,
        ),
    )
    session.add(outcome)
    draft.status = "dry-run-ready"
    session.commit()
    session.refresh(send)
    return _serialize_send(send)


def _resolve_send_target(draft: ContactDraft, channel: str | None, target_value: str | None) -> tuple[str, str | None, list[object]]:
    routes = list(_parse_json(draft.available_routes_json, []))
    resolved_channel = (channel or draft.recommended_channel or "manual_research").strip()
    resolved_target = (target_value or "").strip() or None
    if resolved_target is None:
        for route in routes:
            if not isinstance(route, dict):
                continue
            route_kind = str(route.get("kind") or "")
            route_value = str(route.get("value") or "").strip() or None
            if route_value is None:
                continue
            if resolved_channel == draft.recommended_channel or resolved_channel in route_kind:
                resolved_target = route_value
                if resolved_channel in {"manual_research", ""}:
                    resolved_channel = route_kind
                break
        if resolved_target is None and routes:
            first_route = routes[0]
            if isinstance(first_route, dict):
                resolved_target = str(first_route.get("value") or "").strip() or None
                resolved_channel = str(first_route.get("kind") or resolved_channel)
    return resolved_channel, resolved_target, routes


def _smtp_ready() -> tuple[bool, str]:
    if not settings.smtp_host.strip():
        return False, "smtp_host_missing"
    if not settings.smtp_from_email.strip():
        return False, "smtp_from_email_missing"
    return True, ""


def _send_via_smtp(recipient: str, subject: str, body: str) -> tuple[bool, str]:
    message = EmailMessage()
    sender_name = settings.smtp_from_name.strip()
    sender_email = settings.smtp_from_email.strip()
    sender = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    timeout = max(5, int(settings.smtp_timeout_seconds))
    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout) as smtp:
            if settings.smtp_username.strip():
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return True, "smtp_ssl_sent"

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as smtp:
        if settings.smtp_use_starttls:
            smtp.starttls()
        if settings.smtp_username.strip():
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return True, "smtp_sent"


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
    }


def _submission_payload_for_draft(draft: ContactDraft) -> dict[str, str]:
    profile = _submit_profile()
    campaign = draft.campaign
    if campaign is None:
        return profile

    for raw in (
        getattr(campaign, "submission_defaults_json", "{}"),
        getattr(campaign, "artist_profile_json", "{}"),
        getattr(campaign, "release_assets_json", "{}"),
    ):
        data = _parse_json(raw, {})
        if isinstance(data, dict):
            for key, value in data.items():
                if value is not None and str(value).strip():
                    profile[str(key)] = str(value).strip()

    profile.setdefault("artist_name", campaign.artist_name)
    profile.setdefault("release_title", campaign.song_title)
    profile.setdefault("song_title", campaign.song_title)
    profile.setdefault("release_date", campaign.release_date or "")
    profile.setdefault("song_language", campaign.song_language or "")
    profile.setdefault("press_release_url", campaign.press_release_url or "")
    profile.setdefault("streaming_link", campaign.press_release_url or profile.get("website", ""))
    profile.setdefault("pitch_message_short", campaign.pitch_text or "")
    profile.setdefault("pitch_message_long", campaign.pitch_text or "")
    return profile


def _resolve_form_for_execute(session: Session, draft: ContactDraft, target_value: str | None) -> SubmissionForm | None:
    station = draft.station
    if target_value:
        exact = session.scalar(select(SubmissionForm).where(SubmissionForm.station_id == draft.station_id, SubmissionForm.url == target_value))
        if exact is not None:
            return exact
    if station.forms:
        active_forms = sorted(
            station.forms,
            key=lambda item: (0 if item.status == FormStatus.ACTIVE else 1, -(item.id or 0)),
        )
        return active_forms[0]
    return None


def _field_selector(field: SubmissionFormField) -> str | None:
    if field.name:
        return f'[name="{field.name}"]'
    if field.field_key:
        return f'[name="{field.field_key}"], #{field.field_key}'
    return None


def _field_value_candidates(draft: ContactDraft, field: SubmissionFormField, mapping: dict[str, str]) -> list[str]:
    profile = _submission_payload_for_draft(draft)
    hay = f"{field.label or ''} {field.name or ''} {field.field_key or ''}".lower()
    candidates: list[str] = []

    reverse_mapping = {value: key for key, value in mapping.items()}
    logical = reverse_mapping.get(field.field_key)
    if logical:
        if logical == "artist_name":
            candidates.append(profile["artist_name"])
        elif logical == "contact_email":
            candidates.append(profile["contact_email"])
        elif logical == "streaming_link":
            candidates.append(profile.get("streaming_link", ""))
        elif logical == "bio_short":
            candidates.append(profile.get("artist_bio_short", "") or draft.body[:500])
        elif logical:
            candidates.append(profile.get(logical, ""))

    if "email" in hay:
        candidates.append(profile.get("contact_email", ""))
    if any(token in hay for token in ("name", "artist", "band", "act", "performer")):
        candidates.append(profile.get("artist_name", ""))
    if any(token in hay for token in ("message", "bio", "description", "comments", "about")):
        candidates.append(profile.get("pitch_message_long", ""))
        candidates.append(profile.get("artist_bio_short", ""))
        candidates.append(profile.get("artist_bio_long", ""))
        candidates.append(draft.body)
    if any(token in hay for token in ("subject", "title", "release", "track", "song")):
        candidates.append(profile.get("release_title", ""))
        candidates.append(profile.get("song_title", ""))
        candidates.append(draft.subject)
    if any(token in hay for token in ("website", "url", "spotify", "youtube", "soundcloud", "link", "stream")):
        for key in (
            "spotify_url",
            "soundcloud_url",
            "youtube_url",
            "bandcamp_url",
            "streaming_link",
            "artist_website",
            "press_release_url",
            "website",
        ):
            if profile.get(key):
                candidates.append(profile[key])
    if "phone" in hay or "mobile" in hay:
        if profile.get("contact_phone") or profile.get("phone"):
            candidates.append(profile.get("contact_phone", "") or profile.get("phone", ""))
    if "company" in hay or "label" in hay:
        candidates.append(profile.get("label_name", "") or profile.get("company", ""))
    if "genre" in hay or "style" in hay:
        candidates.append(profile.get("genre", ""))
    if "country" in hay:
        candidates.append(profile.get("artist_country", "") or profile.get("country", ""))
    if "city" in hay:
        if profile.get("artist_city") or profile.get("city"):
            candidates.append(profile.get("artist_city", "") or profile.get("city", ""))

    cleaned = []
    for value in candidates:
        value = (value or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _execute_form_submission(session: Session, draft: ContactDraft, send: ContactSend, resolved_target: str | None) -> tuple[str, str, dict[str, object]]:
    form = _resolve_form_for_execute(session, draft, resolved_target)
    run = start_execute_submission_run(
        session=session,
        station_id=draft.station_id,
        form_id=form.id if form is not None else None,
        target_url=resolved_target,
        mode="execute",
        max_retries=1,
    )
    summary = _parse_json(run.summary_json, {})
    attempts = summary.get("attempts", []) if isinstance(summary, dict) else []
    last_attempt = attempts[-1] if isinstance(attempts, list) and attempts else {}
    details = str(last_attempt.get("details") or run.blocked_reason or run.status)
    return ("sent" if run.status == "completed" else "blocked" if run.status == "blocked" else "failed"), details, {
        "agent_run_id": run.id,
        "form_id": run.form_id,
        "form_url": run.target_url,
        "attempts": attempts,
        "last_attempt": last_attempt,
        "blocked_reason": run.blocked_reason,
    }


def create_execute_send(
    session: Session,
    draft_id: int,
    target_value: str | None = None,
    channel: str | None = None,
) -> ContactSendPayload | None:
    draft = session.get(ContactDraft, draft_id)
    if draft is None:
        return None

    resolved_channel, resolved_target, routes = _resolve_send_target(draft, channel, target_value)
    send = ContactSend(
        draft_id=draft.id,
        station_id=draft.station_id,
        channel=resolved_channel,
        target_value=resolved_target,
        mode="execute",
        status="created",
        payload_json=json.dumps(
            {
                "subject": draft.subject,
                "body": draft.body,
                "recommended_channel": draft.recommended_channel,
                "target_value": resolved_target,
                "available_routes": routes,
            },
            ensure_ascii=False,
        ),
    )
    session.add(send)
    session.flush()

    outcome_status = "created"
    outcome_type = "execute-attempt"
    outcome_details: str | None = None
    outcome_payload: dict[str, object] = {
        "channel": resolved_channel,
        "target_value": resolved_target,
    }

    try:
        if "email" in resolved_channel:
            smtp_ok, smtp_reason = _smtp_ready()
            if not smtp_ok:
                send.status = "failed"
                outcome_status = "failed"
                outcome_details = f"SMTP not configured: {smtp_reason}"
                outcome_payload["reason"] = smtp_reason
            elif not resolved_target:
                send.status = "failed"
                outcome_status = "failed"
                outcome_details = "No recipient email resolved."
                outcome_payload["reason"] = "recipient_missing"
            else:
                _send_via_smtp(resolved_target, draft.subject, draft.body)
                send.status = "sent"
                draft.status = "sent"
                outcome_status = "sent"
                outcome_details = f"SMTP dispatch executed to {resolved_target}."
        elif "form" in resolved_channel:
            send.status, outcome_details, form_payload = _execute_form_submission(session, draft, send, resolved_target)
            outcome_status = "sent" if send.status == "sent" else "failed" if send.status == "failed" else "blocked"
            outcome_type = "execute-form-submit"
            outcome_payload.update(form_payload)
            if send.status == "sent":
                draft.status = "sent"
        else:
            send.status = "blocked"
            outcome_status = "blocked"
            outcome_type = "execute-blocked"
            outcome_details = "No executable route resolved for this draft."
            outcome_payload["reason"] = "no_executable_route"
    except Exception as exc:
        send.status = "failed"
        outcome_status = "failed"
        outcome_details = str(exc).strip() or exc.__class__.__name__
        outcome_payload["reason"] = "exception"
        outcome_payload["error"] = outcome_details

    outcome = ContactOutcome(
        send_id=send.id,
        outcome_type=outcome_type,
        status=outcome_status,
        details=outcome_details,
        payload_json=json.dumps(outcome_payload, ensure_ascii=False),
    )
    session.add(outcome)
    session.commit()
    session.refresh(send)
    return _serialize_send(send)


def list_draft_sends(session: Session, draft_id: int, limit: int = 20) -> list[ContactSendPayload]:
    sends = session.scalars(
        select(ContactSend)
        .where(ContactSend.draft_id == draft_id)
        .order_by(ContactSend.created_at.desc(), ContactSend.id.desc())
        .limit(limit)
    ).all()
    return [_serialize_send(send) for send in sends]


def list_send_outcomes(session: Session, send_id: int, limit: int = 20) -> list[ContactOutcomePayload]:
    outcomes = session.scalars(
        select(ContactOutcome)
        .where(ContactOutcome.send_id == send_id)
        .order_by(ContactOutcome.created_at.desc(), ContactOutcome.id.desc())
        .limit(limit)
    ).all()
    return [_serialize_outcome(outcome) for outcome in outcomes]

from __future__ import annotations

import json
import re
from hashlib import md5
from pathlib import Path
from typing import Any

from openai import OpenAI

from radio_db.config import settings
from radio_db.models.schemas import StationExtract
from radio_db.services.budget import ApiUsageGuard, CostGuard


ROLE_HINTS = [
    ("music director", "music_director"),
    ("program director", "program_director"),
    ("producer", "producer"),
    ("presenter", "host"),
    ("host", "host"),
    ("dj", "dj"),
    ("editor", "editor"),
]


def _has_openai() -> bool:
    return bool(settings.openai_api_key)


def _has_xai() -> bool:
    return bool(settings.xai_api_key)


def choose_llm_provider(routing_key: str = "") -> str:
    mode = (settings.llm_provider_mode or "openai").strip().lower()
    xai_pct = max(0, min(100, int(settings.llm_hybrid_xai_percent)))

    if mode == "xai":
        return "xai" if _has_xai() else ("openai" if _has_openai() else "none")
    if mode == "hybrid":
        if not _has_openai() and not _has_xai():
            return "none"
        if not _has_openai():
            return "xai"
        if not _has_xai():
            return "openai"
        digest = md5((routing_key or "default").encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return "xai" if bucket < xai_pct else "openai"
    return "openai" if _has_openai() else ("xai" if _has_xai() else "none")


def _client_for_provider(provider: str) -> OpenAI | None:
    if provider == "xai":
        if not settings.xai_api_key:
            return None
        return OpenAI(api_key=settings.xai_api_key, base_url=settings.xai_base_url)
    if provider == "openai":
        if not settings.openai_api_key:
            return None
        return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    return None


def model_for_provider(provider: str) -> str:
    if provider == "xai":
        return settings.xai_model
    return settings.llm_model


def _xai_estimated_call_usd(prompt_text: str) -> float:
    input_tokens = CostGuard.estimate_input_tokens(prompt_text)
    return (input_tokens / 1_000_000) * settings.xai_input_price_per_1m + (
        settings.llm_estimated_output_tokens / 1_000_000
    ) * settings.xai_output_price_per_1m


def _response_usage_usd(response: Any) -> float:
    try:
        payload = response.model_dump() if hasattr(response, "model_dump") else {}
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
    except Exception:
        input_tokens = 0
        output_tokens = 0
    return (input_tokens / 1_000_000) * settings.xai_input_price_per_1m + (
        output_tokens / 1_000_000
    ) * settings.xai_output_price_per_1m


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[\\s\\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\\s\\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\s+", " ", text)
    return text.strip()


def _heuristic_contacts_from_text(text: str, url: str) -> list[dict]:
    lowered = text.lower()
    role = "unknown"
    for hint, mapped in ROLE_HINTS:
        if hint in lowered:
            role = mapped
            break

    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.I)
    unique_emails = list(dict.fromkeys(e.lower() for e in emails))[:3]
    contacts: list[dict] = []
    for email in unique_emails:
        contacts.append(
            {
                "name": None,
                "role": role,
                "show_name": None,
                "email": email,
                "contact_url": url,
                "notes": "Auto-extracted from page content.",
                "confidence": 0.42,
            }
        )
    return contacts


def _heuristic_extract(title: str, snippet: str, url: str) -> StationExtract:
    lowered = f"{title} {snippet}".lower()
    method = "form" if "submit" in lowered or "submission" in lowered else "unknown"
    genres = []
    for g in ["rock", "pop", "hip hop", "jazz", "electronic", "country", "dance"]:
        if g in lowered:
            genres.append(g)

    return StationExtract(
        canonical_name=title.split("|")[0].strip() or "Unknown Station",
        aliases=[],
        website_url=url,
        genres=genres,
        confidence=0.35,
        contacts=_heuristic_contacts_from_text(f"{title}\n{snippet}", url),
        submissions=[
            {
                "method": method,
                "url": url if method == "form" else None,
                "email": None,
                "requirements": None,
                "accepts_newcomers": "new artist" in lowered or "newcomer" in lowered,
            }
        ],
    )


def extract_station_from_text(
    title: str,
    snippet: str,
    url: str,
    page_text: str | None = None,
    use_llm: bool = True,
    provider_hint: str | None = None,
) -> StationExtract:
    provider = provider_hint or choose_llm_provider(routing_key=url or title)
    if provider == "none" or not use_llm:
        return _heuristic_extract(title, snippet, url)
    client = _client_for_provider(provider)
    if client is None:
        return _heuristic_extract(title, snippet, url)
    text = f"TITLE: {title}\nURL: {url}\nSNIPPET: {snippet}\nCONTENT: {(page_text or '')[:settings.llm_max_prompt_chars]}"
    xai_estimated_usd = 0.0
    xai_usage: ApiUsageGuard | None = None
    if provider == "xai":
        xai_estimated_usd = _xai_estimated_call_usd(text)
        xai_usage = ApiUsageGuard(
            state_path=Path(".radio_db_state") / "api_usage.json",
            max_brave_calls_per_month=settings.max_brave_calls_per_month,
        )
        if not xai_usage.can_call_grok(
            max_daily_calls=settings.max_grok_calls_per_day,
            max_monthly_calls=settings.max_grok_calls_per_month,
            max_monthly_usd=settings.max_grok_usd_per_month,
            estimated_call_usd=max(settings.grok_estimated_cost_per_call_usd, xai_estimated_usd),
        ):
            return _heuristic_extract(title, snippet, url)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "canonical_name": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "country_code": {"type": ["string", "null"]},
            "language": {"type": ["string", "null"]},
            "city": {"type": ["string", "null"]},
            "website_url": {"type": ["string", "null"]},
            "stream_url": {"type": ["string", "null"]},
            "genres": {"type": "array", "items": {"type": "string"}},
            "programs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": ["string", "null"]},
                        "schedule": {"type": ["string", "null"]},
                    },
                    "required": ["name", "description", "schedule"],
                    "additionalProperties": False,
                },
            },
            "submissions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "url": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "requirements": {"type": ["string", "null"]},
                        "accepts_newcomers": {"type": "boolean"},
                    },
                    "required": ["method", "url", "email", "requirements", "accepts_newcomers"],
                    "additionalProperties": False,
                },
            },
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "role": {"type": "string"},
                        "show_name": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "contact_url": {"type": ["string", "null"]},
                        "notes": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["name", "role", "show_name", "email", "contact_url", "notes", "confidence"],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number"},
        },
        "required": [
            "canonical_name",
            "aliases",
            "country_code",
            "language",
            "city",
            "website_url",
            "stream_url",
            "genres",
            "programs",
            "submissions",
            "contacts",
            "confidence",
        ],
        "additionalProperties": False,
    }

    try:
        response = client.responses.create(
            model=model_for_provider(provider),
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract structured radio station data. Return only valid JSON and prefer null when unknown. "
                        "Focus on song submission links/forms for newcomer artists. "
                        "Extract show names and people/roles relevant for pitching (host, DJ, music director, producer)."
                    ),
                },
                {"role": "user", "content": text},
            ],
            text={"format": {"type": "json_schema", "name": "station_extract", "schema": schema, "strict": True}},
        )
        if provider == "xai" and xai_usage is not None:
            actual_usd = _response_usage_usd(response)
            charged_usd = max(actual_usd, xai_estimated_usd, settings.grok_estimated_cost_per_call_usd)
            xai_usage.register_grok_call(usd_spent=charged_usd)
        payload = json.loads(response.output_text)
        station = StationExtract.model_validate(payload)
        if not station.contacts:
            station.contacts = _heuristic_contacts_from_text(f"{title}\n{snippet}\n{page_text or ''}", url)
        return station
    except Exception:
        return _heuristic_extract(title, f"{snippet}\n{(page_text or '')[:2500]}", url)


def extract_page_text_for_prompt(html: str) -> str:
    return _strip_html(html)[:12000]

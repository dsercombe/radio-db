from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import OpenAI

from radio_db.config import settings
from radio_db.services.budget import ApiUsageGuard, CostGuard


COUNTRY_LANG = {
    "US": "en",
    "GB": "en",
    "CA": "en",
    "AU": "en",
    "IE": "en",
    "NZ": "en",
    "DE": "de",
    "AT": "de",
    "CH": "de",
    "FR": "fr",
    "BE": "fr",
    "ES": "es",
    "MX": "es",
    "AR": "es",
    "CO": "es",
    "IT": "it",
    "PT": "pt",
    "BR": "pt",
    "NL": "nl",
    "SE": "sv",
    "NO": "no",
    "DK": "da",
    "FI": "fi",
    "PL": "pl",
    "JP": "ja",
    "KR": "ko",
    "IN": "hi",
    "ZA": "en",
}


def _target_lang(country: str) -> str:
    c = (country or "").strip().upper()
    return COUNTRY_LANG.get(c, "en")


def _extract_message_text(payload: dict[str, Any]) -> str:
    out: list[str] = []
    for item in payload.get("output", []) if isinstance(payload, dict) else []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                txt = (content.get("text") or "").strip()
                if txt:
                    out.append(txt)
    return "\n".join(out).strip()


@lru_cache(maxsize=4096)
def _translate_cached(query: str, country: str) -> tuple[str, str, bool]:
    lang = _target_lang(country)
    q = (query or "").strip()
    if not q:
        return "", lang, False
    if lang == "en":
        return q, lang, False
    if not settings.agent_enabled:
        return q, lang, False
    if not settings.openai_api_key:
        return q, lang, False

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )
    estimated_input_tokens = CostGuard.estimate_input_tokens(prompt := (
        f"Translate this web search query into {lang} for local search in {country}. "
        "Keep search intent, keep quotes if present, and return only the translated query text.\n\n"
        f"Query: {q}"
    ))
    estimated_usd = (
        (estimated_input_tokens / 1_000_000) * settings.openai_input_price_per_1m
        + (settings.query_translate_estimated_output_tokens / 1_000_000) * settings.openai_output_price_per_1m
    ) * max(0.1, float(settings.openai_cost_multiplier))
    estimated_usd = max(estimated_usd, max(0.0, float(settings.openai_hard_min_call_usd)))
    if (not budget.can_call_llm_today()) or (not budget.can_spend(estimated_usd)):
        return q, lang, False
    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    if settings.openai_hard_guard_enabled and not usage.can_call_openai_api(
        max_daily_calls=settings.openai_hard_max_calls_per_day,
        max_monthly_calls=settings.openai_hard_max_calls_per_month,
        max_daily_usd=settings.openai_hard_max_usd_per_day,
        max_monthly_usd=settings.openai_hard_max_usd_per_month,
        estimated_call_usd=estimated_usd,
    ):
        return q, lang, False
    if settings.openai_hard_guard_enabled:
        usage.register_openai_api_call(usd_spent=estimated_usd)

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    try:
        response = client.responses.create(
            model=settings.query_translate_model,
            input=prompt,
            timeout=25,
        )
        budget.register_call(estimated_usd)
        payload = response.model_dump()
        translated = _extract_message_text(payload)
        translated = (translated or "").strip().strip('"').strip("'")
        if not translated:
            return q, lang, False
        return translated, lang, True
    except Exception:
        return q, lang, False


def translate_query_for_country(query: str, country: str) -> dict[str, Any]:
    translated, lang, used_api = _translate_cached(query, country)
    return {
        "query": translated or query,
        "lang": lang,
        "translated": bool(used_api and translated and translated != query),
        "api_call": bool(used_api),
    }

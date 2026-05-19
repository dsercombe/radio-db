from __future__ import annotations

from pathlib import Path

from radio_db.config import settings
from radio_db.connectors.brave_keys import brave_search_keys, effective_monthly_limit, pick_key
from radio_db.connectors.http import get_json
from radio_db.services.api_call_history import log_api_call
from radio_db.services.budget import ApiUsageGuard


def search_web(query: str, count: int = 20, country: str | None = None) -> list[dict]:
    keys = brave_search_keys()
    if not settings.enable_brave_search or not keys:
        log_api_call(
            provider="brave",
            operation="search_web",
            query=query,
            country=country,
            status="disabled_or_missing_key",
            meta={"count": count},
        )
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=effective_monthly_limit(settings.max_brave_calls_per_month, len(keys)),
    )
    if not usage.can_call_brave():
        log_api_call(
            provider="brave",
            operation="search_web",
            query=query,
            country=country,
            status="skipped_quota",
            meta={
                "count": count,
                "quota": "monthly",
                "effective_monthly_calls": effective_monthly_limit(settings.max_brave_calls_per_month, len(keys)),
                "month_calls": usage.state.brave_calls,
                "keys": len(keys),
            },
        )
        return []
    api_key = pick_key(keys, usage.state.brave_calls)

    params = {
        "q": query,
        "count": min(max(count, 1), 20),
        "text_decorations": "false",
        "spellcheck": "true",
    }
    if country:
        params["country"] = country.upper()

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    try:
        payload = get_json(settings.brave_base_url, params=params, headers=headers)
    except Exception as exc:
        log_api_call(
            provider="brave",
            operation="search_web",
            query=query,
            country=country,
            status="error",
            error=f"{exc.__class__.__name__}:{str(exc)[:220]}",
            meta={"count": count, "keys": len(keys)},
        )
        return []

    usage.register_brave_call()
    web_results = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
    log_api_call(
        provider="brave",
        operation="search_web",
        query=query,
        country=country,
        status="success",
        result_count=len(web_results),
        meta={"count": count, "keys": len(keys)},
    )
    return web_results

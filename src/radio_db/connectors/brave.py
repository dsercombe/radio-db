from __future__ import annotations

from pathlib import Path

from radio_db.config import settings
from radio_db.connectors.http import get_json
from radio_db.services.budget import ApiUsageGuard


def search_web(query: str, count: int = 20, country: str | None = None) -> list[dict]:
    if not settings.enable_brave_search or not settings.brave_api_key:
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    if not usage.can_call_brave():
        return []

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
        "X-Subscription-Token": settings.brave_api_key,
    }
    try:
        payload = get_json(settings.brave_base_url, params=params, headers=headers)
    except Exception:
        return []

    usage.register_brave_call()
    web_results = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
    return web_results

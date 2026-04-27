from __future__ import annotations

from pathlib import Path

from radio_db.config import settings
from radio_db.connectors.http import get_json
from radio_db.services.budget import ApiUsageGuard


def search_google_cse(query: str, count: int = 10, country: str | None = None) -> list[dict]:
    if not settings.enable_google_cse_search or not settings.google_cse_api_key or not settings.google_cse_cx:
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    if not usage.can_call_google_cse(
        max_daily_calls=settings.max_google_cse_calls_per_day,
        max_monthly_calls=settings.max_google_cse_calls_per_month,
    ):
        return []

    params = {
        "key": settings.google_cse_api_key,
        "cx": settings.google_cse_cx,
        "q": query,
        "num": min(max(count, 1), 10),
    }
    if country:
        params["gl"] = country.lower()

    try:
        payload = get_json(settings.google_cse_base_url, params=params)
    except Exception:
        return []

    usage.register_google_cse_call()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    results: list[dict] = []
    for item in items:
        results.append(
            {
                "title": item.get("title", "") or "",
                "description": item.get("snippet", "") or "",
                "url": item.get("link", "") or "",
                "source": "google_cse",
            }
        )
    return results

from __future__ import annotations

from pathlib import Path

from radio_db.config import settings
from radio_db.connectors.http import post_json
from radio_db.services.api_call_history import log_api_call
from radio_db.services.budget import ApiUsageGuard
from radio_db.services.key_rotation import next_key_index


def _linkup_keys() -> list[str]:
    keys: list[str] = []
    if settings.linkup_api_keys:
        keys.extend(k.strip() for k in settings.linkup_api_keys.split(",") if k.strip())
    if settings.linkup_api_key:
        keys.append(settings.linkup_api_key.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def search_linkup(query: str, count: int = 10, country: str | None = None) -> list[dict]:
    keys = _linkup_keys()
    if not settings.enable_linkup_search or not keys:
        log_api_call(
            provider="linkup",
            operation="search",
            query=query,
            country=country,
            status="disabled_or_missing_key",
            meta={"count": count},
        )
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    effective_daily_calls = settings.max_linkup_calls_per_day * max(1, len(keys))
    effective_monthly_calls = settings.max_linkup_calls_per_month * max(1, len(keys))
    if not usage.can_call_linkup(
        max_daily_calls=effective_daily_calls,
        max_monthly_calls=effective_monthly_calls,
    ):
        log_api_call(
            provider="linkup",
            operation="search",
            query=query,
            country=country,
            status="skipped_quota",
            meta={
                "count": count,
                "effective_daily_calls": effective_daily_calls,
                "effective_monthly_calls": effective_monthly_calls,
                "day_calls": usage.state.linkup_calls_day,
                "month_calls": usage.state.linkup_calls_month,
            },
        )
        return []

    payload: dict[str, object] = {
        "q": query,
        "depth": "standard",
        "outputType": "searchResults",
    }
    if country:
        payload["q"] = f"{query} {country}"

    start = next_key_index("linkup", len(keys))
    response = None
    last_error = ""
    attempts = 0
    for offset in range(len(keys)):
        attempts += 1
        key = keys[(start + offset) % len(keys)]
        headers = {"Authorization": f"Bearer {key}"}
        try:
            response = post_json(settings.linkup_base_url, payload=payload, headers=headers)
            break
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}:{str(exc)[:220]}"
            continue
    if response is None:
        log_api_call(
            provider="linkup",
            operation="search",
            query=query,
            country=country,
            status="error",
            error=last_error,
            meta={"count": count, "attempts": attempts, "keys": len(keys)},
        )
        return []

    usage.register_linkup_call()
    rows = response.get("results", []) if isinstance(response, dict) else []
    log_api_call(
        provider="linkup",
        operation="search",
        query=query,
        country=country,
        status="success",
        result_count=len(rows),
        meta={"count": count, "attempts": attempts, "keys": len(keys)},
    )
    results: list[dict] = []
    for row in rows[: min(max(count, 1), 20)]:
        results.append(
            {
                "title": row.get("name", "") or row.get("title", "") or "",
                "description": row.get("content", "") or row.get("snippet", "") or "",
                "url": row.get("url", "") or "",
                "source": "linkup",
            }
        )
    return results

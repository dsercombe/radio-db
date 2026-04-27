from __future__ import annotations

from radio_db.config import settings
from radio_db.connectors.http import get_json


def fetch_station_seeds_page(limit: int = 500, offset: int = 0, language: str | None = None) -> list[dict]:
    params = {
        "hidebroken": "true",
        "order": "clickcount",
        "reverse": "true",
        "limit": str(limit),
        "offset": str(max(offset, 0)),
    }
    if language:
        params["language"] = language
    url = f"{settings.radio_browser_base_url}/stations/search"
    try:
        payload = get_json(url, params=params)
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def fetch_station_seeds(limit: int = 500, language: str | None = None) -> list[dict]:
    return fetch_station_seeds_page(limit=limit, offset=0, language=language)

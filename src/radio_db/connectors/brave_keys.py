from __future__ import annotations

from radio_db.config import settings


def _split_keys(raw: str | None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for value in str(raw or "").replace("\n", ",").split(","):
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def brave_search_keys() -> list[str]:
    keys = _split_keys(settings.brave_api_keys)
    fallback = str(settings.brave_api_key or "").strip()
    if fallback and fallback not in keys:
        keys.insert(0, fallback)
    return keys


def brave_answer_keys() -> list[str]:
    keys = _split_keys(settings.brave_answer_api_keys)
    fallback = str(settings.brave_answer_api_key or "").strip()
    if fallback and fallback not in keys:
        keys.insert(0, fallback)
    return keys


def phase2_brave_answer_keys() -> list[str]:
    keys = _split_keys(settings.phase2_brave_answer_api_keys)
    fallback = str(settings.phase2_brave_answer_api_key or "").strip()
    if fallback and fallback not in keys:
        keys.insert(0, fallback)
    return keys


def pick_key(keys: list[str], call_count: int) -> str:
    if not keys:
        return ""
    return keys[max(0, int(call_count or 0)) % len(keys)]


def effective_monthly_limit(base_limit: int, key_count: int) -> int:
    return max(0, int(base_limit or 0)) * max(1, int(key_count or 0))


def effective_daily_limit(base_limit: int, key_count: int) -> int:
    return max(0, int(base_limit or 0)) * max(1, int(key_count or 0))

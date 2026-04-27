from __future__ import annotations

import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.parse import quote_plus

from radio_db.config import settings
from radio_db.connectors.http import get_text
from radio_db.services.budget import ApiUsageGuard


_RESULT_RE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.I | re.S,
)
_LITE_RESULT_RE = re.compile(
    r'<a[^>]*class="[^"]*result-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_CHALLENGE_MARKERS = ("anomaly-modal", "Unfortunately, bots use DuckDuckGo too", "/anomaly.js?")


def _clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def _extract_href(raw_href: str) -> str:
    if raw_href.startswith("http://") or raw_href.startswith("https://"):
        if "duckduckgo.com/l/?" not in raw_href:
            return raw_href
        query = parse_qs(urlparse(raw_href).query)
        uddg = query.get("uddg", [""])[0]
        return unquote(uddg) if uddg else raw_href
    return raw_href


def _ddg_state_path() -> Path:
    return Path(".radio_db_state") / "duckduckgo_state.json"


def _load_ddg_state() -> dict:
    path = _ddg_state_path()
    if not path.exists():
        return {"blocked_until": "", "challenge_count": 0, "last_challenge_at": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"blocked_until": "", "challenge_count": 0, "last_challenge_at": ""}


def _save_ddg_state(state: dict) -> None:
    path = _ddg_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _is_temporarily_blocked(state: dict) -> bool:
    blocked_until = str(state.get("blocked_until") or "")
    if not blocked_until:
        return False
    try:
        until = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.now(timezone.utc) < until


def _register_challenge(state: dict, cooldown_minutes: int = 120) -> None:
    now = datetime.now(timezone.utc)
    state["challenge_count"] = int(state.get("challenge_count", 0)) + 1
    state["last_challenge_at"] = now.isoformat()
    state["blocked_until"] = (now + timedelta(minutes=cooldown_minutes)).isoformat()
    _save_ddg_state(state)


def _clear_block(state: dict) -> None:
    if state.get("blocked_until"):
        state["blocked_until"] = ""
        _save_ddg_state(state)


def _is_challenge_response(html: str) -> bool:
    return any(marker in html for marker in _CHALLENGE_MARKERS)


def search_duckduckgo(query: str, count: int = 10) -> list[dict]:
    if not settings.enable_duckduckgo_search:
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    if not usage.can_call_duckduckgo(
        max_daily_calls=settings.max_duckduckgo_calls_per_day,
        max_monthly_calls=settings.max_duckduckgo_calls_per_month,
    ):
        return []

    state = _load_ddg_state()
    if _is_temporarily_blocked(state):
        return []

    html = ""
    try:
        html = get_text(
            "https://html.duckduckgo.com/html/?q=" + quote_plus(query),
            headers={
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            },
        )
    except Exception:
        html = ""

    if not html:
        return []
    if _is_challenge_response(html):
        _register_challenge(state)
        return []

    matches = _RESULT_RE.findall(html)
    if not matches:
        # Secondary parser shape used on some lightweight layouts.
        matches = _LITE_RESULT_RE.findall(html)
    if not matches:
        return []

    _clear_block(state)
    usage.register_duckduckgo_call()

    results: list[dict] = []
    for href, title_html in matches[: max(1, count)]:
        url = _extract_href(href)
        if not url:
            continue
        results.append(
            {
                "title": _clean_html(title_html),
                "description": "",
                "url": url,
                "source": "duckduckgo",
            }
        )
    return results

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


STATE_PATH = Path(".radio_db_state") / "provider_cooldowns.json"


def _load_state() -> dict[str, dict[str, str]]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            out: dict[str, dict[str, str]] = {}
            for provider, item in raw.items():
                if isinstance(provider, str) and isinstance(item, dict):
                    out[provider] = {
                        "blocked_until": str(item.get("blocked_until") or ""),
                        "reason": str(item.get("reason") or ""),
                    }
            return out
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, dict[str, str]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_blocked_until(provider: str) -> str | None:
    name = (provider or "").strip().lower()
    if not name:
        return None
    state = _load_state()
    row = state.get(name) or {}
    blocked_until = str(row.get("blocked_until") or "")
    if not blocked_until:
        return None
    try:
        until = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
    except Exception:
        return None
    now = datetime.now(UTC)
    if until <= now:
        # Auto-clear expired cooldown entries.
        state.pop(name, None)
        _save_state(state)
        return None
    return until.astimezone(UTC).isoformat()


def is_provider_blocked(provider: str) -> bool:
    return bool(get_blocked_until(provider))


def block_provider(provider: str, *, minutes: int, reason: str = "quota") -> str:
    name = (provider or "").strip().lower()
    safe_minutes = max(1, int(minutes))
    until = datetime.now(UTC) + timedelta(minutes=safe_minutes)
    state = _load_state()
    state[name] = {
        "blocked_until": until.isoformat(),
        "reason": (reason or "quota").strip().lower(),
    }
    _save_state(state)
    return until.isoformat()


def clear_provider_block(provider: str) -> None:
    name = (provider or "").strip().lower()
    if not name:
        return
    state = _load_state()
    if name in state:
        state.pop(name, None)
        _save_state(state)

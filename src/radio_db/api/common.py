from __future__ import annotations

from typing import Literal

from fastapi import Header, HTTPException

from radio_db.config import settings

PermissionMode = Literal["read-only", "dry-run", "execute"]

_MODE_RANK: dict[str, int] = {
    "read-only": 0,
    "dry-run": 1,
    "execute": 2,
}


def normalize_permission_mode(value: str | None) -> PermissionMode:
    raw = (value or "").strip().lower().replace("_", "-")
    if raw in {"readonly", "read-only", "read"}:
        return "read-only"
    if raw in {"dryrun", "dry-run", "dry"}:
        return "dry-run"
    if raw == "execute":
        return "execute"
    fallback = str(getattr(settings, "api_permission_mode", "dry-run"))
    safe_fallback = fallback.strip().lower().replace("_", "-")
    if safe_fallback in _MODE_RANK:
        return safe_fallback  # type: ignore[return-value]
    return "dry-run"


def get_permission_mode(
    x_radio_db_mode: str | None = Header(default=None, alias="X-Radio-DB-Mode"),
) -> PermissionMode:
    return normalize_permission_mode(x_radio_db_mode)


def require_permission_mode(minimum: PermissionMode):
    def _dependency(
        x_radio_db_mode: str | None = Header(default=None, alias="X-Radio-DB-Mode"),
    ) -> PermissionMode:
        mode = normalize_permission_mode(x_radio_db_mode)
        if _MODE_RANK[mode] < _MODE_RANK[minimum]:
            raise HTTPException(
                status_code=403,
                detail=f"insufficient_permission_mode:{minimum}_required_current_{mode}",
            )
        return mode

    return _dependency

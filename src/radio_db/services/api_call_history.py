from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


API_CALL_HISTORY_PATH = Path(".radio_db_state") / "api_call_history.jsonl"
MAX_HISTORY_FILE_BYTES = 5_000_000
MAX_HISTORY_LINES_KEEP = 20_000


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _trim_if_oversize(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size <= MAX_HISTORY_FILE_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        kept = lines[-MAX_HISTORY_LINES_KEEP:]
        path.write_text(("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8")
    except Exception:
        # History must never break production calls.
        return


def log_api_call(
    *,
    provider: str,
    operation: str,
    query: str,
    country: str | None,
    status: str,
    result_count: int | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ts_utc": datetime.now(UTC).isoformat(),
        "provider": provider,
        "operation": operation,
        "query": query,
        "country": (country or "").upper() or None,
        "status": status,
        "result_count": result_count,
        "error": (error or "")[:500] or None,
    }
    if meta:
        payload["meta"] = meta

    path = API_CALL_HISTORY_PATH
    _ensure_parent(path)
    _trim_if_oversize(path)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return


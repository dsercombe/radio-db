"""Gemini generateContent client with hard subprocess wall timeout.

In-process httpx reads can block inside native code; a Python ``wall_deadline`` check
inside ``iter_bytes()`` never runs while blocked waiting for the first byte. A child
process containing the full retry loop can be killed reliably when
``subprocess.run(..., timeout=...)`` fires.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from radio_db.config import settings

DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _subprocess_timeout_seconds(
    *,
    wall_timeout_per_attempt: float,
    max_attempts: int,
) -> float:
    """Upper bound for one full generateContent call (all retries, backoff, reads)."""
    ma = max(1, int(max_attempts))
    per = max(5.0, float(wall_timeout_per_attempt))
    # Per attempt: wall + read/connect slack + worst-case exponential backoff sleeps.
    slack = 75.0
    total = ma * (per + slack) + 45.0
    return min(max(total, 90.0), 900.0)


def _gemini_via_subprocess(req: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "radio_db.connectors.gemini_generate_worker"],
            input=json.dumps(req, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout_seconds,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        proc = exc.process
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise RuntimeError("gemini_subprocess_hard_timeout") from exc

    text_out = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text_out:
        err = (completed.stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"gemini_worker_no_stdout rc={completed.returncode} stderr={err!r}")

    last_line = text_out.splitlines()[-1]
    payload = json.loads(last_line)
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "gemini_worker_failed"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("gemini_worker_bad_data_shape")
    return data


def gemini_generate_content_json(
    *,
    url: str,
    params: dict[str, str],
    json_payload: dict[str, Any],
    wall_timeout_per_attempt: float,
    connect_timeout: float = 10.0,
    read_timeout: float = 45.0,
    write_timeout: float = 30.0,
    pool_timeout: float = 5.0,
    retry_statuses: frozenset[int] | None = None,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """POST to ``...:generateContent``; return parsed top-level JSON object."""
    retries = retry_statuses if retry_statuses is not None else DEFAULT_RETRY_STATUSES
    req = {
        "url": url,
        "params": params,
        "json_payload": json_payload,
        "wall_timeout_per_attempt": wall_timeout_per_attempt,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
        "write_timeout": write_timeout,
        "pool_timeout": pool_timeout,
        "max_attempts": max_attempts,
        "retry_statuses": sorted(retries),
        "trust_env": bool(settings.gemini_http_trust_env),
    }

    timeout_seconds = _subprocess_timeout_seconds(
        wall_timeout_per_attempt=wall_timeout_per_attempt,
        max_attempts=max_attempts,
    )
    return _gemini_via_subprocess(req, timeout_seconds)

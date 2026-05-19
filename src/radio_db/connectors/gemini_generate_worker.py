"""Isolated Gemini generateContent worker (run as ``python -m radio_db.connectors.gemini_generate_worker``).

Reads one JSON object from stdin; writes one JSON line to stdout. Used so the parent
process can enforce a hard wall timeout via ``subprocess.run(..., timeout=...)`` and
``kill`` — unlike in-process httpx reads, which may block without running Python code.
"""

from __future__ import annotations

import json
import random
import sys
import time
from typing import Any

import httpx

DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _backoff_with_jitter(attempt: int) -> float:
    base = min(2**max(0, attempt), 8)
    return max(0.2, base + random.uniform(0.0, 0.8))


def _run(stdin_json: dict[str, Any]) -> dict[str, Any]:
    url = str(stdin_json["url"])
    params = stdin_json["params"]
    json_payload = stdin_json["json_payload"]
    if not isinstance(params, dict) or not isinstance(json_payload, dict):
        raise ValueError("invalid_request_shape")

    wall = float(stdin_json.get("wall_timeout_per_attempt") or 120.0)
    connect_timeout = float(stdin_json.get("connect_timeout") or 10.0)
    read_timeout = float(stdin_json.get("read_timeout") or 45.0)
    write_timeout = float(stdin_json.get("write_timeout") or 30.0)
    pool_timeout = float(stdin_json.get("pool_timeout") or 5.0)
    max_attempts = max(1, int(stdin_json.get("max_attempts") or 4))
    raw_retries = stdin_json.get("retry_statuses")
    if isinstance(raw_retries, list):
        retries = frozenset(int(x) for x in raw_retries)
    else:
        retries = DEFAULT_RETRY_STATUSES

    trust_env = bool(stdin_json.get("trust_env", False))

    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )
    last_status: int | None = None
    last_error: str | None = None
    data: dict[str, Any] | None = None

    # Default trust_env=False avoids surprise HTTP(S)_PROXY routing to broken intermediaries.
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=trust_env) as client:
        for attempt in range(max_attempts):
            wall_deadline = time.monotonic() + max(5.0, wall)
            try:
                with client.stream(
                    "POST",
                    url,
                    params=params,
                    json=json_payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    last_status = resp.status_code
                    if last_status in retries and attempt < max_attempts - 1:
                        resp.read()
                        time.sleep(_backoff_with_jitter(attempt))
                        continue
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    for chunk in resp.iter_bytes():
                        if time.monotonic() > wall_deadline:
                            raise RuntimeError("gemini_wall_timeout")
                        if chunk:
                            chunks.append(chunk)
                raw = b"".join(chunks)
                if not raw.strip():
                    raise RuntimeError("gemini_empty_body")
                parsed: Any = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise RuntimeError("gemini_response_not_object")
                data = parsed
                break
            except httpx.HTTPStatusError as exc:
                last_status = exc.response.status_code
                try:
                    last_error = (exc.response.text or "")[:300]
                except Exception:
                    last_error = exc.__class__.__name__
                if last_status in retries and attempt < max_attempts - 1:
                    time.sleep(_backoff_with_jitter(attempt))
                    continue
                raise RuntimeError(f"gemini_http_error status={last_status} body={last_error}") from exc
            except httpx.HTTPError as exc:
                last_error = exc.__class__.__name__
                if attempt < max_attempts - 1:
                    time.sleep(_backoff_with_jitter(attempt))
                    continue
                raise RuntimeError(f"gemini_transport_error error={last_error}") from exc

    if data is None:
        raise RuntimeError(f"gemini_retry_exhausted status={last_status} error={last_error}")
    return data


def main() -> None:
    raw_in = sys.stdin.buffer.read().decode("utf-8")
    req = json.loads(raw_in)
    out = _run(req)
    sys.stdout.write(json.dumps({"ok": True, "data": out}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)[:800]}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        sys.exit(1)

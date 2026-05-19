from __future__ import annotations

import threading
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from radio_db.config import settings


def _get_text_once(url: str, headers: dict[str, str], timeout: float) -> str:
    result: dict[str, object] = {}

    def fetch() -> None:
        try:
            request_timeout = httpx.Timeout(
                timeout=max(1.0, timeout),
                connect=max(1.0, min(3.0, timeout)),
                read=max(1.0, timeout),
                write=max(1.0, min(3.0, timeout)),
                pool=max(1.0, min(3.0, timeout)),
            )
            with httpx.Client(timeout=request_timeout, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                result["text"] = response.text
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()
    thread.join(max(1.0, timeout + 1.0))
    if thread.is_alive():
        raise TimeoutError("http_wall_timeout")
    error = result.get("error")
    if isinstance(error, BaseException):
        raise error
    return str(result.get("text") or "")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list:
    default_headers = {"User-Agent": settings.crawl_user_agent}
    if headers:
        default_headers.update(headers)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(url, params=params, headers=default_headers)
        response.raise_for_status()
        return response.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def post_json(url: str, payload: dict, headers: dict | None = None) -> dict | list:
    default_headers = {"User-Agent": settings.crawl_user_agent, "Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.post(url, json=payload, headers=default_headers)
        response.raise_for_status()
        return response.json()


def get_text(
    url: str,
    headers: dict | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
) -> str:
    default_headers = {"User-Agent": settings.crawl_user_agent}
    if headers:
        default_headers.update(headers)
    timeout = float(timeout_seconds) if timeout_seconds is not None else 30.0
    attempts = max(1, int(max_attempts) if max_attempts is not None else 1)
    for attempt in range(attempts):
        try:
            return _get_text_once(url=url, headers=default_headers, timeout=timeout)
        except Exception:
            if attempt < attempts - 1:
                time.sleep(min(2**attempt, 6.0))
                continue
            raise

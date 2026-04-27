from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from radio_db.config import settings


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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def get_text(url: str, headers: dict | None = None) -> str:
    default_headers = {"User-Agent": settings.crawl_user_agent}
    if headers:
        default_headers.update(headers)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(url, headers=default_headers)
        response.raise_for_status()
        return response.text

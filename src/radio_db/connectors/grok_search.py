from __future__ import annotations

import re
from pathlib import Path

from openai import OpenAI

from radio_db.config import settings
from radio_db.services.budget import ApiUsageGuard


def _clean_desc(text: str) -> str:
    text = re.sub(r"\[\[\d+\]\]\([^)]+\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_rows_from_message_text(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*[-*]\s*(https?://\S+)\s*:\s*(.+)$", line.strip())
        if not m:
            continue
        url = m.group(1).strip().rstrip(".,)")
        desc = _clean_desc(m.group(2))
        rows.append({"url": url, "description": desc})
    return rows


def search_grok(query: str, count: int = 10, country: str | None = None) -> list[dict]:
    if not settings.enable_grok_search or not settings.xai_api_key:
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    if not usage.can_call_grok(
        max_daily_calls=settings.max_grok_calls_per_day,
        max_monthly_calls=settings.max_grok_calls_per_month,
        max_monthly_usd=settings.max_grok_usd_per_month,
        estimated_call_usd=settings.grok_estimated_cost_per_call_usd,
    ):
        return []

    client = OpenAI(api_key=settings.xai_api_key, base_url=settings.xai_base_url)
    prompt = (
        f"Find up to {min(max(count, 1), 20)} relevant URLs for this search query and return concise bullets "
        f"in format '- URL: short note'. Query: {query}."
    )
    if country:
        prompt += f" Prefer sources relevant to country code {country}."

    try:
        response = client.responses.create(
            model=settings.xai_search_model,
            input=prompt,
            tools=[{"type": "web_search"}],
        )
    except Exception:
        return []

    payload = response.model_dump()
    usage_info = payload.get("usage", {}) if isinstance(payload, dict) else {}
    input_tokens = int(usage_info.get("input_tokens", 0) or 0)
    output_tokens = int(usage_info.get("output_tokens", 0) or 0)
    usd_spent = (input_tokens / 1_000_000) * settings.xai_input_price_per_1m + (
        output_tokens / 1_000_000
    ) * settings.xai_output_price_per_1m
    usage.register_grok_call(usd_spent=max(usd_spent, settings.grok_estimated_cost_per_call_usd))
    output_items = payload.get("output", []) if isinstance(payload, dict) else []

    message_texts: list[str] = []
    annotation_urls: list[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                txt = content.get("text", "")
                if txt:
                    message_texts.append(txt)
                for ann in content.get("annotations", []):
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        annotation_urls.append(str(ann["url"]).strip())

    rows: list[dict] = []
    for txt in message_texts:
        rows.extend(_extract_rows_from_message_text(txt))

    # If the model text contains no bullet lines, use cited URLs as fallback.
    if not rows and annotation_urls:
        rows = [{"url": u, "description": ""} for u in annotation_urls]

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        desc = (row.get("description") or "").strip()
        title = url
        out.append(
            {
                "title": title,
                "description": desc,
                "url": url,
                "source": "grok",
            }
        )
        if len(out) >= min(max(count, 1), 20):
            break

    return out

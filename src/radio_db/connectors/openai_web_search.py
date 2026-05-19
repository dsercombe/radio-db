from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from openai import OpenAI

from radio_db.config import settings
from radio_db.services.budget import ApiUsageGuard, CostGuard


def _extract_rows_from_message_text(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*[-*]\s*(https?://\S+)\s*:\s*(.+)$", line.strip())
        if not m:
            continue
        url = m.group(1).strip().rstrip(".,)")
        desc = re.sub(r"\s+", " ", m.group(2)).strip()
        rows.append({"url": url, "description": desc})
    return rows


def search_openai_web(
    query: str,
    count: int = 10,
    country: str | None = None,
    excluded_domains: list[str] | None = None,
) -> list[dict]:
    if (not settings.agent_enabled) or not settings.enable_openai_web_search or not settings.openai_api_key:
        return []

    usage = ApiUsageGuard(
        state_path=Path(".radio_db_state") / "api_usage.json",
        max_brave_calls_per_month=settings.max_brave_calls_per_month,
    )
    cost = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )
    if not usage.can_call_openai_web(
        max_daily_calls=settings.max_openai_web_calls_per_day,
        max_monthly_calls=settings.max_openai_web_calls_per_month,
    ):
        return []
    estimated = max(0.0, float(settings.openai_web_estimated_cost_per_call_usd)) * max(
        0.1,
        float(settings.openai_cost_multiplier),
    )
    estimated = max(estimated, max(0.0, float(settings.openai_hard_min_call_usd)))
    if (not cost.can_call_llm_today()) or (not cost.can_spend(estimated)):
        return []
    if settings.openai_hard_guard_enabled and not usage.can_call_openai_api(
        max_daily_calls=settings.openai_hard_max_calls_per_day,
        max_monthly_calls=settings.openai_hard_max_calls_per_month,
        max_daily_usd=settings.openai_hard_max_usd_per_day,
        max_monthly_usd=settings.openai_hard_max_usd_per_month,
        estimated_call_usd=estimated,
    ):
        return []
    if settings.openai_hard_guard_enabled:
        usage.register_openai_api_call(usd_spent=estimated)

    prompt = (
        f"Find up to {min(max(count, 1), 20)} relevant URLs for this query and return concise bullets "
        "in format '- URL: short note'. "
        "Only include official radio station websites or official station-owned contact/submission pages. "
        "Prioritize regional, community, college, independent and niche stations over already globally dominant stations. "
        "Exclude directory/listing/aggregator pages, app stores, social profiles, PR/marketing services, and generic blog posts. "
        f"Query: {query}."
    )
    if country:
        prompt += f" Prefer sources relevant to country code {country}."
    if excluded_domains:
        cleaned = [d.strip().lower() for d in excluded_domains if d and d.strip()]
        if cleaned:
            blocked = ", ".join(cleaned[:80])
            prompt += (
                " Do not return URLs from these already-known domains: "
                f"{blocked}."
            )

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    tool_candidates = [settings.openai_web_search_tool_type.strip() or "web_search_preview", "web_search_preview", "web_search"]
    tried: set[str] = set()
    response = None
    for tool_type in tool_candidates:
        if tool_type in tried:
            continue
        tried.add(tool_type)
        try:
            response = client.responses.create(
                model=settings.openai_web_search_model,
                input=prompt,
                tools=[{"type": tool_type}],
                timeout=45,
            )
            break
        except Exception:
            continue

    if response is None:
        return []

    usage.register_openai_web_call()
    cost.register_call(estimated)
    payload = response.model_dump()
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
        host = (urlparse(url).netloc or "").lower().replace("www.", "")
        title = host or url
        out.append(
            {
                "title": title,
                "description": f"{desc} query: {query}".strip(),
                "url": url,
                "source": "openai_web",
            }
        )
        if len(out) >= min(max(count, 1), 20):
            break

    return out

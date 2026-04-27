from __future__ import annotations

from typing import Any

from radio_db.config import settings
from radio_db.connectors.brave import search_web
from radio_db.connectors.duckduckgo import search_duckduckgo
from radio_db.connectors.grok_search import search_grok
from radio_db.connectors.google_cse import search_google_cse
from radio_db.connectors.linkup import search_linkup
from radio_db.connectors.tavily import search_tavily


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in results:
        url = (row.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(row)
    return out


def collect_search_results(
    query: str,
    country: str | None = None,
    include_linkup: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    per_source = max(1, min(settings.max_results_per_source, settings.max_search_results))
    calls = {"brave": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0}
    results: list[dict[str, Any]] = []

    if settings.enable_brave_search:
        brave = search_web(query=query, count=per_source, country=country)
        for row in brave:
            row["source"] = "brave"
        calls["brave"] += 1
        results.extend(brave)

    if settings.enable_google_cse_search:
        google = search_google_cse(query=query, count=min(per_source, 10), country=country)
        calls["google_cse"] += 1
        if settings.google_enable_detail_query and settings.google_detail_query_suffix.strip():
            google.extend(
                search_google_cse(
                    query=f'{query} {settings.google_detail_query_suffix.strip()}',
                    count=min(per_source, 10),
                    country=country,
                )
            )
            calls["google_cse"] += 1
        results.extend(google)

    if settings.enable_tavily_search:
        tavily = search_tavily(query=query, count=min(per_source, 20), country=country)
        calls["tavily"] += 1
        if settings.tavily_enable_detail_query and settings.tavily_detail_query_suffix.strip():
            tavily.extend(
                search_tavily(
                    query=f'{query} {settings.tavily_detail_query_suffix.strip()}',
                    count=min(per_source, 20),
                    country=country,
                )
            )
            calls["tavily"] += 1
        results.extend(tavily)

    if settings.enable_grok_search:
        grok = search_grok(query=query, count=min(per_source, 20), country=country)
        calls["grok"] += 1
        if settings.grok_enable_detail_query and settings.grok_detail_query_suffix.strip():
            grok.extend(
                search_grok(
                    query=f'{query} {settings.grok_detail_query_suffix.strip()}',
                    count=min(per_source, 20),
                    country=country,
                )
            )
            calls["grok"] += 1
        results.extend(grok)

    if include_linkup and settings.enable_linkup_search:
        linkup = search_linkup(query=query, count=min(per_source, 20), country=country)
        calls["linkup"] += 1
        if settings.linkup_enable_detail_query and settings.linkup_detail_query_suffix.strip():
            linkup.extend(
                search_linkup(
                    query=f'{query} {settings.linkup_detail_query_suffix.strip()}',
                    count=min(per_source, 20),
                    country=country,
                )
            )
            calls["linkup"] += 1
        results.extend(linkup)

    if settings.enable_duckduckgo_search:
        ddg = search_duckduckgo(query=query, count=min(per_source, 25))
        calls["duckduckgo"] += 1
        if settings.duckduckgo_enable_detail_query and settings.duckduckgo_detail_query_suffix.strip():
            ddg.extend(
                search_duckduckgo(
                    query=f'{query} {settings.duckduckgo_detail_query_suffix.strip()}',
                    count=min(per_source, 25),
                )
            )
            calls["duckduckgo"] += 1
        results.extend(ddg)

    return _dedupe_results(results), calls


def collect_station_enrichment_results(
    query: str,
    country: str | None = None,
    max_results: int = 8,
    include_google: bool = True,
    include_tavily: bool = True,
    include_grok: bool = True,
    include_linkup: bool = False,
    include_duckduckgo: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    per_source = max(1, min(max_results, settings.max_search_results))
    calls = {"brave": 0, "google_cse": 0, "tavily": 0, "grok": 0, "linkup": 0, "duckduckgo": 0}
    results: list[dict[str, Any]] = []

    if settings.enable_brave_search:
        brave = search_web(query=query, count=per_source, country=country)
        for row in brave:
            row["source"] = "brave"
        calls["brave"] += 1
        results.extend(brave)

    if include_tavily and settings.enable_tavily_search:
        tav = search_tavily(query=query, count=min(per_source, 20), country=country)
        calls["tavily"] += 1
        results.extend(tav)

    if include_grok and settings.enable_grok_search:
        grok = search_grok(query=query, count=min(per_source, 20), country=country)
        calls["grok"] += 1
        results.extend(grok)

    if include_linkup and settings.enable_linkup_search:
        linkup = search_linkup(query=query, count=min(per_source, 20), country=country)
        calls["linkup"] += 1
        results.extend(linkup)

    if include_google and settings.enable_google_cse_search:
        google = search_google_cse(query=query, count=min(per_source, 10), country=country)
        calls["google_cse"] += 1
        results.extend(google)

    if include_duckduckgo and settings.enable_duckduckgo_search:
        ddg = search_duckduckgo(query=query, count=min(per_source, 25))
        calls["duckduckgo"] += 1
        results.extend(ddg)

    deduped = _dedupe_results(results)
    return deduped[: max_results], calls

from __future__ import annotations

from radio_db.config import settings
from radio_db.connectors.http import get_json


def fetch_radio_stations_page(limit: int = 200, offset: int = 0) -> list[dict]:
    query = f"""
SELECT ?station ?stationLabel ?countryLabel ?officialWebsite WHERE {{
  ?station wdt:P31/wdt:P279* wd:Q14350 .
  OPTIONAL {{ ?station wdt:P17 ?country . }}
  OPTIONAL {{ ?station wdt:P856 ?officialWebsite . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en\". }}
}}
LIMIT {int(limit)}
OFFSET {max(offset, 0)}
""".strip()

    params = {"query": query, "format": "json"}
    headers = {"Accept": "application/sparql-results+json"}
    try:
        payload = get_json(settings.wikidata_sparql_url, params=params, headers=headers)
        return payload.get("results", {}).get("bindings", []) if isinstance(payload, dict) else []
    except Exception:
        return []


def fetch_radio_stations(limit: int = 200) -> list[dict]:
    return fetch_radio_stations_page(limit=limit, offset=0)

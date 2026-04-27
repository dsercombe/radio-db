from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.models.entities import Station
from radio_db.services.normalize import normalize_domain, normalize_text


def station_fingerprint(name: str, website_url: str | None, country_code: str | None) -> str:
    domain = normalize_domain(website_url)
    return "|".join([normalize_text(name), (country_code or "").upper(), domain])


def find_duplicate_station(
    session: Session,
    canonical_name: str,
    website_url: str | None,
    country_code: str | None,
    min_name_similarity: int = 92,
) -> Station | None:
    fp = station_fingerprint(canonical_name, website_url, country_code)
    direct = session.scalar(select(Station).where(Station.fingerprint == fp))
    if direct:
        return direct

    country_code = (country_code or "").upper()
    candidates = session.scalars(select(Station).where(Station.country_code == country_code)).all()
    website_domain = normalize_domain(website_url)

    for candidate in candidates:
        sim = fuzz.ratio(normalize_text(candidate.canonical_name), normalize_text(canonical_name))
        same_domain = normalize_domain(candidate.website_url) and normalize_domain(candidate.website_url) == website_domain
        if sim >= min_name_similarity and (same_domain or not website_domain):
            return candidate
    return None

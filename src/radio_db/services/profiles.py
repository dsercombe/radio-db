from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.models.entities import Evidence, SourceType, Station, StationContact, StationGenre, StationProfileSnapshot, StationProgram


STYLE_KEYWORDS = {
    "indie": ["indie", "alternative", "unsigned", "new artist", "emerging"],
    "mainstream": ["chart", "top 40", "hits", "mainstream"],
    "electronic": ["electronic", "house", "techno", "dance", "edm"],
    "talk": ["talk", "news", "podcast", "discussion"],
    "community": ["community", "local", "volunteer"],
}

EDITORIAL_KEYWORDS = {
    "open_submission": ["submit", "submission", "send us your music", "demo"],
    "playlist_driven": ["playlist", "rotation", "airplay"],
    "show_focused": ["show", "program", "schedule", "host", "dj"],
    "new_music_support": ["new music", "new artist", "unsigned", "emerging"],
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_+-]{2,}", text.lower())


def _pick_tags(text_blob: str, mapping: dict[str, list[str]]) -> list[str]:
    lowered = text_blob.lower()
    tags: list[str] = []
    for tag, needles in mapping.items():
        if any(n in lowered for n in needles):
            tags.append(tag)
    return tags


def _confidence_from_counts(source_count: int, diversity_score: float, style_count: int, signal_count: int) -> float:
    score = min(1.0, 0.15 + (source_count / 30.0) + (diversity_score * 0.35) + ((style_count + signal_count) * 0.05))
    return round(max(0.0, min(score, 1.0)), 3)


def build_station_profiles(session: Session, limit: int = 500, station_id: int | None = None) -> dict:
    station_stmt = select(Station).order_by(Station.updated_at.desc()).limit(limit)
    if station_id is not None:
        station_stmt = select(Station).where(Station.id == station_id).limit(1)
    stations = session.scalars(station_stmt).all()

    built = 0
    for station in stations:
        genre_rows = session.scalars(select(StationGenre.genre).where(StationGenre.station_id == station.id)).all()
        program_rows = session.scalars(
            select(StationProgram.name).where(StationProgram.station_id == station.id)
        ).all()
        contact_rows = session.scalars(
            select(StationContact.role).where(StationContact.station_id == station.id)
        ).all()
        evidence_rows = session.scalars(
            select(Evidence).where(Evidence.station_id == station.id).order_by(Evidence.created_at.desc()).limit(settings.profile_max_evidence_items)
        ).all()

        source_urls = [e.source_url for e in evidence_rows if e.source_url]
        domains = {re.sub(r"^www\.", "", re.sub(r"^https?://", "", (u or "")).split("/")[0].lower()) for u in source_urls}
        domains.discard("")
        source_count = len(source_urls)
        diversity_score = round(min(1.0, len(domains) / 8.0), 3)

        text_parts = [
            station.canonical_name or "",
            station.country_code or "",
            station.language or "",
            " ".join(genre_rows),
            " ".join(program_rows),
            " ".join(str(r.value) for r in contact_rows),
        ]
        for ev in evidence_rows:
            text_parts.append(ev.raw_title or "")
            text_parts.append(ev.raw_snippet or "")
            if ev.extracted_payload_json:
                text_parts.append(ev.extracted_payload_json[:4000])
        text_blob = "\n".join(text_parts)

        style_tags = _pick_tags(text_blob, STYLE_KEYWORDS)
        editorial_signals = _pick_tags(text_blob, EDITORIAL_KEYWORDS)

        role_counts = Counter(str(r.value) for r in contact_rows)
        top_roles = ", ".join([f"{role}:{count}" for role, count in role_counts.most_common(4)]) or "unknown"
        top_programs = ", ".join(program_rows[:6]) or "unknown"
        show_personality = f"Programs: {top_programs}"
        host_voice = f"Roles: {top_roles}"

        confidence = _confidence_from_counts(source_count, diversity_score, len(style_tags), len(editorial_signals))

        snapshot = StationProfileSnapshot(
            station_id=station.id,
            style_tags_json=json.dumps(sorted(set(style_tags))),
            editorial_signals_json=json.dumps(sorted(set(editorial_signals))),
            show_personality=show_personality,
            host_voice=host_voice,
            source_count=source_count,
            diversity_score=diversity_score,
            confidence=confidence,
        )
        session.add(snapshot)
        built += 1

    session.commit()
    return {"profiles_built": built, "stations_considered": len(stations)}

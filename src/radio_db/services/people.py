from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radio_db.config import settings
from radio_db.models.entities import (
    ContactRole,
    Evidence,
    SourceType,
    Station,
    StationContact,
    StationGenre,
    StationPerson,
    StationStatus,
)
from radio_db.services.source_search import collect_search_results

PREFERENCE_HINTS = {
    "electronic": ["electronic", "edm", "house", "techno", "dance", "dj set"],
    "indie": ["indie", "alternative", "new artist", "unsigned", "emerging"],
    "hip_hop": ["hip hop", "rap", "trap", "r&b"],
    "rock": ["rock", "metal", "punk", "guitar"],
    "pop": ["pop", "chart", "top 40", "mainstream"],
    "jazz": ["jazz", "soul", "blues"],
}

ROLE_HINTS = {
    "music_director": ["music director", "head of music"],
    "program_director": ["program director", "programme director"],
    "producer": ["producer"],
    "host": ["host", "presenter", "radio host"],
    "dj": ["dj", "deejay", "selector"],
    "editor": ["editor"],
}

PREFERENCE_PRIORITY_ROLES = (
    ContactRole.MUSIC_DIRECTOR,
    ContactRole.PROGRAM_DIRECTOR,
    ContactRole.PRODUCER,
    ContactRole.DJ,
    ContactRole.HOST,
)

GENERIC_STATION_TOKENS = {
    "radio",
    "station",
    "fm",
    "am",
    "music",
    "live",
    "online",
    "web",
}


def _infer_preferences(contact: dict, station_genres: list[str], notes: str | None = None) -> tuple[str | None, list[str], str | None]:
    text_parts = [
        str(contact.get("show_name") or ""),
        str(contact.get("notes") or ""),
        str(notes or ""),
        " ".join(station_genres),
    ]
    blob = " ".join(text_parts).lower()

    tags: list[str] = []
    for tag, needles in PREFERENCE_HINTS.items():
        if any(n in blob for n in needles):
            tags.append(tag)

    pref_summary = ", ".join(tags[:5]) if tags else None
    linkedin_url = None
    for token in re.findall(r"https?://[^\s)]+", blob):
        if "linkedin.com/" in token:
            linkedin_url = token
            break
    return pref_summary, sorted(set(tags)), linkedin_url


def _person_passes_quality_gate(
    *,
    role: ContactRole,
    name: str | None,
    email: str | None,
    linkedin_url: str | None,
    confidence: float,
) -> bool:
    if name:
        name_norm = name.strip().lower()
        if name_norm.startswith(("http://", "https://", "www.")):
            return False
        if "linkedin.com/in/" in name_norm or "/" in name_norm:
            return False
    if confidence < settings.people_min_confidence:
        return False
    if settings.people_require_name_or_email and not (name or email):
        return False
    if role == ContactRole.UNKNOWN and settings.people_require_known_role:
        if settings.people_allow_unknown_role_with_linkedin and linkedin_url:
            return True
        return False
    return True


def upsert_station_person(session: Session, station_id: int, contact: dict, station_genres: list[str]) -> None:
    role_str = (contact.get("role") or "unknown").lower()
    role = ContactRole(role_str) if role_str in {r.value for r in ContactRole} else ContactRole.UNKNOWN
    name = (contact.get("name") or "").strip() or None
    email = (contact.get("email") or "").strip() or None
    show_name = (contact.get("show_name") or "").strip() or None
    contact_url = contact.get("contact_url")
    linkedin_url = contact.get("linkedin_url")
    notes = contact.get("notes")
    confidence = float(contact.get("confidence", 0.4))
    prefs, genre_affinity, inferred_linkedin_url = _infer_preferences(contact, station_genres, notes=notes)
    linkedin_url = linkedin_url or inferred_linkedin_url
    if not _person_passes_quality_gate(
        role=role,
        name=name,
        email=email,
        linkedin_url=linkedin_url,
        confidence=confidence,
    ):
        return

    name_norm = (name or "").strip().lower()
    show_norm = (show_name or "").strip().lower()
    existing = None
    if name_norm or email:
        candidates = session.scalars(
            select(StationPerson).where(
                StationPerson.station_id == station_id,
                StationPerson.role == role,
            )
        ).all()
        for row in candidates:
            row_name_norm = (row.name or "").strip().lower()
            row_show_norm = (row.show_name or "").strip().lower()
            same_name = bool(name_norm and row_name_norm == name_norm)
            same_email = bool(email and row.email and row.email.strip().lower() == email.strip().lower())
            same_show = (show_norm == row_show_norm) or not show_norm or not row_show_norm
            if (same_name or same_email) and same_show:
                existing = row
                break
    if existing:
        existing.show_name = show_name or existing.show_name
        existing.contact_url = contact_url or existing.contact_url
        existing.linkedin_url = linkedin_url or existing.linkedin_url
        existing.musical_preferences = prefs or existing.musical_preferences
        existing.genre_affinities_json = json.dumps(genre_affinity or json.loads(existing.genre_affinities_json or "[]"))
        existing.notes = notes or existing.notes
        existing.source_count += 1
        existing.confidence = max(existing.confidence, confidence)
        existing.last_seen_at = datetime.utcnow()
        return

    session.add(
        StationPerson(
            station_id=station_id,
            name=name,
            role=role,
            show_name=show_name,
            email=email,
            contact_url=contact_url,
            linkedin_url=linkedin_url,
            musical_preferences=prefs,
            genre_affinities_json=json.dumps(genre_affinity),
            source_count=1,
            confidence=confidence,
            notes=notes,
            last_seen_at=datetime.utcnow(),
        )
    )


def build_station_people(session: Session, limit: int = 1000, station_id: int | None = None) -> dict:
    stmt = select(Station).order_by(Station.updated_at.desc()).limit(limit)
    if station_id is not None:
        stmt = select(Station).where(Station.id == station_id).limit(1)
    else:
        stmt = stmt.where(
            Station.status != StationStatus.REJECTED,
            Station.confidence_score >= settings.people_min_station_confidence,
        )
    stations = session.scalars(stmt).all()

    built = 0
    for station in stations:
        genres = session.scalars(select(StationGenre.genre).where(StationGenre.station_id == station.id)).all()
        contacts = session.scalars(select(StationContact).where(StationContact.station_id == station.id)).all()
        for c in contacts:
            upsert_station_person(
                session=session,
                station_id=station.id,
                station_genres=genres,
                contact={
                    "name": c.name,
                    "role": c.role.value,
                    "show_name": c.show_name,
                    "email": c.email,
                    "contact_url": c.contact_url,
                    "notes": c.notes,
                    "confidence": c.confidence,
                },
            )
            built += 1
    session.commit()
    return {"people_processed": built, "stations_considered": len(stations)}


def search_station_people(
    session: Session,
    q: str = "",
    role: str = "",
    station_query: str = "",
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[dict]:
    stmt = select(StationPerson, Station).join(Station, Station.id == StationPerson.station_id)
    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(func.coalesce(StationPerson.name, "")).like(needle),
                func.lower(func.coalesce(StationPerson.show_name, "")).like(needle),
                func.lower(func.coalesce(StationPerson.email, "")).like(needle),
                func.lower(func.coalesce(StationPerson.musical_preferences, "")).like(needle),
            )
        )
    if station_query:
        needle = f"%{station_query.lower()}%"
        stmt = stmt.where(func.lower(Station.canonical_name).like(needle))
    if role:
        try:
            stmt = stmt.where(StationPerson.role == ContactRole(role))
        except ValueError:
            pass
    if min_confidence > 0:
        stmt = stmt.where(StationPerson.confidence >= min_confidence)

    rows = session.execute(stmt.order_by(StationPerson.confidence.desc(), StationPerson.updated_at.desc()).limit(limit)).all()
    out: list[dict] = []
    for person, station in rows:
        out.append(
            {
                "person_id": person.id,
                "station_id": station.id,
                "station_name": station.canonical_name,
                "name": person.name,
                "role": person.role.value,
                "show_name": person.show_name,
                "email": person.email,
                "contact_url": person.contact_url,
                "linkedin_url": person.linkedin_url,
                "musical_preferences": person.musical_preferences,
                "genre_affinities": json.loads(person.genre_affinities_json or "[]"),
                "source_count": person.source_count,
                "confidence": person.confidence,
                "last_seen_at": person.last_seen_at.isoformat(),
            }
        )
    return out


def _infer_role(text: str) -> str:
    lowered = text.lower()
    for role, hints in ROLE_HINTS.items():
        if any(h in lowered for h in hints):
            return role
    return "unknown"


def _extract_name(title: str, station_name: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", title).strip()
    cleaned = re.sub(r"\|\s*linkedin.*$", "", cleaned, flags=re.I).strip()
    parts = re.split(r"\s[-|]\s", cleaned)
    candidate = parts[0].strip() if parts else cleaned
    candidate = re.sub(re.escape(station_name), "", candidate, flags=re.I).strip(" -|,")
    if not candidate:
        return None
    lower = candidate.lower()
    if lower.startswith(("http://", "https://", "www.")):
        return None
    if "/" in candidate or "%" in candidate:
        return None
    words = candidate.split()
    if 1 <= len(words) <= 5:
        return candidate
    return None


def _station_name_tokens(station_name: str) -> list[str]:
    parts = re.findall(r"[a-z0-9]+", (station_name or "").lower())
    return [p for p in parts if len(p) >= 4 and p not in GENERIC_STATION_TOKENS]


def _station_name_is_too_generic(station_name: str) -> bool:
    tokens = _station_name_tokens(station_name)
    return len(tokens) == 0


def _blob_has_station_token(blob: str, station_name: str) -> bool:
    tokens = _station_name_tokens(station_name)
    if not tokens:
        return False
    return any(tok in blob for tok in tokens)


def _preference_tags_from_search_text(text: str) -> list[str]:
    lowered = (text or "").lower()
    tags: list[str] = []
    for tag, needles in PREFERENCE_HINTS.items():
        if any(n in lowered for n in needles):
            tags.append(tag)
    return sorted(set(tags))


def enrich_people_preferences(
    session: Session,
    people_limit: int = 120,
    queries_per_person: int = 1,
    min_hits_for_update: int = 1,
) -> dict:
    people_rows = session.execute(
        select(StationPerson, Station)
        .join(Station, Station.id == StationPerson.station_id)
        .where(
            Station.status != StationStatus.REJECTED,
            Station.confidence_score >= settings.people_min_station_confidence,
            StationPerson.role.in_(PREFERENCE_PRIORITY_ROLES),
        )
        .order_by(StationPerson.confidence.desc(), StationPerson.updated_at.desc())
        .limit(people_limit)
    ).all()

    source_map = {
        "brave": SourceType.BRAVE_SEARCH,
        "google_cse": SourceType.GOOGLE_CSE,
        "tavily": SourceType.TAVILY,
        "grok": SourceType.GROK_SEARCH,
        "linkup": SourceType.LINKUP,
        "duckduckgo": SourceType.DUCKDUCKGO,
    }

    processed = 0
    updated = 0
    evidence_added = 0
    queries_executed = 0

    for person, station in people_rows:
        person_name = (person.name or "").strip()
        if not person_name:
            continue

        query_plan = [
            f'"{person_name}" "{station.canonical_name}" ("music taste" OR genre OR playlist OR influences OR "favorite music")',
            f'"{person_name}" "{person.show_name or station.canonical_name}" ("dj set" OR "new music" OR "artists" OR "radio show")',
        ][: max(1, min(2, queries_per_person))]

        hit_urls: set[str] = set()
        tag_counter: dict[str, int] = {}
        best_snippets: list[str] = []

        for q in query_plan:
            queries_executed += 1
            results, _calls = collect_search_results(
                query=q,
                country=station.country_code or None,
                include_linkup=False,
            )
            for item in results[:8]:
                url = (item.get("url") or "").strip()
                if not url or url in hit_urls:
                    continue
                title = (item.get("title") or "").strip()
                snippet = (item.get("description") or "").strip()
                blob = f"{title}\n{snippet}".lower()
                if person_name.lower() not in blob and station.canonical_name.lower() not in blob:
                    continue

                tags = _preference_tags_from_search_text(blob)
                if not tags:
                    continue
                hit_urls.add(url)
                for t in tags:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
                if snippet:
                    best_snippets.append(snippet[:220])

                session.add(
                    Evidence(
                        station_id=station.id,
                        source_type=source_map.get(str(item.get("source")), SourceType.BRAVE_SEARCH),
                        source_url=url,
                        source_id="people_preference_enrich",
                        raw_title=title,
                        raw_snippet=snippet[:1200],
                        extracted_payload_json=json.dumps(
                            {
                                "person_id": person.id,
                                "person_name": person_name,
                                "tags": tags,
                                "query": q,
                            }
                        ),
                        confidence=min(0.85, 0.45 + (0.1 * len(tags))),
                    )
                )
                evidence_added += 1

        processed += 1
        if len(hit_urls) < max(1, min_hits_for_update) or not tag_counter:
            continue

        ranked_tags = sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))
        new_tags = [t for t, _c in ranked_tags[:5]]
        try:
            existing_tags = json.loads(person.genre_affinities_json or "[]")
        except Exception:
            existing_tags = []
        merged_tags = sorted(set(existing_tags + new_tags))
        person.genre_affinities_json = json.dumps(merged_tags)
        person.musical_preferences = ", ".join(merged_tags[:6]) if merged_tags else person.musical_preferences
        snippets = " | ".join(best_snippets[:2])
        note_line = f"prefs-enrich tags={','.join(new_tags)} hits={len(hit_urls)}"
        if snippets:
            note_line = f"{note_line}; sample={snippets}"
        person.notes = f"{(person.notes or '')}\n{note_line}".strip()
        person.source_count = max(1, person.source_count) + len(hit_urls)
        person.confidence = min(0.98, max(person.confidence, 0.55 + min(0.25, 0.05 * len(merged_tags))))
        person.last_seen_at = datetime.utcnow()
        updated += 1

    session.commit()
    return {
        "people_processed": processed,
        "people_updated": updated,
        "queries_executed": queries_executed,
        "evidence_added": evidence_added,
    }


def discover_people_from_search(
    session: Session,
    station_limit: int = 200,
    queries_per_station: int = 2,
    min_confidence: float = 0.35,
) -> dict:
    stations = session.scalars(
        select(Station)
        .where(
            Station.website_url.is_not(None),
            Station.status != StationStatus.REJECTED,
            Station.confidence_score >= settings.people_discovery_min_station_confidence,
        )
        .order_by(Station.updated_at.desc())
        .limit(station_limit)
    ).all()

    source_map = {
        "brave": SourceType.BRAVE_SEARCH,
        "google_cse": SourceType.GOOGLE_CSE,
        "tavily": SourceType.TAVILY,
        "linkup": SourceType.LINKUP,
        "duckduckgo": SourceType.DUCKDUCKGO,
    }

    people_upserted = 0
    evidence_added = 0
    searched_queries = 0

    for st in stations:
        genres = session.scalars(select(StationGenre.genre).where(StationGenre.station_id == st.id)).all()
        station_domain = (urlparse(st.website_url or "").netloc or "").lower().replace("www.", "")
        generic_station = _station_name_is_too_generic(st.canonical_name)
        query_plan: list[str] = []
        if not generic_station:
            query_plan.append(f'"{st.canonical_name}" ("music director" OR "program director" OR dj OR host) site:linkedin.com/in')
        if station_domain:
            query_plan.append(
                f'site:{station_domain} ("team" OR "staff" OR "about" OR "contact" OR "host" OR "dj" OR "music director" OR "program director")'
            )
        query_plan.append(f'"{st.canonical_name}" ("radio host" OR presenter OR "on air" OR show)')
        query_plan = query_plan[: max(1, min(3, queries_per_station))]

        for q in query_plan:
            searched_queries += 1
            results, _calls = collect_search_results(query=q, country=st.country_code or None)
            for item in results:
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                title = item.get("title", "") or ""
                snippet = item.get("description", "") or ""
                blob = f"{title}\n{snippet}".lower()
                result_domain = (urlparse(url).netloc or "").lower().replace("www.", "")
                on_station_domain = bool(station_domain and (result_domain == station_domain or result_domain.endswith(f".{station_domain}")))
                # Off-domain hits are only accepted when they explicitly mention station-specific tokens.
                if not on_station_domain and not _blob_has_station_token(blob, st.canonical_name):
                    continue
                role = _infer_role(f"{title}\n{snippet}")
                name = _extract_name(title, st.canonical_name)
                email_match = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", snippet or "", flags=re.I)
                email = email_match[0].lower() if email_match else None
                linkedin_url = url if "linkedin.com/in/" in url.lower() else None

                confidence = 0.55 if linkedin_url else 0.42
                if not on_station_domain and linkedin_url:
                    confidence -= 0.10
                if role == "unknown":
                    confidence -= 0.1
                if not name and not email and role == "unknown":
                    continue
                if confidence < min_confidence:
                    continue
                if not _person_passes_quality_gate(
                    role=ContactRole(role) if role in {r.value for r in ContactRole} else ContactRole.UNKNOWN,
                    name=name,
                    email=email,
                    linkedin_url=linkedin_url,
                    confidence=confidence,
                ):
                    continue

                upsert_station_person(
                    session=session,
                    station_id=st.id,
                    station_genres=genres,
                    contact={
                        "name": name,
                        "role": role,
                        "show_name": None,
                        "email": email,
                        "contact_url": url,
                        "linkedin_url": linkedin_url,
                        "notes": f"people-discovery query={q}; snippet={snippet[:400]}",
                        "confidence": confidence,
                    },
                )
                people_upserted += 1

                session.add(
                    Evidence(
                        station_id=st.id,
                        source_type=source_map.get(str(item.get("source")), SourceType.BRAVE_SEARCH),
                        source_url=url,
                        source_id="people_discovery",
                        raw_title=title,
                        raw_snippet=snippet[:1500],
                        extracted_payload_json=json.dumps(
                            {
                                "person_name": name,
                                "role": role,
                                "email": email,
                                "linkedin_url": linkedin_url,
                                "query": q,
                            }
                        ),
                        confidence=confidence,
                    )
                )
                evidence_added += 1
    session.commit()
    return {
        "stations_processed": len(stations),
        "queries_executed": searched_queries,
        "people_upserted": people_upserted,
        "evidence_added": evidence_added,
    }


def cleanup_people_quality(session: Session, limit: int = 500000, dry_run: bool = True) -> dict:
    rows = session.execute(
        select(StationPerson, Station).join(Station, Station.id == StationPerson.station_id).limit(limit)
    ).all()
    checked = len(rows)
    remove_ids: list[int] = []

    for person, station in rows:
        role = person.role
        if station.status == StationStatus.REJECTED or station.confidence_score < settings.people_min_station_confidence:
            remove_ids.append(person.id)
            continue
        if not _person_passes_quality_gate(
            role=role,
            name=person.name,
            email=person.email,
            linkedin_url=person.linkedin_url,
            confidence=person.confidence,
        ):
            remove_ids.append(person.id)

    if not dry_run and remove_ids:
        session.query(StationPerson).where(StationPerson.id.in_(remove_ids)).delete(synchronize_session=False)
        session.commit()

    return {"checked": checked, "to_remove": len(remove_ids), "dry_run": dry_run}

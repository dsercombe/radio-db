from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from radio_db.db import SessionLocal
from radio_db.models.entities import ContactRole, Station, StationContact, StationPerson

router = APIRouter(prefix="/api/v1/contacts", tags=["contacts"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _enum_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    return str(value or "")


def _parse_json(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _station_name(db: Session, station_id: int) -> str:
    station_name = db.scalar(select(Station.canonical_name).where(Station.id == station_id))
    if station_name is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    return str(station_name)


class ContactDTO(BaseModel):
    id: int
    station_id: int
    station_name: str
    name: str | None = None
    role: str
    show_name: str | None = None
    email: str | None = None
    contact_url: str | None = None
    notes: str | None = None
    confidence: float


class ContactUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    role: Literal["host", "dj", "producer", "music_director", "program_director", "editor", "unknown"] | None = None
    show_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    contact_url: str | None = Field(default=None, max_length=1024)
    notes: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PersonDTO(BaseModel):
    id: int
    station_id: int
    station_name: str
    name: str | None = None
    role: str
    show_name: str | None = None
    email: str | None = None
    contact_url: str | None = None
    linkedin_url: str | None = None
    musical_preferences: str | None = None
    genre_affinities: list[str] = Field(default_factory=list)
    source_count: int
    confidence: float
    notes: str | None = None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class PersonUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    role: Literal["host", "dj", "producer", "music_director", "program_director", "editor", "unknown"] | None = None
    show_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    contact_url: str | None = Field(default=None, max_length=1024)
    linkedin_url: str | None = Field(default=None, max_length=1024)
    musical_preferences: str | None = None
    genre_affinities: list[str] | None = None
    source_count: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: str | None = None
    last_seen_at: datetime | None = None


def _serialize_contact(contact: StationContact, station_name: str) -> ContactDTO:
    return ContactDTO(
        id=contact.id,
        station_id=contact.station_id,
        station_name=station_name,
        name=contact.name,
        role=_enum_value(contact.role),
        show_name=contact.show_name,
        email=contact.email,
        contact_url=contact.contact_url,
        notes=contact.notes,
        confidence=float(contact.confidence or 0.0),
    )


def _serialize_person(person: StationPerson, station_name: str) -> PersonDTO:
    return PersonDTO(
        id=person.id,
        station_id=person.station_id,
        station_name=station_name,
        name=person.name,
        role=_enum_value(person.role),
        show_name=person.show_name,
        email=person.email,
        contact_url=person.contact_url,
        linkedin_url=person.linkedin_url,
        musical_preferences=person.musical_preferences,
        genre_affinities=list(_parse_json(person.genre_affinities_json, [])),
        source_count=int(person.source_count or 0),
        confidence=float(person.confidence or 0.0),
        notes=person.notes,
        last_seen_at=person.last_seen_at,
        created_at=person.created_at,
        updated_at=person.updated_at,
    )


@router.get("/station/{station_id}")
def list_station_contacts(station_id: int, db: Session = Depends(_get_db)) -> dict:
    station_name = _station_name(db, station_id)
    contacts = db.scalars(
        select(StationContact).where(StationContact.station_id == station_id).order_by(StationContact.confidence.desc())
    ).all()
    people = db.scalars(
        select(StationPerson).where(StationPerson.station_id == station_id).order_by(StationPerson.confidence.desc())
    ).all()
    return {
        "station_id": station_id,
        "station_name": station_name,
        "contacts": [_serialize_contact(contact, station_name).model_dump() for contact in contacts],
        "people": [_serialize_person(person, station_name).model_dump() for person in people],
    }


@router.patch("/contact/{contact_id}", response_model=ContactDTO)
def update_contact(contact_id: int, request: ContactUpdateRequest, db: Session = Depends(_get_db)) -> ContactDTO:
    contact = db.scalar(select(StationContact).where(StationContact.id == contact_id))
    if contact is None:
        raise HTTPException(status_code=404, detail="contact_not_found")

    if request.name is not None:
        contact.name = request.name
    if request.role is not None:
        contact.role = ContactRole(request.role)
    if request.show_name is not None:
        contact.show_name = request.show_name
    if request.email is not None:
        contact.email = request.email
    if request.contact_url is not None:
        contact.contact_url = request.contact_url
    if request.notes is not None:
        contact.notes = request.notes
    if request.confidence is not None:
        contact.confidence = request.confidence

    db.commit()
    station_name = _station_name(db, contact.station_id)
    return _serialize_contact(contact, station_name)


@router.patch("/person/{person_id}", response_model=PersonDTO)
def update_person(person_id: int, request: PersonUpdateRequest, db: Session = Depends(_get_db)) -> PersonDTO:
    person = db.scalar(select(StationPerson).where(StationPerson.id == person_id))
    if person is None:
        raise HTTPException(status_code=404, detail="person_not_found")

    if request.name is not None:
        person.name = request.name
    if request.role is not None:
        person.role = ContactRole(request.role)
    if request.show_name is not None:
        person.show_name = request.show_name
    if request.email is not None:
        person.email = request.email
    if request.contact_url is not None:
        person.contact_url = request.contact_url
    if request.linkedin_url is not None:
        person.linkedin_url = request.linkedin_url
    if request.musical_preferences is not None:
        person.musical_preferences = request.musical_preferences
    if request.genre_affinities is not None:
        person.genre_affinities_json = json.dumps(request.genre_affinities, ensure_ascii=False)
    if request.source_count is not None:
        person.source_count = request.source_count
    if request.confidence is not None:
        person.confidence = request.confidence
    if request.notes is not None:
        person.notes = request.notes
    if request.last_seen_at is not None:
        person.last_seen_at = request.last_seen_at

    db.commit()
    station_name = _station_name(db, person.station_id)
    return _serialize_person(person, station_name)
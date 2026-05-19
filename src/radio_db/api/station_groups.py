from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radio_db.api.common import require_permission_mode
from radio_db.db import SessionLocal
from radio_db.models.entities import Station, StationGroup, StationGroupMembership

router = APIRouter(prefix="/api/v1/station-groups", tags=["station-groups"])


def _get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class StationGroupStationDTO(BaseModel):
    station_id: int
    canonical_name: str
    country_code: str = ""
    city: str | None = None
    status: str
    confidence_score: float
    website_url: str | None = None
    added_at: datetime
    note: str | None = None


class StationGroupListItem(BaseModel):
    id: int
    name: str
    description: str | None = None
    artist_key: str | None = None
    color_hint: str | None = None
    is_active: bool
    station_count: int = 0
    created_at: datetime
    updated_at: datetime


class StationGroupDetailResponse(StationGroupListItem):
    stations: list[StationGroupStationDTO] = Field(default_factory=list)


class StationGroupListResponse(BaseModel):
    items: list[StationGroupListItem] = Field(default_factory=list)


class StationGroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    artist_key: str | None = Field(default=None, max_length=255)
    color_hint: str | None = Field(default=None, max_length=32)


class StationGroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    artist_key: str | None = Field(default=None, max_length=255)
    color_hint: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None


class StationGroupMembershipCreateRequest(BaseModel):
    station_ids: list[int] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=4000)


class StationGroupMembershipResponse(BaseModel):
    group_id: int
    station_id: int
    created: bool


class StationMembershipListResponse(BaseModel):
    items: list[StationGroupListItem] = Field(default_factory=list)


def _serialize_group(group: StationGroup, station_count: int) -> StationGroupListItem:
    return StationGroupListItem(
        id=group.id,
        name=group.name,
        description=group.description,
        artist_key=group.artist_key,
        color_hint=group.color_hint,
        is_active=bool(group.is_active),
        station_count=int(station_count),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@router.get("", response_model=StationGroupListResponse)
def list_station_groups(db: Session = Depends(_get_db)) -> StationGroupListResponse:
    rows = db.execute(
        select(StationGroup, func.count(StationGroupMembership.id))
        .outerjoin(StationGroupMembership, StationGroupMembership.group_id == StationGroup.id)
        .group_by(StationGroup.id)
        .order_by(StationGroup.updated_at.desc(), StationGroup.id.desc())
    ).all()
    return StationGroupListResponse(items=[_serialize_group(group, count) for group, count in rows])


@router.post("", response_model=StationGroupDetailResponse)
def create_station_group(
    request: StationGroupCreateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationGroupDetailResponse:
    existing = db.scalar(select(StationGroup).where(func.lower(StationGroup.name) == request.name.strip().lower()))
    if existing is not None:
      raise HTTPException(status_code=409, detail="station_group_name_exists")
    group = StationGroup(
        name=request.name.strip(),
        description=(request.description or "").strip() or None,
        artist_key=(request.artist_key or "").strip() or None,
        color_hint=(request.color_hint or "").strip() or None,
        is_active=True,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return StationGroupDetailResponse(**_serialize_group(group, 0).model_dump(), stations=[])


@router.get("/{group_id}", response_model=StationGroupDetailResponse)
def get_station_group(group_id: int, db: Session = Depends(_get_db)) -> StationGroupDetailResponse:
    group = db.scalar(select(StationGroup).where(StationGroup.id == group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="station_group_not_found")

    station_rows = db.execute(
        select(StationGroupMembership, Station)
        .join(Station, Station.id == StationGroupMembership.station_id)
        .where(StationGroupMembership.group_id == group_id)
        .order_by(StationGroupMembership.created_at.desc(), Station.id.desc())
    ).all()

    return StationGroupDetailResponse(
        **_serialize_group(group, len(station_rows)).model_dump(),
        stations=[
            StationGroupStationDTO(
                station_id=station.id,
                canonical_name=station.canonical_name,
                country_code=station.country_code or "",
                city=station.city,
                status=str(getattr(station.status, "value", station.status)),
                confidence_score=float(station.confidence_score or 0.0),
                website_url=station.website_url,
                added_at=membership.created_at,
                note=membership.note,
            )
            for membership, station in station_rows
        ],
    )


@router.patch("/{group_id}", response_model=StationGroupListItem)
def update_station_group(
    group_id: int,
    request: StationGroupUpdateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationGroupListItem:
    group = db.scalar(select(StationGroup).where(StationGroup.id == group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="station_group_not_found")

    if request.name is not None:
        candidate_name = request.name.strip()
        if not candidate_name:
            raise HTTPException(status_code=422, detail="station_group_name_required")
        existing = db.scalar(
            select(StationGroup).where(
                func.lower(StationGroup.name) == candidate_name.lower(),
                StationGroup.id != group_id,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="station_group_name_exists")
        group.name = candidate_name
    group.description = request.description.strip() if request.description is not None and request.description.strip() else None
    group.artist_key = request.artist_key.strip() if request.artist_key is not None and request.artist_key.strip() else None
    group.color_hint = request.color_hint.strip() if request.color_hint is not None and request.color_hint.strip() else None
    if request.is_active is not None:
        group.is_active = bool(request.is_active)
    db.add(group)
    db.commit()
    db.refresh(group)
    station_count = db.scalar(select(func.count(StationGroupMembership.id)).where(StationGroupMembership.group_id == group.id)) or 0
    return _serialize_group(group, int(station_count))


@router.post("/{group_id}/stations", response_model=list[StationGroupMembershipResponse])
def add_stations_to_group(
    group_id: int,
    request: StationGroupMembershipCreateRequest,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> list[StationGroupMembershipResponse]:
    group = db.scalar(select(StationGroup).where(StationGroup.id == group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="station_group_not_found")

    station_ids = sorted({int(station_id) for station_id in request.station_ids if int(station_id) > 0})
    if not station_ids:
        raise HTTPException(status_code=422, detail="station_ids_required")

    existing_station_ids = set(
        db.scalars(select(Station.id).where(Station.id.in_(station_ids))).all()
    )
    if len(existing_station_ids) != len(station_ids):
        raise HTTPException(status_code=404, detail="station_not_found")

    current_memberships = set(
        db.scalars(
            select(StationGroupMembership.station_id).where(
                StationGroupMembership.group_id == group_id,
                StationGroupMembership.station_id.in_(station_ids),
            )
        ).all()
    )

    created_ids: list[int] = []
    for station_id in station_ids:
        if station_id in current_memberships:
            continue
        membership = StationGroupMembership(
            group_id=group_id,
            station_id=station_id,
            note=(request.note or "").strip() or None,
        )
        db.add(membership)
        created_ids.append(station_id)

    db.commit()
    return [
        StationGroupMembershipResponse(
            group_id=group_id,
            station_id=station_id,
            created=station_id in created_ids,
        )
        for station_id in station_ids
    ]


@router.get("/stations/{station_id}", response_model=StationMembershipListResponse)
def list_station_memberships(station_id: int, db: Session = Depends(_get_db)) -> StationMembershipListResponse:
    station = db.scalar(select(Station.id).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    rows = db.execute(
        select(StationGroup, func.count(StationGroupMembership.id))
        .join(StationGroupMembership, StationGroupMembership.group_id == StationGroup.id)
        .where(StationGroupMembership.station_id == station_id)
        .group_by(StationGroup.id)
        .order_by(StationGroup.name.asc())
    ).all()
    return StationMembershipListResponse(items=[_serialize_group(group, count) for group, count in rows])


@router.post("/stations/{station_id}/{group_id}", response_model=StationGroupMembershipResponse)
def add_station_to_group(
    station_id: int,
    group_id: int,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationGroupMembershipResponse:
    station = db.scalar(select(Station.id).where(Station.id == station_id))
    if station is None:
        raise HTTPException(status_code=404, detail="station_not_found")
    group = db.scalar(select(StationGroup.id).where(StationGroup.id == group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="station_group_not_found")
    existing = db.scalar(
        select(StationGroupMembership.id).where(
            StationGroupMembership.station_id == station_id,
            StationGroupMembership.group_id == group_id,
        )
    )
    if existing is not None:
        return StationGroupMembershipResponse(group_id=group_id, station_id=station_id, created=False)

    db.add(StationGroupMembership(group_id=group_id, station_id=station_id))
    db.commit()
    return StationGroupMembershipResponse(group_id=group_id, station_id=station_id, created=True)


@router.delete("/stations/{station_id}/{group_id}", response_model=StationGroupMembershipResponse)
def remove_station_from_group(
    station_id: int,
    group_id: int,
    db: Session = Depends(_get_db),
    _mode: str = Depends(require_permission_mode("execute")),
) -> StationGroupMembershipResponse:
    membership = db.scalar(
        select(StationGroupMembership).where(
            StationGroupMembership.station_id == station_id,
            StationGroupMembership.group_id == group_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="station_group_membership_not_found")
    db.delete(membership)
    db.commit()
    return StationGroupMembershipResponse(group_id=group_id, station_id=station_id, created=False)

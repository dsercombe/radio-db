from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from radio_db.api.common import require_permission_mode
from radio_db.services.browser import (
    BrowserSession,
    PlaywrightError,
    close_browser_session,
    get_browser_session,
    get_browser_session_state,
    list_browser_session_events,
    list_browser_sessions,
    navigate_browser_session,
    open_browser_session,
    perform_browser_action,
    snapshot_browser_session,
)

router = APIRouter(prefix="/api/v1/browser", tags=["browser"])


class BrowserSessionCreateRequest(BaseModel):
    start_url: str | None = Field(default=None, max_length=2048)
    station_id: int | None = Field(default=None, ge=1)
    run_id: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=200)


class BrowserSessionNavigateRequest(BaseModel):
    url: str = Field(max_length=2048)


class BrowserSessionActionRequest(BaseModel):
    action: Literal["click", "fill", "press", "select", "goto", "wait"]
    selector: str | None = Field(default=None, max_length=1000)
    value: str | None = Field(default=None, max_length=4000)


class BrowserSessionStateResponse(BaseModel):
    id: str
    station_id: int | None = None
    run_id: int | None = None
    url: str
    title: str
    status: str
    last_error: str
    screenshot_path: str | None = None
    html_path: str | None = None
    action_count: int = 0
    actions: list[dict] = Field(default_factory=list)
    timeline: list[dict] = Field(default_factory=list)
    event_count: int = 0
    metadata: dict = Field(default_factory=dict)
    created_at: str
    updated_at: str


class BrowserSessionListResponse(BaseModel):
    items: list[BrowserSessionStateResponse]


class BrowserSessionEventResponse(BaseModel):
    id: int
    type: str
    payload: dict = Field(default_factory=dict)
    at: str
    url: str
    status: str
    title: str


class BrowserSessionTimelineResponse(BaseModel):
    session_id: str
    event_count: int
    items: list[BrowserSessionEventResponse]


def _serialize(session: BrowserSession) -> BrowserSessionStateResponse:
    payload = session.to_dict()
    return BrowserSessionStateResponse(**payload)


def _serialize_event(item: dict) -> BrowserSessionEventResponse:
    return BrowserSessionEventResponse(**item)


@router.get("/sessions", response_model=BrowserSessionListResponse)
def list_sessions() -> BrowserSessionListResponse:
    return BrowserSessionListResponse(items=[BrowserSessionStateResponse(**item) for item in list_browser_sessions()])


@router.post("/sessions", response_model=BrowserSessionStateResponse)
def create_session(
    request: BrowserSessionCreateRequest,
    _mode: str = Depends(require_permission_mode("dry-run")),
) -> BrowserSessionStateResponse:
    try:
        session = open_browser_session(
            start_url=request.start_url,
            station_id=request.station_id,
            run_id=request.run_id,
            metadata={"label": request.label} if request.label else {},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _serialize(session)


@router.get("/sessions/{session_id}", response_model=BrowserSessionStateResponse)
def get_session(session_id: str) -> BrowserSessionStateResponse:
    try:
        return BrowserSessionStateResponse(**get_browser_session_state(session_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/navigate", response_model=BrowserSessionStateResponse)
def navigate(
    session_id: str,
    request: BrowserSessionNavigateRequest,
    _mode: str = Depends(require_permission_mode("dry-run")),
) -> BrowserSessionStateResponse:
    try:
        return _serialize(navigate_browser_session(session_id, request.url))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/action", response_model=BrowserSessionStateResponse)
def action(
    session_id: str,
    request: BrowserSessionActionRequest,
    _mode: str = Depends(require_permission_mode("dry-run")),
) -> BrowserSessionStateResponse:
    try:
        return _serialize(perform_browser_action(session_id, request.action, request.selector, request.value))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/snapshot", response_model=BrowserSessionStateResponse)
def snapshot(
    session_id: str,
    _mode: str = Depends(require_permission_mode("dry-run")),
) -> BrowserSessionStateResponse:
    try:
        return _serialize(snapshot_browser_session(session_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/events", response_model=BrowserSessionTimelineResponse)
def session_events(session_id: str, limit: int = 50) -> BrowserSessionTimelineResponse:
    try:
        event_count, items = list_browser_session_events(session_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    return BrowserSessionTimelineResponse(
        session_id=session_id,
        event_count=event_count,
        items=[_serialize_event(item) for item in items],
    )


@router.get("/sessions/{session_id}/stream")
def stream_session_events(session_id: str) -> StreamingResponse:
    try:
        get_browser_session_state(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc

    def _event_stream():
        last_event_id = 0
        while True:
            try:
                session = get_browser_session(session_id)
                new_events = [event for event in session.timeline if int(event.get("id", 0)) > last_event_id]
                for event in new_events:
                    last_event_id = int(event.get("id", last_event_id))
                    yield f"event: browser-session\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                time.sleep(1.0)
                continue
            except KeyError:
                try:
                    _, items = list_browser_session_events(session_id, limit=500)
                except KeyError:
                    yield "event: closed\ndata: {\"closed\": true}\n\n"
                    break
                for event in items:
                    event_id = int(event.get("id", 0))
                    if event_id <= last_event_id:
                        continue
                    last_event_id = event_id
                    yield f"event: browser-session\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield "event: closed\ndata: {\"closed\": true}\n\n"
                break

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.delete("/sessions/{session_id}")
def stop_session(
    session_id: str,
    _mode: str = Depends(require_permission_mode("dry-run")),
) -> dict:
    try:
        close_browser_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser_session_not_found") from exc
    return {"deleted": True, "session_id": session_id}

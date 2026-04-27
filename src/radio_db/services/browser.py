from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radio_db.config import settings

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency in some environments
    PlaywrightError = Exception  # type: ignore[assignment]
    sync_playwright = None


_LOCK = threading.Lock()
_SESSIONS: dict[str, "BrowserSession"] = {}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _session_dir(session_id: str) -> Path:
    return Path(settings.browser_snapshot_dir).resolve() / "browser_sessions" / session_id


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _readable_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[-2000:]


def _record_event(session: BrowserSession, event_type: str, payload: dict[str, Any] | None = None) -> None:
    session.timeline.append(
        {
            "id": len(session.timeline) + 1,
            "type": event_type,
            "payload": payload or {},
            "at": _now().isoformat(),
            "url": session.url,
            "status": session.status,
            "title": session.title,
        }
    )


@dataclass
class BrowserSession:
    id: str
    created_at: datetime
    updated_at: datetime
    station_id: int | None = None
    run_id: int | None = None
    url: str = "about:blank"
    title: str = ""
    status: str = "idle"
    last_error: str = ""
    screenshot_path: str | None = None
    html_path: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _browser: Any = field(default=None, repr=False)
    _context: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "run_id": self.run_id,
            "url": self.url,
            "title": self.title,
            "status": self.status,
            "last_error": self.last_error,
            "screenshot_path": self.screenshot_path,
            "html_path": self.html_path,
            "action_count": len(self.actions),
            "actions": list(self.actions[-20:]),
            "timeline": list(self.timeline[-50:]),
            "event_count": len(self.timeline),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _ensure_playwright() -> None:
    if sync_playwright is None:
        raise RuntimeError("playwright_not_installed")


def _persist_snapshot(session: BrowserSession, page: Any, label: str = "snapshot") -> None:
    session_dir = _session_dir(session.id)
    session_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    html_path = session_dir / f"{label}-{timestamp}.html"
    screenshot_path = session_dir / f"{label}-{timestamp}.png"

    try:
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
        session.html_path = str(html_path)
    except Exception:
        session.last_error = session.last_error or "html_snapshot_failed"

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        session.screenshot_path = str(screenshot_path)
    except Exception:
        session.last_error = session.last_error or "screenshot_failed"

    _record_event(session, "snapshot", {"label": label, "html_path": session.html_path, "screenshot_path": session.screenshot_path})


def _update_state(session: BrowserSession, page: Any) -> BrowserSession:
    try:
        session.url = page.url or session.url
    except Exception:
        pass
    try:
        session.title = page.title() or session.title
    except Exception:
        pass
    session.updated_at = _now()
    _persist_snapshot(session, page)
    return session


def _attach_page(session: BrowserSession, page: Any) -> None:
    session._page = page


def open_browser_session(start_url: str | None = None, station_id: int | None = None, run_id: int | None = None, metadata: dict[str, Any] | None = None) -> BrowserSession:
    _ensure_playwright()
    session_id = uuid.uuid4().hex
    payload = metadata or {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.browser_headless)
        context = browser.new_context(viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        if start_url:
            page.goto(start_url, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
        session = BrowserSession(
            id=session_id,
            created_at=_now(),
            updated_at=_now(),
            station_id=station_id,
            run_id=run_id,
            url=page.url or (start_url or "about:blank"),
            title=page.title() if start_url else "",
            status="idle",
            metadata=payload,
            _browser=browser,
            _context=context,
            _page=page,
        )
        _persist_snapshot(session, page, label="opened")
        _record_event(session, "opened", {"start_url": start_url, "station_id": station_id, "run_id": run_id})
        with _LOCK:
            _SESSIONS[session.id] = session
        return session


def list_browser_sessions() -> list[dict[str, Any]]:
    with _LOCK:
        return [session.to_dict() for session in sorted(_SESSIONS.values(), key=lambda item: item.updated_at, reverse=True)]


def get_browser_session(session_id: str) -> BrowserSession:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError(session_id)
    return session


def navigate_browser_session(session_id: str, url: str) -> BrowserSession:
    session = get_browser_session(session_id)
    try:
        session.status = "navigating"
        _record_event(session, "navigate_started", {"url": url})
        session._page.goto(url, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
        session.status = "idle"
        session.last_error = ""
    except Exception as exc:
        session.status = "error"
        session.last_error = _readable_error(exc)
        _record_event(session, "navigate_failed", {"url": url, "error": session.last_error})
    finally:
        _update_state(session, session._page)
        session.actions.append({"type": "navigate", "url": url, "at": _now().isoformat()})
        _record_event(session, "navigate_completed", {"url": url, "status": session.status})
    return session


def perform_browser_action(session_id: str, action: str, selector: str | None = None, value: str | None = None) -> BrowserSession:
    session = get_browser_session(session_id)
    page = session._page
    try:
        session.status = "working"
        _record_event(session, "action_started", {"action": action, "selector": selector, "value": value})
        if action == "click":
            if not selector:
                raise ValueError("selector_required")
            page.locator(selector).first.click(timeout=settings.browser_timeout_ms)
        elif action == "fill":
            if not selector:
                raise ValueError("selector_required")
            page.locator(selector).first.fill(value or "", timeout=settings.browser_timeout_ms)
        elif action == "press":
            if not selector:
                raise ValueError("selector_required")
            page.locator(selector).first.press(value or "Enter", timeout=settings.browser_timeout_ms)
        elif action == "goto":
            if not value:
                raise ValueError("url_required")
            page.goto(value, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
        elif action == "wait":
            page.wait_for_timeout(int(float(value or "1500")))
        elif action == "select":
            if not selector:
                raise ValueError("selector_required")
            page.locator(selector).first.select_option(value or "", timeout=settings.browser_timeout_ms)
        else:
            raise ValueError(f"unknown_action:{action}")
        session.status = "idle"
        session.last_error = ""
    except Exception as exc:
        session.status = "error"
        session.last_error = _readable_error(exc)
        _record_event(session, "action_failed", {"action": action, "selector": selector, "value": value, "error": session.last_error})
    finally:
        _update_state(session, page)
        session.actions.append({"type": action, "selector": selector, "value": value, "at": _now().isoformat()})
        _record_event(session, "action_completed", {"action": action, "selector": selector, "value": value, "status": session.status})
    return session


def snapshot_browser_session(session_id: str) -> BrowserSession:
    session = get_browser_session(session_id)
    _record_event(session, "snapshot_requested", {})
    return _update_state(session, session._page)


def close_browser_session(session_id: str) -> None:
    with _LOCK:
        session = _SESSIONS.pop(session_id, None)
    if session is None:
        raise KeyError(session_id)
    try:
        _record_event(session, "closing", {})
        session._context.close()
    except Exception:
        pass
    try:
        session._browser.close()
    except Exception:
        pass

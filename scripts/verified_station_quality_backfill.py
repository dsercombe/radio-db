from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from radio_db.db import SessionLocal
from radio_db.models.entities import Station, StationStatus
from radio_db.services.station_quality import run_station_quality_verification


STATE_PATH = Path(".radio_db_state/verified_station_quality_backfill_state.json")
PROVIDER = "gemini"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    session = SessionLocal()
    state: dict = {}
    try:
        station_rows = session.execute(
            select(Station.id, Station.canonical_name)
            .where(Station.status == StationStatus.VERIFIED, Station.website_url.is_not(None))
            .order_by(Station.id.asc())
        ).all()

        state = {
            "started_at": _utc_now(),
            "updated_at": _utc_now(),
            "finished_at": None,
            "provider": PROVIDER,
            "mode": "verified_full_reassessment",
            "total": len(station_rows),
            "processed": 0,
            "saved": 0,
            "errors": 0,
            "skipped_budget": 0,
            "last_station_id": None,
            "last_station_name": None,
            "decisions": {"promote": 0, "review": 0, "reject": 0},
            "samples": [],
            "status": "running",
        }
        _save_state(state)

        for station_id, station_name in station_rows:
            result = run_station_quality_verification(
                session=session,
                limit=1,
                station_id=int(station_id),
                provider=PROVIDER,
                only_unassessed=False,
                apply=True,
            )
            state["processed"] += int(result.get("processed", 0) or 0)
            state["saved"] += int(result.get("saved", 0) or 0)
            state["errors"] += int(result.get("errors", 0) or 0)
            state["skipped_budget"] += int(result.get("skipped_budget", 0) or 0)
            state["last_station_id"] = int(station_id)
            state["last_station_name"] = station_name

            for key, value in (result.get("decisions") or {}).items():
                if key in state["decisions"]:
                    state["decisions"][key] += int(value or 0)

            if result.get("samples"):
                sample = dict(result["samples"][0])
                sample["station_name"] = station_name
                state["samples"] = (state["samples"] + [sample])[-20:]

            state["updated_at"] = _utc_now()
            _save_state(state)

        state["status"] = "completed"
        state["finished_at"] = _utc_now()
        state["updated_at"] = state["finished_at"]
        _save_state(state)
        return 0
    except Exception as exc:
        state = state or {}
        state["status"] = "failed"
        state["finished_at"] = _utc_now()
        state["updated_at"] = state["finished_at"]
        state["fatal_error"] = str(exc)
        state["traceback"] = traceback.format_exc()[-4000:]
        _save_state(state)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

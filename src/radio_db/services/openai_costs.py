from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from radio_db.config import settings
from radio_db.services.budget import CostGuard


def _day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _extract_amount_usd(node: Any) -> float:
    if isinstance(node, (int, float)):
        return float(node)
    if not isinstance(node, dict):
        return 0.0

    # Typical shape: {"amount": {"value": 0.123, "currency": "usd"}}
    amount = node.get("amount")
    if isinstance(amount, dict):
        value = amount.get("value")
        currency = str(amount.get("currency", "usd")).lower()
        if isinstance(value, (int, float)) and currency in {"usd", "$", "us$"}:
            return float(value)
    if isinstance(amount, (int, float)):
        return float(amount)

    # Alternate wrapper shapes.
    for key in ("cost", "total_cost", "result", "data"):
        child = node.get(key)
        if isinstance(child, dict):
            nested = _extract_amount_usd(child)
            if nested > 0:
                return nested
    return 0.0


def _sum_costs_payload(payload: dict[str, Any]) -> float:
    def walk(node: Any) -> float:
        if isinstance(node, (int, float)):
            return float(node)
        if isinstance(node, list):
            return sum(walk(item) for item in node)
        if not isinstance(node, dict):
            return 0.0

        # Prefer explicit amount/cost wrappers and do not recurse further if one is present,
        # otherwise we risk double-counting nested payload summaries.
        amount = node.get("amount")
        if isinstance(amount, dict):
            value = amount.get("value")
            currency = str(amount.get("currency", "usd")).lower()
            if isinstance(value, (int, float)) and currency in {"usd", "$", "us$"}:
                return float(value)
        if isinstance(amount, (int, float)):
            return float(amount)

        for key in ("cost", "total_cost"):
            value = node.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, dict):
                nested = walk(value)
                if nested > 0:
                    return nested

        total = 0.0
        for key in ("data", "results", "line_items", "buckets", "items", "entries", "result"):
            child = node.get(key)
            if child is not None:
                total += walk(child)
        return total

    return max(0.0, float(walk(payload)))


def _reconcile_state_path() -> Path:
    return Path(".radio_db_state") / "openai_cost_reconcile.json"


def _write_state(payload: dict[str, Any]) -> None:
    path = _reconcile_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def reconcile_openai_daily_cost(day: date | None = None) -> dict[str, Any]:
    today = day or datetime.now(timezone.utc).date()
    start_dt, end_dt = _day_bounds_utc(today)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    budget = CostGuard(
        state_path=Path(".radio_db_state") / "cost_guard.json",
        max_daily_usd=settings.max_daily_usd,
        max_llm_calls_per_day=settings.max_llm_calls_per_day,
        input_price_per_1m=settings.llm_input_price_per_1m,
        output_price_per_1m=settings.llm_output_price_per_1m,
    )
    previous_estimate = float(budget.state.usd_spent_estimate)

    if not settings.agent_enabled:
        result = {
            "ok": False,
            "source": "openai_costs_api",
            "error": "AGENT_MODE=off",
            "date": today.isoformat(),
            "start_time": start_ts,
            "end_time": end_ts,
            "previous_usd_estimate": previous_estimate,
            "new_usd_estimate": previous_estimate,
            "synced": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(result)
        return result

    if not settings.openai_api_key:
        result = {
            "ok": False,
            "source": "openai_costs_api",
            "error": "OPENAI_API_KEY missing",
            "date": today.isoformat(),
            "start_time": start_ts,
            "end_time": end_ts,
            "previous_usd_estimate": previous_estimate,
            "new_usd_estimate": previous_estimate,
            "synced": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(result)
        return result

    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    url = f"{base_url}/organization/costs"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    params = {
        "start_time": start_ts,
        "end_time": end_ts,
        "bucket_width": "1d",
        "limit": 7,
    }

    timeout_seconds = float(max(10, int(settings.openai_cost_reconcile_timeout_seconds)))
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, params=params, headers=headers)
        status_code = int(response.status_code)
        if status_code >= 400:
            body = (response.text or "")[:600]
            result = {
                "ok": False,
                "source": "openai_costs_api",
                "error": f"HTTP {status_code}",
                "response_snippet": body,
                "date": today.isoformat(),
                "start_time": start_ts,
                "end_time": end_ts,
                "previous_usd_estimate": previous_estimate,
                "new_usd_estimate": previous_estimate,
                "synced": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_state(result)
            return result

        payload = response.json() if response.text else {}
        api_total = _sum_costs_payload(payload if isinstance(payload, dict) else {})
        sync_mode = (settings.openai_cost_reconcile_sync_mode or "max").strip().lower()
        new_estimate = budget.sync_usd_spent(api_total, mode=sync_mode)
        result = {
            "ok": True,
            "source": "openai_costs_api",
            "date": today.isoformat(),
            "start_time": start_ts,
            "end_time": end_ts,
            "api_daily_total_usd": round(api_total, 6),
            "previous_usd_estimate": round(previous_estimate, 6),
            "new_usd_estimate": round(new_estimate, 6),
            "sync_mode": sync_mode,
            "synced": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "source": "openai_costs_api",
            "error": str(exc),
            "date": today.isoformat(),
            "start_time": start_ts,
            "end_time": end_ts,
            "previous_usd_estimate": previous_estimate,
            "new_usd_estimate": previous_estimate,
            "synced": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(result)
        return result

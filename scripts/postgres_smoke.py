#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import create_engine, text


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a Radio DB instance against PostgreSQL.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081", help="Radio DB HTTP base URL.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), help="Expected PostgreSQL DATABASE_URL.")
    parser.add_argument("--radio-db-bin", default="/opt/radio-database/.venv/bin/radio-db")
    parser.add_argument("--run-cli", action="store_true", help="Also run selected CLI read checks.")
    parser.add_argument("--write-tests", action="store_true", help="Run reversible API write tests.")
    parser.add_argument(
        "--allow-non-postgres",
        action="store_true",
        help="Allow DATABASE_URL checks to run against a non-PostgreSQL database.",
    )
    return parser.parse_args()


def _json_request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    mode: str = "read-only",
    expected_status: int = 200,
) -> tuple[int, Any, str]:
    data = None
    headers = {"X-Radio-DB-Mode": mode}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = int(exc.code)
        body = exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return 0, None, str(exc)
    parsed: Any
    try:
        parsed = json.loads(body) if body else None
    except Exception:
        parsed = body
    if status != expected_status:
        return status, parsed, f"expected_status={expected_status} actual_status={status}"
    return status, parsed, ""


def _check_http_json(name: str, method: str, url: str, **kwargs: Any) -> CheckResult:
    status, payload, error = _json_request(method, url, **kwargs)
    if error:
        return CheckResult(name, False, error)
    if status == 0:
        return CheckResult(name, False, str(payload))
    return CheckResult(name, True, f"status={status}")


def _check_http_text(name: str, url: str, expected_status: int = 200) -> CheckResult:
    request = Request(url, headers={"X-Radio-DB-Mode": "read-only"}, method="GET")
    try:
        with urlopen(request, timeout=20) as response:
            status = int(response.status)
            body = response.read(2048).decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = int(exc.code)
        body = exc.read(2048).decode("utf-8", errors="replace")
    except URLError as exc:
        return CheckResult(name, False, str(exc))
    if status != expected_status:
        return CheckResult(name, False, f"expected_status={expected_status} actual_status={status}")
    return CheckResult(name, True, f"status={status} bytes_sample={len(body)}")


def _db_checks(database_url: str, allow_non_postgres: bool) -> list[CheckResult]:
    if not database_url:
        return [CheckResult("db:url", False, "missing DATABASE_URL")]
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    results: list[CheckResult] = []
    try:
        with engine.connect() as conn:
            dialect = engine.dialect.name
            if dialect != "postgresql" and not allow_non_postgres:
                results.append(CheckResult("db:dialect", False, f"expected=postgresql actual={dialect}"))
                return results
            results.append(CheckResult("db:dialect", True, dialect))
            for table, minimum in {
                "stations": 1,
                "evidence": 1,
                "forms": 0,
                "submission_agent_runs": 0,
                "contact_drafts": 0,
            }.items():
                count = int(conn.execute(text(f"select count(*) from {table}")).scalar() or 0)
                results.append(CheckResult(f"db:count:{table}", count >= minimum, str(count)))
            seq = conn.execute(text("select last_value from stations_id_seq")).scalar()
            max_id = conn.execute(text("select max(id) from stations")).scalar()
            results.append(CheckResult("db:sequence:stations", int(seq or 0) >= int(max_id or 0), f"seq={seq} max={max_id}"))
    except Exception as exc:
        results.append(CheckResult("db:connect", False, repr(exc)))
    finally:
        engine.dispose()
    return results


def _cli_checks(database_url: str, radio_db_bin: str) -> list[CheckResult]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.setdefault("ENABLE_BRAVE_SEARCH", "false")
    env.setdefault("ENABLE_GOOGLE_CSE_SEARCH", "false")
    env.setdefault("ENABLE_TAVILY_SEARCH", "false")
    env.setdefault("ENABLE_GROK_SEARCH", "false")
    env.setdefault("ENABLE_LINKUP_SEARCH", "false")
    env.setdefault("ENABLE_DUCKDUCKGO_SEARCH", "false")
    checks = [("cli:stats", [radio_db_bin, "stats"])]
    results: list[CheckResult] = []
    for name, cmd in checks:
        try:
            completed = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        except Exception as exc:
            results.append(CheckResult(name, False, repr(exc)))
            continue
        detail = (completed.stdout or completed.stderr or "").strip().splitlines()[:3]
        results.append(CheckResult(name, completed.returncode == 0, " | ".join(detail)))
    return results


def _read_api_checks(base_url: str) -> list[CheckResult]:
    base = base_url.rstrip("/")
    checks = [
        ("http:root", "GET", f"{base}/", None),
        ("api:stations", "GET", f"{base}/api/v1/stations?{urlencode({'limit': 3})}", None),
        ("api:station-detail", "GET", f"{base}/api/v1/stations/48711", None),
        ("api:forms", "GET", f"{base}/api/v1/forms?{urlencode({'limit': 3})}", None),
        ("api:agent-runs", "GET", f"{base}/api/v1/agent/runs?{urlencode({'limit': 3})}", None),
        ("api:data-control", "GET", f"{base}/api/v1/data-control/overview", None),
        ("api:contact-draft-preview", "GET", f"{base}/api/v1/contact-center/stations/48711/draft", None),
        ("api:station-groups", "GET", f"{base}/api/v1/station-groups", None),
        ("api:outreach-campaigns", "GET", f"{base}/api/v1/outreach-campaigns", None),
        ("api:browser-sessions", "GET", f"{base}/api/v1/browser/sessions", None),
    ]
    results: list[CheckResult] = []
    for name, method, url, payload in checks:
        if name == "http:root":
            results.append(_check_http_text(name, url))
            continue
        results.append(_check_http_json(name, method, url, payload=payload))
    results.append(
        _check_http_json(
            "api:write-blocked-read-only",
            "POST",
            f"{base}/api/v1/contact-center/stations/48711/drafts",
            payload={"subject": "postgres smoke blocked write", "body": "postgres smoke blocked write"},
            mode="read-only",
            expected_status=403,
        )
    )
    return results


def _cleanup_temp_station_groups(database_url: str) -> CheckResult:
    if not database_url:
        return CheckResult("api:write:cleanup-temp-groups", False, "missing DATABASE_URL")
    try:
        engine = create_engine(database_url, future=True, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    delete from station_group_memberships
                    where group_id in (
                        select id from station_groups
                        where name = 'postgres-smoke-temporary'
                           or name like 'postgres-smoke-temporary-%'
                    )
                    """
                )
            )
            result = conn.execute(
                text(
                    """
                    delete from station_groups
                    where name = 'postgres-smoke-temporary'
                       or name like 'postgres-smoke-temporary-%'
                    """
                )
            )
        engine.dispose()
        return CheckResult("api:write:cleanup-temp-groups", True, f"deleted={result.rowcount}")
    except Exception as exc:
        return CheckResult("api:write:cleanup-temp-groups", False, repr(exc))


def _write_api_checks(base_url: str, database_url: str) -> list[CheckResult]:
    base = base_url.rstrip("/")
    results: list[CheckResult] = [_cleanup_temp_station_groups(database_url)]
    if not results[0].ok:
        return results
    group_name = "postgres-smoke-temporary"
    status, payload, error = _json_request(
        "POST",
        f"{base}/api/v1/station-groups",
        payload={"name": group_name, "description": "temporary postgres smoke test"},
        mode="execute",
        expected_status=200,
    )
    if error:
        return [CheckResult("api:write:create-station-group", False, error)]
    group_id = int(payload.get("id") or 0) if isinstance(payload, dict) else 0
    results.append(CheckResult("api:write:create-station-group", group_id > 0, f"group_id={group_id}"))
    if group_id <= 0:
        return results
    results.append(
        _check_http_json(
            "api:write:add-station-group-membership",
            "POST",
            f"{base}/api/v1/station-groups/{group_id}/stations",
            payload={"station_ids": [48711], "note": "temporary postgres smoke test"},
            mode="execute",
        )
    )
    results.append(
        _check_http_json(
            "api:write:remove-station-group-membership",
            "DELETE",
            f"{base}/api/v1/station-groups/stations/48711/{group_id}",
            mode="execute",
        )
    )
    results.append(
        _check_http_json(
            "api:write:archive-station-group",
            "PATCH",
            f"{base}/api/v1/station-groups/{group_id}",
            payload={"name": f"{group_name}-{group_id}", "is_active": False},
            mode="execute",
        )
    )
    results.append(_cleanup_temp_station_groups(database_url))
    return results


def main() -> int:
    args = parse_args()
    results: list[CheckResult] = []
    results.extend(_db_checks(args.database_url, args.allow_non_postgres))
    results.extend(_read_api_checks(args.base_url))
    if args.run_cli:
        results.extend(_cli_checks(args.database_url, args.radio_db_bin))
    if args.write_tests:
        results.extend(_write_api_checks(args.base_url, args.database_url))

    failed = [item for item in results if not item.ok]
    for item in results:
        status = "ok" if item.ok else "FAIL"
        print(f"{status:4} {item.name:36} {item.detail}")
    print(f"\nsummary: {len(results) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

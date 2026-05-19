from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from radio_db.db import SessionLocal
from radio_db.dashboard import create_app
from radio_db.models.entities import (
    BrowserSessionEvent,
    BrowserSessionRecord,
    FormRecipe,
    FormStatus,
    FormType,
    Station,
    StationStatus,
    SubmissionForm,
)


def _cleanup_contract_test_artifacts() -> None:
    with SessionLocal() as db:
        test_station_ids = (
            "select id from stations "
            "where canonical_name like 'Execute Test Station %' "
            "or website_url like 'https://example.com/station-%'"
        )
        db.execute(text(f"delete from candidate_rescan_queue where main_station_id in ({test_station_ids})"))
        db.execute(text(f"delete from station_submission_assessments where station_id in ({test_station_ids})"))
        db.execute(text(f"delete from submission_agent_runs where station_id in ({test_station_ids})"))
        db.execute(text(f"delete from form_recipes where form_id in (select id from forms where station_id in ({test_station_ids}))"))
        db.execute(text(f"delete from form_fields where form_id in (select id from forms where station_id in ({test_station_ids}))"))
        db.execute(text(f"delete from forms where station_id in ({test_station_ids})"))
        db.execute(text(f"delete from stations where id in ({test_station_ids})"))
        db.execute(
            text(
                """
                delete from browser_session_events
                where session_id in (
                    select id from browser_sessions where session_key like 'test-persisted-%'
                )
                """
            )
        )
        db.execute(text("delete from browser_sessions where session_key like 'test-persisted-%'"))
        db.commit()


@pytest.fixture(autouse=True)
def cleanup_contract_test_artifacts():
    _cleanup_contract_test_artifacts()
    yield
    _cleanup_contract_test_artifacts()


def _client() -> TestClient:
    return TestClient(create_app())


def _first_station_id(client: TestClient) -> int:
    response = client.get("/api/v1/stations", params={"page": 1, "page_size": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    assert payload["items"]
    return int(payload["items"][0]["id"])


def _create_execute_test_form() -> tuple[int, int]:
    stamp = int(datetime.now(UTC).timestamp() * 1000)
    with SessionLocal() as db:
        station = Station(
            canonical_name=f"Execute Test Station {stamp}",
            normalized_name=f"execute test station {stamp}",
            country_code="DE",
            language="de",
            website_url=f"https://example.com/station-{stamp}",
            status=StationStatus.VERIFIED,
            confidence_score=0.9,
            fingerprint=f"execute-test-{stamp}",
        )
        db.add(station)
        db.flush()
        form = SubmissionForm(
            station_id=station.id,
            url=f"https://example.com/station-{stamp}/submit",
            page_title="Upload Your Music",
            language="en",
            form_type=FormType.MUSIC_SUBMISSION,
            status=FormStatus.CAPTCHA_PRESENT,
            requires_login=False,
            has_captcha=True,
            confidence=0.8,
        )
        db.add(form)
        db.flush()
        db.add(
            FormRecipe(
                form_id=form.id,
                version=1,
                mode="manual_scan",
                confidence_score=0.8,
                status="restricted",
                machine_mapping_json="{}",
                field_order_json="[]",
                upload_strategy_json="{}",
                submit_strategy_json='{"allow_live_submit": false}',
                success_detection_rules_json="[]",
                error_detection_rules_json="[]",
                retry_rules_json="[]",
                notes_for_future_runs="contract-test recipe",
            )
        )
        db.commit()
        return int(station.id), int(form.id)


def test_modular_api_contracts_cover_core_dashboard_flows() -> None:
    client = _client()
    station_id = _first_station_id(client)

    stations_response = client.get("/api/v1/stations", params={"page": 1, "page_size": 5})
    stations_payload = stations_response.json()
    assert stations_response.status_code == 200
    assert set(stations_payload) == {"total", "page", "page_size", "items"}
    assert stations_payload["items"][0]["id"] == station_id
    assert "canonical_name" in stations_payload["items"][0]
    assert "submission_count" in stations_payload["items"][0]

    station_detail_response = client.get(f"/api/v1/stations/{station_id}")
    station_detail = station_detail_response.json()
    assert station_detail_response.status_code == 200
    assert station_detail["id"] == station_id
    assert "submissions" in station_detail
    assert "contacts" in station_detail
    assert "people" in station_detail
    assert "forms" in station_detail

    contacts_response = client.get(f"/api/v1/contacts/station/{station_id}")
    contacts_payload = contacts_response.json()
    assert contacts_response.status_code == 200
    assert contacts_payload["station_id"] == station_id
    assert contacts_payload["station_name"]
    assert "contacts" in contacts_payload
    assert "people" in contacts_payload

    contact_draft_response = client.get(f"/api/v1/contact-center/stations/{station_id}/draft")
    contact_draft_payload = contact_draft_response.json()
    assert contact_draft_response.status_code == 200
    assert contact_draft_payload["station_id"] == station_id
    assert "subject" in contact_draft_payload
    assert "body" in contact_draft_payload
    assert "permission_mode" in contact_draft_payload

    templates_response = client.get("/api/v1/contact-center/templates")
    templates_payload = templates_response.json()
    assert templates_response.status_code == 200
    assert "items" in templates_payload
    assert isinstance(templates_payload["items"], list)

    drafts_response = client.get(f"/api/v1/contact-center/stations/{station_id}/drafts")
    drafts_payload = drafts_response.json()
    assert drafts_response.status_code == 200
    assert "items" in drafts_payload
    assert isinstance(drafts_payload["items"], list)

    control_response = client.get(f"/api/v1/control/stations/{station_id}")
    control_payload = control_response.json()
    assert control_response.status_code == 200
    assert control_payload["station_id"] == station_id
    assert control_payload["station_name"]
    assert "submissions" in control_payload
    assert "assessments" in control_payload

    forms_response = client.get("/api/v1/forms", params={"station_id": station_id, "limit": 5})
    forms_payload = forms_response.json()
    assert forms_response.status_code == 200
    assert set(forms_payload) == {"total", "items"}
    assert isinstance(forms_payload["items"], list)

    agent_runs_response = client.get("/api/v1/agent/runs", params={"station_id": station_id, "limit": 5})
    agent_runs_payload = agent_runs_response.json()
    assert agent_runs_response.status_code == 200
    assert set(agent_runs_payload) == {"total", "items"}
    assert isinstance(agent_runs_payload["items"], list)

    browser_sessions_response = client.get("/api/v1/browser/sessions")
    browser_sessions_payload = browser_sessions_response.json()
    assert browser_sessions_response.status_code == 200
    assert "items" in browser_sessions_payload
    assert isinstance(browser_sessions_payload["items"], list)

    data_control_response = client.get("/api/v1/data-control/overview")
    data_control_payload = data_control_response.json()
    assert data_control_response.status_code == 200
    assert "generated_at" in data_control_payload
    assert "monitor" in data_control_payload
    assert "country_discovery" in data_control_payload
    assert "api_history" in data_control_payload


def test_permission_mode_blocks_execute_operations_without_execute_header() -> None:
    client = _client()
    station_id = _first_station_id(client)

    blocked_response = client.patch(
        f"/api/v1/stations/{station_id}",
        json={"city": "Permission Check City"},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert blocked_response.status_code == 403
    assert "insufficient_permission_mode" in blocked_response.json()["detail"]


def test_permission_mode_blocks_contact_draft_persistence_in_read_only() -> None:
    client = _client()
    station_id = _first_station_id(client)

    blocked_response = client.post(
        f"/api/v1/contact-center/stations/{station_id}/drafts",
        json={},
        headers={"X-Radio-DB-Mode": "read-only"},
    )
    assert blocked_response.status_code == 403
    assert "insufficient_permission_mode" in blocked_response.json()["detail"]


def test_contact_center_dry_run_send_flow_contract() -> None:
    client = _client()
    station_id = _first_station_id(client)

    draft_response = client.post(
        f"/api/v1/contact-center/stations/{station_id}/drafts",
        json={},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert draft_response.status_code == 200
    draft_payload = draft_response.json()
    draft_id = int(draft_payload["draft_id"])

    send_response = client.post(
        f"/api/v1/contact-center/drafts/{draft_id}/send",
        json={},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert send_response.status_code == 200
    send_payload = send_response.json()
    send_id = int(send_payload["id"])
    assert send_payload["draft_id"] == draft_id
    assert send_payload["mode"] == "dry-run"

    sends_response = client.get(f"/api/v1/contact-center/drafts/{draft_id}/sends")
    assert sends_response.status_code == 200
    sends_payload = sends_response.json()
    assert "items" in sends_payload
    assert isinstance(sends_payload["items"], list)

    outcomes_response = client.get(f"/api/v1/contact-center/sends/{send_id}/outcomes")
    assert outcomes_response.status_code == 200
    outcomes_payload = outcomes_response.json()
    assert "items" in outcomes_payload
    assert isinstance(outcomes_payload["items"], list)


def test_contact_center_execute_send_requires_execute_mode() -> None:
    client = _client()
    station_id = _first_station_id(client)

    draft_response = client.post(
        f"/api/v1/contact-center/stations/{station_id}/drafts",
        json={},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert draft_response.status_code == 200
    draft_id = int(draft_response.json()["draft_id"])

    blocked_response = client.post(
        f"/api/v1/contact-center/drafts/{draft_id}/send",
        json={"mode": "execute", "channel": "email", "target_value": "test@example.com"},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert blocked_response.status_code == 403
    assert "execute_required" in blocked_response.json()["detail"]


def test_contact_center_execute_send_records_outcome_without_smtp_config() -> None:
    client = _client()
    station_id = _first_station_id(client)

    draft_response = client.post(
        f"/api/v1/contact-center/stations/{station_id}/drafts",
        json={},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert draft_response.status_code == 200
    draft_id = int(draft_response.json()["draft_id"])

    send_response = client.post(
        f"/api/v1/contact-center/drafts/{draft_id}/send",
        json={"mode": "execute", "channel": "email", "target_value": "test@example.com"},
        headers={"X-Radio-DB-Mode": "execute"},
    )
    assert send_response.status_code == 200
    send_payload = send_response.json()
    assert send_payload["mode"] == "execute"
    assert send_payload["status"] in {"sent", "failed", "blocked"}


def test_agent_execute_run_requires_execute_permission_for_live_mode() -> None:
    client = _client()
    station_id, form_id = _create_execute_test_form()

    blocked_response = client.post(
        "/api/v1/agent/runs/execute",
        json={"station_id": station_id, "form_id": form_id, "mode": "execute"},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert blocked_response.status_code == 403
    assert "execute_required" in blocked_response.json()["detail"]


def test_agent_execute_run_records_blocked_captcha_form() -> None:
    client = _client()
    station_id, form_id = _create_execute_test_form()

    response = client.post(
        "/api/v1/agent/runs/execute",
        json={"station_id": station_id, "form_id": form_id, "mode": "dry-run"},
        headers={"X-Radio-DB-Mode": "dry-run"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["station_id"] == station_id
    assert payload["form_id"] == form_id
    assert payload["goal"] == "submission_execute"
    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "captcha_present"
    assert payload["issues"]


def test_browser_api_reads_persisted_closed_session_and_events() -> None:
    session_key = f"test-persisted-{int(datetime.now(UTC).timestamp())}"
    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as db:
        row = BrowserSessionRecord(
            session_key=session_key,
            url="https://example.com/contact",
            title="Example Contact",
            status="closed",
            last_error="",
            action_count=1,
            event_count=2,
            metadata_json='{"label":"persisted-test","actions":[{"type":"navigate"}]}',
            created_at=now,
            updated_at=now,
            closed_at=now,
        )
        db.add(row)
        db.flush()
        db.add(
            BrowserSessionEvent(
                session_id=row.id,
                event_index=1,
                event_type="opened",
                payload_json='{"start_url":"https://example.com"}',
                url="https://example.com",
                status="idle",
                title="Example",
                created_at=now,
            )
        )
        db.add(
            BrowserSessionEvent(
                session_id=row.id,
                event_index=2,
                event_type="closing",
                payload_json="{}",
                url="https://example.com/contact",
                status="closed",
                title="Example Contact",
                created_at=now,
            )
        )
        db.commit()

    client = _client()

    session_response = client.get(f"/api/v1/browser/sessions/{session_key}")
    assert session_response.status_code == 200
    session_payload = session_response.json()
    assert session_payload["id"] == session_key
    assert session_payload["status"] == "closed"
    assert session_payload["action_count"] == 1

    events_response = client.get(f"/api/v1/browser/sessions/{session_key}/events", params={"limit": 10})
    assert events_response.status_code == 200
    events_payload = events_response.json()
    assert events_payload["session_id"] == session_key
    assert events_payload["event_count"] == 2
    assert len(events_payload["items"]) == 2
    assert events_payload["items"][0]["type"] == "opened"
    assert events_payload["items"][1]["type"] == "closing"

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from html import escape
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import Select, and_, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from radio_db.api.stations import router as stations_api_router
from radio_db.api.agent import router as agent_api_router
from radio_db.api.browser import router as browser_api_router
from radio_db.api.forms import router as forms_api_router
from radio_db.api.contacts import router as contacts_api_router
from radio_db.api.control import router as control_api_router
from radio_db.api.contact_center import router as contact_center_api_router
from radio_db.api.outreach_campaigns import router as outreach_campaigns_api_router
from radio_db.api.outreach_campaigns import redirect_short_press_release_code
from radio_db.api.data_control import router as data_control_api_router
from radio_db.api.station_groups import router as station_groups_api_router
from radio_db.config import settings
from radio_db.db import SessionLocal, engine, init_db, is_sqlite_engine
from radio_db.models.entities import (
    ContactRole,
    CrawlFrontier,
    DistributionNetwork,
    EmailBlacklist,
    Evidence,
    FormRecipe,
    MarketIntelligence,
    Station,
    StationAlias,
    StationContact,
    StationGenre,
    StationNetworkLink,
    StationSubmissionAssessment,
    StationProfileSnapshot,
    StationProgram,
    StationPerson,
    SubmissionAgentIssue,
    SubmissionAgentRun,
    SubmissionAgentStep,
    SubmissionMethod,
    SubmissionForm,
    StationStatus,
    SubmissionChannel,
)
from radio_db.services.dedupe import find_duplicate_station, station_fingerprint
from radio_db.connectors.brave_keys import (
    brave_answer_keys,
    brave_search_keys,
    effective_daily_limit,
    effective_monthly_limit,
)
from radio_db.services.forms_agent import agent_run_snapshot, start_manual_scan_run
from radio_db.services.normalize import normalize_text
from radio_db.services.pipeline import (
    boost_top_major_station_ids,
    get_scan_focus_countries,
    load_country_discovery_intelligence,
    load_brave_boost_state,
    load_high_priority_state,
    load_scan_focus_state,
    market_focus_countries,
    run_brave_boost_round,
    run_high_priority_deep_dive,
    save_scan_focus_state,
    stats as pipeline_stats,
)


_BOOST_LOCK = threading.Lock()
_HIGH_PRIORITY_LOCK = threading.Lock()
_AGENT_LOCK = threading.Lock()

_ARCHIVED_STATUS = getattr(StationStatus, "ARCHIVED", None)

AGENT_MANUAL_STATE_PATH = Path(".radio_db_state/agent_manual_state.json")
COUNTRY_DISCOVERY_STATE_PATH = Path(".radio_db_state/country_discovery_state.json")
ENV_PATH = Path(".env")


def _non_archived_status_filter():
    excluded = [StationStatus.REJECTED]
    if _ARCHIVED_STATUS is not None:
        excluded.append(_ARCHIVED_STATUS)
    return Station.status.notin_(excluded)


def _archived_status_condition():
    if _ARCHIVED_STATUS is None:
        return False
    return Station.status == _ARCHIVED_STATUS

AGENT_CONFIG_KEYS = [
    "AGENT_MODE",
    "PRIORITY_COUNTRIES",
    "COUNTRY_DISCOVERY_SEARCH_MODE",
    "COUNTRY_DISCOVERY_AUTO_TRANSLATE_QUERIES",
    "QUERY_TRANSLATE_MODEL",
    "COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN",
    "COUNTRY_DISCOVERY_MAX_RESULTS_PER_QUERY",
    "COUNTRY_DISCOVERY_MIN_CONFIDENCE",
    "COUNTRY_DISCOVERY_INCLUDE_LINKUP",
    "ENABLE_OPENAI_WEB_SEARCH",
    "OPENAI_WEB_SEARCH_MODEL",
    "MAX_OPENAI_WEB_CALLS_PER_DAY",
    "MAX_OPENAI_WEB_CALLS_PER_MONTH",
    "MAX_DAILY_USD",
    "MAX_LLM_CALLS_PER_DAY",
    "MAX_LLM_CALLS_PER_RUN",
    "MAX_LLM_CALLS_PER_DOMAIN_PER_RUN",
    "MAX_PAGE_FETCHES_PER_RUN",
    "MAX_CODEX_CALLS_PER_DAY",
    "MAX_CODEX_DAILY_USD",
    "BROWSER_WORKER_ENABLED",
    "BROWSER_MAX_STATIONS_PER_RUN",
    "BROWSER_MAX_FORMS_PER_STATION",
]

DEFAULT_MARKET_SEEDS: dict[str, dict[str, Any]] = {
    "CA": {
        "market_name": "Canada",
        "language_context": "English/French split market. Campus and community radio are unusually relevant for discovery and regional scenes.",
        "submission_norms": "Combine station-local outreach with networked distro paths where available.",
        "editorial_notes": "Campus/community and public-interest stations can be more useful than pure commercial Top 40 targets.",
        "outreach_style": "Use shared distro for reach, then follow up directly with the best-fit stations or shows.",
        "key_networks": ["NCRA/ANREC", "!earshot Distro"],
        "confidence": 0.95,
        "networks": [
            {
                "network_key": "ca_ncra_earshot",
                "name": "NCRA/ANREC + !earshot Distro",
                "network_type": "college_distribution_network",
                "submission_url": "https://earshot-online.com/",
                "coverage_note": "National campus/community distro path.",
                "rules_summary": "One distro submission can reach a broad member network; still follow up with priority stations.",
                "source_url": "https://www.ncra.ca/",
                "confidence": 0.95,
            }
        ],
    },
    "US": {
        "market_name": "United States",
        "language_context": "Large fragmented market with strong separation between commercial radio, college radio, public radio and tastemaker stations.",
        "submission_norms": "Do not assume one national route. Station-specific and show-specific targeting matters.",
        "editorial_notes": "College, non-commercial and tastemaker stations usually have higher discovery value than pure mainstream commercial networks.",
        "outreach_style": "Prioritize show fit, format fit and regional relevance.",
        "key_networks": ["NACC", "Public/college radio clusters"],
        "confidence": 0.88,
    },
    "GB": {
        "market_name": "United Kingdom",
        "language_context": "National brands are strong, but specialist, indie and community radio remain important for discovery.",
        "submission_norms": "Network brands and specialist DJs can matter more than generic station inboxes.",
        "editorial_notes": "BBC specialist strands, indie stations and community scenes often outperform broad commercial pop outlets for emerging music.",
        "outreach_style": "Target specific presenters or specialist shows where possible.",
        "key_networks": ["BBC specialist ecosystem", "Community radio"],
        "confidence": 0.86,
    },
    "DE": {
        "market_name": "Germany",
        "language_context": "Public broadcasters, youth brands, indie-oriented stations and regional private radio behave very differently.",
        "submission_norms": "Generic inboxes are often weak; editorial fit and youth/culture/public formats matter more.",
        "editorial_notes": "Public youth/culture stations and indie outlets are usually better discovery targets than broad AC/CHR private networks.",
        "outreach_style": "Lean on genre fit, editorial angle and regional/cultural relevance.",
        "key_networks": ["ARD youth/culture ecosystem", "Campusradio"],
        "confidence": 0.86,
    },
    "FR": {
        "market_name": "France",
        "language_context": "National brands and independent cultural radio coexist; language and local cultural framing matter.",
        "submission_norms": "Editorial framing and format identity often matter more than cold generic mailouts.",
        "editorial_notes": "Public/cultural and alternative stations can be more useful than pure mainstream commercial radio.",
        "outreach_style": "Use concise, culturally aware outreach with strong fit cues.",
        "key_networks": ["Radio associative ecosystem"],
        "confidence": 0.8,
    },
    "NL": {
        "market_name": "Netherlands",
        "language_context": "Compact market with strong national brands plus alternative and youth-oriented discovery lanes.",
        "submission_norms": "Direct editor or show targeting often beats broad generic submission.",
        "editorial_notes": "Alternative, culture and youth formats are disproportionately valuable.",
        "outreach_style": "Keep outreach direct, concise and fit-driven.",
        "key_networks": ["3FM/indie ecosystem"],
        "confidence": 0.8,
    },
    "AU": {
        "market_name": "Australia",
        "language_context": "Public youth radio, community radio and national networks all matter; regional scenes are meaningful.",
        "submission_norms": "Discovery routes often run through youth/community systems, not only large commercial stations.",
        "editorial_notes": "Triple j / community pathways can be far more valuable than broad commercial playlists for new acts.",
        "outreach_style": "Prioritize presenter/show fit and scene relevance.",
        "key_networks": ["Community radio sector", "ABC youth ecosystem"],
        "confidence": 0.84,
    },
    "AT": {"market_name": "Austria", "language_context": "Small market with public and private split.", "submission_norms": "Direct fit matters more than mass outreach.", "editorial_notes": "Youth/culture/public channels are the main discovery lanes.", "outreach_style": "Keep it concise and format-specific.", "key_networks": ["ORF ecosystem"], "confidence": 0.74},
    "CH": {"market_name": "Switzerland", "language_context": "Multilingual market with regional editorial differences.", "submission_norms": "Language region matters.", "editorial_notes": "Target by language area and format, not only by country.", "outreach_style": "Use localized outreach.", "key_networks": ["SRG regional ecosystem"], "confidence": 0.76},
    "IE": {"market_name": "Ireland", "language_context": "Small English-speaking market with national and local discovery lanes.", "submission_norms": "Presenter/show targeting matters.", "editorial_notes": "Local and specialist programs can matter more than generic station contact.", "outreach_style": "Short, direct, locally aware.", "key_networks": ["Student/community radio"], "confidence": 0.72},
    "BE": {"market_name": "Belgium", "language_context": "Split by language communities and regional media ecosystems.", "submission_norms": "Treat Flemish and Francophone lanes separately where possible.", "editorial_notes": "Regional fit matters strongly.", "outreach_style": "Localize by language community.", "key_networks": ["Regional public/community ecosystems"], "confidence": 0.72},
    "SE": {"market_name": "Sweden", "language_context": "Compact market with strong national brands and alternative taste clusters.", "submission_norms": "High value in format/presenter fit.", "editorial_notes": "Specialist and culture-led lanes can outperform broad commercial radio for discovery.", "outreach_style": "Direct and taste-aware.", "key_networks": ["Public/culture ecosystem"], "confidence": 0.72},
    "NO": {"market_name": "Norway", "language_context": "Small but structured market with public/commercial divide.", "submission_norms": "Target format and editorial lane precisely.", "editorial_notes": "Discovery value is concentrated in a limited set of outlets.", "outreach_style": "Focused and concise.", "key_networks": ["NRK ecosystem"], "confidence": 0.7},
    "DK": {"market_name": "Denmark", "language_context": "Small market; curation fit matters.", "submission_norms": "Generic mass outreach has limited value.", "editorial_notes": "Public and specialist routes matter most.", "outreach_style": "Target format fit.", "key_networks": ["Public/specialist ecosystem"], "confidence": 0.7},
    "FI": {"market_name": "Finland", "language_context": "Small market with public and specialist routes.", "submission_norms": "Strong format fit matters.", "editorial_notes": "Specialist lanes can carry disproportionate value.", "outreach_style": "Localized and concise.", "key_networks": ["Public/specialist ecosystem"], "confidence": 0.68},
    "ES": {"market_name": "Spain", "language_context": "National brands plus regional/language variation.", "submission_norms": "Regional context can matter significantly.", "editorial_notes": "Target by scene, region and format.", "outreach_style": "Local relevance helps.", "key_networks": ["Regional radio ecosystems"], "confidence": 0.72},
    "IT": {"market_name": "Italy", "language_context": "Large national brands with strong regional flavor.", "submission_norms": "Station-specific and region-specific outreach works better than generic volume.", "editorial_notes": "Private mainstream radio is less discovery-friendly than selective cultural/specialist outlets.", "outreach_style": "Be targeted and format-aware.", "key_networks": ["Regional/private ecosystems"], "confidence": 0.7},
    "NZ": {"market_name": "New Zealand", "language_context": "Small market where national youth and alternative lanes matter.", "submission_norms": "A few high-fit outlets can matter more than broad outreach.", "editorial_notes": "Youth/community routes are important.", "outreach_style": "Keep outreach lean and relevant.", "key_networks": ["Youth/community ecosystem"], "confidence": 0.72},
    "JP": {"market_name": "Japan", "language_context": "Large market with strong domestic ecosystem and formatting differences.", "submission_norms": "Local context and language matter heavily.", "editorial_notes": "Without local framing, generic outreach underperforms.", "outreach_style": "Localized, relationship-aware.", "key_networks": ["Regional network ecosystem"], "confidence": 0.68},
    "KR": {"market_name": "South Korea", "language_context": "Centralized media landscape with strong domestic market logic.", "submission_norms": "Format and local market alignment are key.", "editorial_notes": "Generic foreign outreach has low baseline yield.", "outreach_style": "Use localized framing.", "key_networks": ["Broadcast network ecosystem"], "confidence": 0.66},
    "BR": {"market_name": "Brazil", "language_context": "Large market with regional diversity and strong local identities.", "submission_norms": "Regional and format segmentation matter strongly.", "editorial_notes": "Broad national assumptions are risky.", "outreach_style": "Localize by region and style.", "key_networks": ["Regional/private ecosystems"], "confidence": 0.68},
    "MX": {"market_name": "Mexico", "language_context": "Large market with regional broadcast variation.", "submission_norms": "Regional targeting matters.", "editorial_notes": "Commercial mainstream and discovery paths diverge sharply.", "outreach_style": "Format and city relevance first.", "key_networks": ["Regional/private ecosystems"], "confidence": 0.68},
    "AR": {"market_name": "Argentina", "language_context": "Strong city/scene concentration with cultural-radio relevance.", "submission_norms": "Scene fit and cultural framing matter.", "editorial_notes": "Local credibility can matter more than volume.", "outreach_style": "Keep it culturally aware.", "key_networks": ["Cultural/indie radio ecosystem"], "confidence": 0.66},
    "CL": {"market_name": "Chile", "language_context": "Compact market with strong local scene orientation.", "submission_norms": "Target quality beats target count.", "editorial_notes": "A few strong-fit stations can matter more than broad mailing.", "outreach_style": "Concise and local.", "key_networks": ["Local/alternative ecosystems"], "confidence": 0.64},
    "CO": {"market_name": "Colombia", "language_context": "Large city-driven market with strong local scene influence.", "submission_norms": "Regional and city fit matter.", "editorial_notes": "Commercial vs discovery lanes differ heavily.", "outreach_style": "Target city and format carefully.", "key_networks": ["Regional ecosystems"], "confidence": 0.64},
    "ZA": {"market_name": "South Africa", "language_context": "Regional and community/public dimensions matter strongly.", "submission_norms": "Community and public ecosystems can be strategically important.", "editorial_notes": "Do not treat the market as one homogeneous lane.", "outreach_style": "Localize by audience and region.", "key_networks": ["Community/public ecosystems"], "confidence": 0.66},
    "PL": {"market_name": "Poland", "language_context": "National and regional differences matter.", "submission_norms": "Target by format and regional relevance.", "editorial_notes": "Broad generic outreach has limited efficiency.", "outreach_style": "Use local context.", "key_networks": ["Public/regional ecosystems"], "confidence": 0.64},
    "CZ": {"market_name": "Czechia", "language_context": "Smaller market with concentrated media lanes.", "submission_norms": "Targeted outreach matters more than volume.", "editorial_notes": "Public/specialist routes can carry outsized value.", "outreach_style": "Concise and local.", "key_networks": ["Public/regional ecosystems"], "confidence": 0.62},
    "PT": {"market_name": "Portugal", "language_context": "Compact market with national and regional station differences.", "submission_norms": "Direct fit matters.", "editorial_notes": "Selective outreach is preferable to broad lists.", "outreach_style": "Short and relevant.", "key_networks": ["Public/regional ecosystems"], "confidence": 0.62},
    "IN": {"market_name": "India", "language_context": "Very large, heterogeneous market with strong regional/language fragmentation.", "submission_norms": "Country-level assumptions are weak; regional strategy is essential.", "editorial_notes": "Language and audience segmentation dominate.", "outreach_style": "Localize heavily.", "key_networks": ["Regional/language ecosystems"], "confidence": 0.62},
}


def _run_command(cmd: list[str], timeout: int = 3) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.stdout or result.stderr or "").strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_json(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _pretty_json(raw: str | None) -> str:
    payload = _parse_json(raw, {})
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except Exception:
        return str(payload)


def _artifact_href(path_value: str | None) -> str:
    if not path_value:
        return ""
    return "/agent-artifact?" + urlencode({"path": path_value})


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _serve_spa_file(relative_path: str = "index.html") -> FileResponse:
    frontend_dist = _frontend_dist_dir().resolve()
    target = (frontend_dist / relative_path).resolve()
    if not frontend_dist.exists() or not target.is_file():
        raise HTTPException(status_code=503, detail="frontend_build_missing")
    if frontend_dist not in target.parents and target != frontend_dist:
        raise HTTPException(status_code=404, detail="frontend_asset_not_found")
    return FileResponse(target)


def _is_valid_email(value: str) -> bool:
    email = (value or "").strip().lower()
    if not email or len(email) > 320:
        return False
    return bool(re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email))


def _seed_market_intelligence(session: Session) -> None:
    for market_code, payload in DEFAULT_MARKET_SEEDS.items():
        market = session.scalar(
            select(MarketIntelligence).where(MarketIntelligence.market_code == market_code)
        )
        if market is None:
            session.add(
                MarketIntelligence(
                    market_code=market_code,
                    market_name=payload["market_name"],
                    language_context=payload.get("language_context"),
                    submission_norms=payload.get("submission_norms"),
                    editorial_notes=payload.get("editorial_notes"),
                    outreach_style=payload.get("outreach_style"),
                    key_networks_json=json.dumps(payload.get("key_networks") or []),
                    confidence=float(payload.get("confidence") or 0.0),
                )
            )
        for network_payload in payload.get("networks") or []:
            network = session.scalar(
                select(DistributionNetwork).where(
                    DistributionNetwork.network_key == network_payload["network_key"]
                )
            )
            if network is None:
                session.add(
                    DistributionNetwork(
                        network_key=network_payload["network_key"],
                        name=network_payload["name"],
                        market_code=market_code,
                        network_type=network_payload.get("network_type") or "association",
                        submission_url=network_payload.get("submission_url"),
                        submission_email=network_payload.get("submission_email"),
                        coverage_note=network_payload.get("coverage_note"),
                        rules_summary=network_payload.get("rules_summary"),
                        source_url=network_payload.get("source_url"),
                        confidence=float(network_payload.get("confidence") or 0.0),
                    )
                )


def _load_blacklisted_emails(session: Session) -> set[str]:
    return {
        str(row).strip().lower()
        for row in session.scalars(select(EmailBlacklist.email)).all()
        if str(row).strip()
    }


def _ensure_dashboard_schema() -> None:
    with SessionLocal() as session:
        if is_sqlite_engine(engine):
                columns = {
                    str(row[1])
                    for row in session.execute(text("PRAGMA table_info(stations)")).all()
                }
                if "manual_confirmed" not in columns:
                    session.execute(text("ALTER TABLE stations ADD COLUMN manual_confirmed BOOLEAN DEFAULT 0"))
                if "manual_confirmed_at" not in columns:
                    session.execute(text("ALTER TABLE stations ADD COLUMN manual_confirmed_at DATETIME"))
                submission_columns = {
                    str(row[1])
                    for row in session.execute(text("PRAGMA table_info(submission_channels)")).all()
                }
                if "manual_confirmed" not in submission_columns:
                    session.execute(text("ALTER TABLE submission_channels ADD COLUMN manual_confirmed BOOLEAN DEFAULT 0"))
                if "manual_confirmed_at" not in submission_columns:
                    session.execute(text("ALTER TABLE submission_channels ADD COLUMN manual_confirmed_at DATETIME"))
                contact_columns = {
                    str(row[1])
                    for row in session.execute(text("PRAGMA table_info(station_contacts)")).all()
                }
                if "manual_confirmed" not in contact_columns:
                    session.execute(text("ALTER TABLE station_contacts ADD COLUMN manual_confirmed BOOLEAN DEFAULT 0"))
                if "manual_confirmed_at" not in contact_columns:
                    session.execute(text("ALTER TABLE station_contacts ADD COLUMN manual_confirmed_at DATETIME"))
                people_columns = {
                    str(row[1])
                    for row in session.execute(text("PRAGMA table_info(station_people)")).all()
                }
                if "manual_confirmed" not in people_columns:
                    session.execute(text("ALTER TABLE station_people ADD COLUMN manual_confirmed BOOLEAN DEFAULT 0"))
                if "manual_confirmed_at" not in people_columns:
                    session.execute(text("ALTER TABLE station_people ADD COLUMN manual_confirmed_at DATETIME"))
                session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS market_intelligence (
                            id INTEGER PRIMARY KEY,
                            market_code VARCHAR(8) NOT NULL,
                            market_name VARCHAR(120) NOT NULL,
                            language_context TEXT,
                            submission_norms TEXT,
                            editorial_notes TEXT,
                            outreach_style TEXT,
                            key_networks_json TEXT NOT NULL DEFAULT '[]',
                            confidence FLOAT NOT NULL DEFAULT 0.0,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT uq_market_intelligence_code UNIQUE (market_code)
                        )
                        """
                    )
                )
                session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS distribution_networks (
                            id INTEGER PRIMARY KEY,
                            network_key VARCHAR(120) NOT NULL,
                            name VARCHAR(255) NOT NULL,
                            market_code VARCHAR(8) NOT NULL,
                            network_type VARCHAR(64) NOT NULL DEFAULT 'association',
                            submission_url VARCHAR(1024),
                            submission_email VARCHAR(320),
                            coverage_note TEXT,
                            rules_summary TEXT,
                            source_url VARCHAR(1024),
                            confidence FLOAT NOT NULL DEFAULT 0.0,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT uq_distribution_network_key UNIQUE (network_key)
                        )
                        """
                    )
                )
                session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS station_network_links (
                            id INTEGER PRIMARY KEY,
                            station_id INTEGER NOT NULL,
                            network_id INTEGER NOT NULL,
                            relationship_type VARCHAR(64) NOT NULL DEFAULT 'member',
                            confidence FLOAT NOT NULL DEFAULT 0.0,
                            notes TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT uq_station_network_link UNIQUE (station_id, network_id, relationship_type),
                            FOREIGN KEY(station_id) REFERENCES stations(id) ON DELETE CASCADE,
                            FOREIGN KEY(network_id) REFERENCES distribution_networks(id) ON DELETE CASCADE
                        )
                        """
                    )
                )
                session.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS email_blacklist (
                            id INTEGER PRIMARY KEY,
                            email VARCHAR(320) NOT NULL,
                            reason TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT uq_email_blacklist_email UNIQUE (email)
                        )
                        """
                    )
                )
        _seed_market_intelligence(session)
        ca_network = session.scalar(
            select(DistributionNetwork).where(DistributionNetwork.network_key == "ca_ncra_earshot")
        )
        if ca_network is None:
            session.commit()
            return
        linked_station_ids = {
            int(row[0])
            for row in session.execute(
                text(
                    """
                    SELECT id
                    FROM stations
                    WHERE upper(country_code) = 'CA'
                      AND status = 'VERIFIED'
                      AND (
                        lower(canonical_name) LIKE '%cjlo%'
                        OR lower(canonical_name) LIKE '%cfuv%'
                        OR lower(canonical_name) LIKE '%cism%'
                        OR lower(canonical_name) LIKE '%chly%'
                        OR lower(canonical_name) LIKE '%campus%'
                        OR lower(canonical_name) LIKE '%community%'
                      )
                    LIMIT 24
                    """
                )
            ).all()
        }
        for station_id in sorted(linked_station_ids):
            exists_link = session.scalar(
                select(StationNetworkLink).where(
                    StationNetworkLink.station_id == station_id,
                    StationNetworkLink.network_id == ca_network.id,
                    StationNetworkLink.relationship_type == "distributed_via",
                )
            )
            if exists_link is None:
                session.add(
                    StationNetworkLink(
                        station_id=station_id,
                        network_id=ca_network.id,
                        relationship_type="distributed_via",
                        confidence=0.75,
                        notes="Auto-linked by Canadian campus/community heuristic.",
                    )
                )
        session.commit()


def _load_env_map(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _write_env_updates(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    output: list[str] = []
    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        k = key.strip()
        if k in remaining:
            output.append(f"{k}={remaining.pop(k)}")
        else:
            output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _agent_manual_state() -> dict[str, Any]:
    default = {
        "is_running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "last_exit_code": None,
        "last_error": "",
        "last_output": "",
    }
    payload = _load_json(AGENT_MANUAL_STATE_PATH)
    if not payload:
        return default
    out = dict(default)
    out.update(payload)
    return out


def _read_only_redirect(target: str, msg_key: str = "legacy_msg", msg: str = "read_only") -> RedirectResponse:
    safe_target = target if target.startswith("/") else "/legacy"
    joiner = "&" if "?" in safe_target else "?"
    return RedirectResponse(url=f"{safe_target}{joiner}{msg_key}={msg}", status_code=303)


def _save_agent_manual_state(state: dict[str, Any]) -> None:
    _save_json(AGENT_MANUAL_STATE_PATH, state)


def _start_country_discovery_background() -> bool:
    with _AGENT_LOCK:
        state = _agent_manual_state()
        if state.get("is_running"):
            return False
        state.update(
            {
                "is_running": True,
                "last_started_at": datetime.now(UTC).isoformat(),
                "last_error": "",
            }
        )
        _save_agent_manual_state(state)

    def _worker() -> None:
        try:
            result = subprocess.run(
                [
                    "bash",
                    "-lc",
                    "cd /opt/radio-database && source .venv/bin/activate && ./scripts/run_country_discovery_cycle.sh",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            output = (result.stdout or "").strip()
            error = (result.stderr or "").strip()
            state = _agent_manual_state()
            state.update(
                {
                    "is_running": False,
                    "last_finished_at": datetime.now(UTC).isoformat(),
                    "last_exit_code": int(result.returncode),
                    "last_output": (output[-4000:] if output else ""),
                    "last_error": (error[-2000:] if error else ""),
                }
            )
            _save_agent_manual_state(state)
        except Exception as exc:
            state = _agent_manual_state()
            state.update(
                {
                    "is_running": False,
                    "last_finished_at": datetime.now(UTC).isoformat(),
                    "last_exit_code": 1,
                    "last_error": str(exc),
                }
            )
            _save_agent_manual_state(state)

    thread = threading.Thread(target=_worker, name="radio-db-country-discovery", daemon=True)
    thread.start()
    return True


def _station_query(
    session: Session,
    q: str,
    country: str,
    status: str,
    genre: str,
    has_submission: bool | None,
    has_people: bool | None,
    min_confidence: float,
    priority_only: bool = False,
) -> Select[tuple[Station]]:
    stmt: Select[tuple[Station]] = select(Station)
    status_value = (status or "").strip().lower()

    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Station.canonical_name).like(needle),
                func.lower(func.coalesce(Station.city, "")).like(needle),
                func.lower(func.coalesce(Station.website_url, "")).like(needle),
            )
        )
    if country:
        stmt = stmt.where(func.upper(Station.country_code) == country.upper())
    # Frontend restriction: default to the active verified shortlist, but allow explicit status views.
    if status_value == "candidate":
        stmt = stmt.where(Station.status == StationStatus.CANDIDATE)
    elif status_value == "verified":
        stmt = stmt.where(Station.status == StationStatus.VERIFIED)
    elif status_value == "rejected":
        stmt = stmt.where(Station.status == StationStatus.REJECTED)
    elif status_value == "archived":
        stmt = stmt.where(_archived_status_condition())
    else:
        stmt = stmt.where(
            Station.status == StationStatus.VERIFIED,
            Station.confidence_score >= 0.6,
            Station.priority_tier > 0,
        )
    if genre:
        genre_needle = f"%{genre.lower()}%"
        stmt = stmt.where(
            exists(
                select(StationGenre.id).where(
                    StationGenre.station_id == Station.id,
                    func.lower(StationGenre.genre).like(genre_needle),
                )
            )
        )
    if has_submission is True:
        stmt = stmt.where(exists(select(SubmissionChannel.id).where(SubmissionChannel.station_id == Station.id)))
    if has_submission is False:
        stmt = stmt.where(~exists(select(SubmissionChannel.id).where(SubmissionChannel.station_id == Station.id)))
    if has_people is True:
        stmt = stmt.where(exists(select(StationPerson.id).where(StationPerson.station_id == Station.id)))
    if has_people is False:
        stmt = stmt.where(~exists(select(StationPerson.id).where(StationPerson.station_id == Station.id)))
    if min_confidence > 0:
        stmt = stmt.where(Station.confidence_score >= min_confidence)

    if priority_only:
        stmt = stmt.where(Station.priority_tier > 0)
        stmt = stmt.where(Station.status == StationStatus.VERIFIED)
        stmt = stmt.where(Station.confidence_score >= 0.6)

    return stmt


def _monitor_snapshot(session: Session) -> dict[str, Any]:
    state_dir = Path(".radio_db_state")
    budget = _load_json(state_dir / "cost_guard.json")
    usage = _load_json(state_dir / "api_usage.json")
    openai_reconcile = _load_json(state_dir / "openai_cost_reconcile.json")
    verify_checkpoint = _load_json(state_dir / "verify_checkpoint.json")
    boost_state = load_brave_boost_state()
    high_priority_state = load_high_priority_state()
    scan_focus_state = load_scan_focus_state()
    scan_focus_countries = get_scan_focus_countries()
    scan_focus_enabled = bool(scan_focus_state.get("enabled", False))
    scan_focus_market = str(scan_focus_state.get("market_focus", "international"))

    focus_filters = [
        _non_archived_status_filter(),
        Station.website_url.is_not(None),
        func.length(func.trim(func.coalesce(Station.website_url, ""))) > 0,
        Station.confidence_score >= settings.station_enrich_min_station_confidence,
    ]
    if scan_focus_enabled and scan_focus_countries:
        focus_filters.append(func.upper(Station.country_code).in_(sorted(scan_focus_countries)))

    focus_eligible_total = session.scalar(select(func.count(Station.id)).where(*focus_filters)) or 0
    focus_processed_ever = session.scalar(
        select(func.count(func.distinct(Evidence.station_id)))
        .select_from(Evidence)
        .join(Station, Station.id == Evidence.station_id)
        .where(
            Evidence.source_id.in_(("station_enrich_search", "station_enrich_touch")),
            *focus_filters,
        )
    ) or 0
    focus_pending = max(0, focus_eligible_total - focus_processed_ever)
    focus_progress_ratio = (focus_processed_ever / focus_eligible_total) if focus_eligible_total else 1.0

    boost_unique_seen_by_market: dict[str, list[int]] = boost_state.get("unique_seen_by_market") or {}

    def _boost_market_progress(market: str) -> tuple[int, int, float]:
        countries = market_focus_countries(market)
        eligible_filters = [
            _non_archived_status_filter(),
            Station.website_url.is_not(None),
            func.length(func.trim(func.coalesce(Station.website_url, ""))) > 0,
            Station.confidence_score >= settings.brave_boost_min_station_confidence,
        ]
        if market == "top_major":
            top_major_ids = boost_top_major_station_ids(
                session=session,
                min_station_confidence=settings.brave_boost_min_station_confidence,
            )
            if not top_major_ids:
                return 0, 0, 1.0
            eligible_filters.append(Station.id.in_(sorted(top_major_ids)))
        if countries:
            eligible_filters.append(func.upper(Station.country_code).in_(sorted(countries)))
        eligible = session.scalar(select(func.count(Station.id)).where(*eligible_filters)) or 0

        seen_raw = boost_unique_seen_by_market.get(market, []) or []
        seen_ids = sorted({int(sid) for sid in seen_raw if str(sid).isdigit()})
        if not seen_ids:
            # Backward-compatible fallback for runs before per-market unique tracking existed.
            unique_seen = session.scalar(
                select(func.count(func.distinct(Evidence.station_id)))
                .select_from(Evidence)
                .join(Station, Station.id == Evidence.station_id)
                .where(
                    Evidence.source_id == "brave_boost",
                    *eligible_filters,
                )
            ) or 0
            ratio = (unique_seen / eligible) if eligible else 1.0
            return int(eligible), int(unique_seen), float(ratio)
        seen_filters = list(eligible_filters)
        seen_filters.append(Station.id.in_(seen_ids))
        unique_seen = session.scalar(select(func.count(Station.id)).where(*seen_filters)) or 0
        ratio = (unique_seen / eligible) if eligible else 1.0
        return int(eligible), int(unique_seen), float(ratio)

    last_boost_market = str(boost_state.get("last_market_focus") or "international").strip().lower()
    if last_boost_market not in {"international", "dach", "anglo", "eu_core", "top_major"}:
        last_boost_market = "international"
    dach_eligible, dach_unique, dach_ratio = _boost_market_progress("dach")
    last_eligible, last_unique, last_ratio = _boost_market_progress(last_boost_market)

    def _key_count(csv_value: str | None, single_value: str | None) -> int:
        keys: list[str] = []
        if csv_value:
            keys.extend(k.strip() for k in csv_value.split(",") if k.strip())
        if single_value:
            keys.append(single_value.strip())
        deduped = {k for k in keys if k}
        return max(1, len(deduped))

    tavily_key_count = _key_count(settings.tavily_api_keys, settings.tavily_api_key)
    linkup_key_count = _key_count(settings.linkup_api_keys, settings.linkup_api_key)
    effective_max_tavily_day = settings.max_tavily_calls_per_day * tavily_key_count
    effective_max_tavily_month = settings.max_tavily_calls_per_month * tavily_key_count
    effective_max_linkup_day = settings.max_linkup_calls_per_day * linkup_key_count
    effective_max_linkup_month = settings.max_linkup_calls_per_month * linkup_key_count

    stations_total = session.scalar(select(func.count(Station.id))) or 0
    stations_rejected = session.scalar(select(func.count(Station.id)).where(Station.status == StationStatus.REJECTED)) or 0
    stations_archived = session.scalar(select(func.count(Station.id)).where(_archived_status_condition())) or 0
    stations_active = max(0, stations_total - stations_rejected - stations_archived)
    stations_verified = session.scalar(
        select(func.count(Station.id)).where(Station.status == StationStatus.VERIFIED)
    ) or 0
    visible_high_priority = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            Station.confidence_score >= 0.6,
            Station.priority_tier > 0,
        )
    ) or 0
    manual_confirmed_count = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            Station.priority_tier > 0,
            Station.manual_confirmed.is_(True),
        )
    ) or 0
    stations_with_submission = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id)))
        .select_from(SubmissionChannel)
        .join(Station, Station.id == SubmissionChannel.station_id)
        .where(_non_archived_status_filter())
    ) or 0
    stations_without_submission = max(0, stations_active - stations_with_submission)
    submission_coverage_active = (stations_with_submission / stations_active) if stations_active else 0.0
    verified_with_submission = session.scalar(
        select(func.count(Station.id)).where(
            Station.status == StationStatus.VERIFIED,
            Station.id.in_(select(SubmissionChannel.station_id)),
        )
    ) or 0
    verified_submission_coverage = (verified_with_submission / stations_verified) if stations_verified else 0.0
    stations_with_newcomer_signal = session.scalar(
        select(func.count(func.distinct(SubmissionChannel.station_id)))
        .select_from(SubmissionChannel)
        .join(Station, Station.id == SubmissionChannel.station_id)
        .where(
            _non_archived_status_filter(),
            (SubmissionChannel.accepts_newcomers.is_(True))
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%new artist%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%unsigned%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%emerging%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%newcomer%")
            | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%demo%")
        )
    ) or 0
    stations_with_decision_maker = session.scalar(
        select(func.count(func.distinct(StationPerson.station_id))).where(
            StationPerson.role.in_([ContactRole.MUSIC_DIRECTOR, ContactRole.PROGRAM_DIRECTOR, ContactRole.PRODUCER])
        )
    ) or 0
    pitch_ready_stations = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            Station.confidence_score >= settings.station_enrich_min_station_confidence,
            Station.id.in_(select(SubmissionChannel.station_id)),
            Station.id.in_(
                select(SubmissionChannel.station_id).where(
                    (SubmissionChannel.accepts_newcomers.is_(True))
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%new artist%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%unsigned%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%emerging%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%newcomer%")
                    | func.lower(func.coalesce(SubmissionChannel.requirements, "")).like("%demo%")
                )
            ),
            Station.id.in_(
                select(StationPerson.station_id).where(
                    StationPerson.role.in_([ContactRole.MUSIC_DIRECTOR, ContactRole.PROGRAM_DIRECTOR, ContactRole.PRODUCER])
                )
            ),
        )
    ) or 0
    pitch_ready_ratio = (pitch_ready_stations / stations_active) if stations_active else 0.0
    verified_ratio_active = (stations_verified / stations_active) if stations_active else 0.0
    candidate_stations = session.scalar(
        select(func.count(Station.id)).where(Station.status == StationStatus.CANDIDATE)
    ) or 0
    tested_candidate_stations = session.scalar(
        select(func.count(func.distinct(Evidence.station_id)))
        .select_from(Evidence)
        .join(Station, Station.id == Evidence.station_id)
        .where(
            Evidence.source_id == "quality_verify",
            Station.status == StationStatus.CANDIDATE,
        )
    ) or 0
    untested_candidate_stations = max(0, candidate_stations - tested_candidate_stations)
    verify_progress_ratio = (tested_candidate_stations / candidate_stations) if candidate_stations else 1.0
    active_without_website = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            or_(Station.website_url.is_(None), func.length(func.trim(func.coalesce(Station.website_url, ""))) == 0),
        )
    ) or 0
    cooldown_cutoff = datetime.utcnow() - timedelta(hours=max(1, settings.station_enrich_cooldown_hours))
    touched_recent_station_count = session.scalar(
        select(func.count(func.distinct(Evidence.station_id))).where(
            Evidence.source_id == "station_enrich_touch",
            Evidence.created_at >= cooldown_cutoff,
        )
    ) or 0
    eligible_active_with_website = session.scalar(
        select(func.count(Station.id)).where(
            _non_archived_status_filter(),
            Station.website_url.is_not(None),
            Station.confidence_score >= settings.station_enrich_min_station_confidence,
        )
    ) or 0
    pending_after_cooldown = max(0, eligible_active_with_website - touched_recent_station_count)

    return {
        "now_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "stations_total": stations_total,
        "stations_active": stations_active,
        "stations_rejected": stations_rejected,
        "stations_archived": stations_archived,
        "stations_verified": stations_verified,
        "visible_high_priority": visible_high_priority,
        "manual_confirmed_count": manual_confirmed_count,
        "verified_ratio_active": verified_ratio_active,
        "submission_channels": session.scalar(select(func.count(SubmissionChannel.id))) or 0,
        "stations_with_submission": stations_with_submission,
        "stations_without_submission": stations_without_submission,
        "submission_coverage_active": submission_coverage_active,
        "verified_with_submission": verified_with_submission,
        "verified_submission_coverage": verified_submission_coverage,
        "stations_with_newcomer_signal": stations_with_newcomer_signal,
        "stations_with_decision_maker": stations_with_decision_maker,
        "pitch_ready_stations": pitch_ready_stations,
        "pitch_ready_ratio": pitch_ready_ratio,
        "candidate_stations": candidate_stations,
        "tested_candidate_stations": tested_candidate_stations,
        "untested_candidate_stations": untested_candidate_stations,
        "verify_progress_ratio": verify_progress_ratio,
        "active_without_website": active_without_website,
        "station_enrich_touched_recent": touched_recent_station_count,
        "station_enrich_pending_after_cooldown": pending_after_cooldown,
        "station_enrich_cooldown_hours": settings.station_enrich_cooldown_hours,
        "people_entities": session.scalar(select(func.count(StationPerson.id))) or 0,
        "profile_snapshots": session.scalar(select(func.count(StationProfileSnapshot.id))) or 0,
        "evidence_24h": session.scalar(
            select(func.count(Evidence.id)).where(Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
        )
        or 0,
        "internal_urls_today": session.scalar(
            select(func.count(Evidence.id)).where(
                Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                Evidence.source_id == "station_enrich_search",
                Evidence.raw_title == "Internal station source",
            )
        )
        or 0,
        "internal_stations_today": session.scalar(
            select(func.count(func.distinct(Evidence.station_id))).where(
                Evidence.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                Evidence.source_id == "station_enrich_search",
                Evidence.raw_title == "Internal station source",
            )
        )
        or 0,
        "frontier_due": session.scalar(
            select(func.count(CrawlFrontier.id)).where(
                or_(CrawlFrontier.next_run_at.is_(None), CrawlFrontier.next_run_at <= datetime.utcnow())
            )
        )
        or 0,
        "verify_checkpoint_last_station_id": verify_checkpoint.get("last_station_id", 0),
        "verify_checkpoint_completed": bool(verify_checkpoint.get("completed", False)),
        "verify_checkpoint_updated_at": verify_checkpoint.get("updated_at", "-"),
        "max_daily_usd": settings.max_daily_usd,
        "usd_spent_today_estimate": budget.get("usd_spent_estimate", 0.0),
        "llm_calls_today": budget.get("llm_calls", 0),
        "max_llm_calls_per_day": settings.max_llm_calls_per_day,
        "llm_provider_mode": settings.llm_provider_mode,
        "xai_configured": bool(settings.xai_api_key),
        "openai_reconcile_ok": bool(openai_reconcile.get("ok", False)),
        "openai_reconcile_updated_at": openai_reconcile.get("updated_at", "-"),
        "openai_reconcile_daily_total_usd": float(openai_reconcile.get("api_daily_total_usd", 0.0) or 0.0),
        "openai_reconcile_sync_mode": str(openai_reconcile.get("sync_mode", "-")),
        "openai_reconcile_error": str(openai_reconcile.get("error", "") or ""),
        "brave_calls_month": usage.get("brave_calls", 0),
        "max_brave_calls_per_month": effective_monthly_limit(
            settings.max_brave_calls_per_month,
            len(brave_search_keys()),
        ),
        "brave_answer_calls_day": usage.get("brave_answer_calls_day", 0),
        "brave_answer_calls_month": usage.get("brave_answer_calls_month", 0),
        "max_brave_answer_calls_per_day": effective_daily_limit(
            settings.max_brave_answer_calls_per_day,
            len(brave_answer_keys()),
        ),
        "max_brave_answer_calls_per_month": effective_monthly_limit(
            settings.max_brave_answer_calls_per_month,
            len(brave_answer_keys()),
        ),
        "google_cse_calls_day": usage.get("google_cse_calls_day", 0),
        "google_cse_calls_month": usage.get("google_cse_calls_month", 0),
        "max_google_cse_calls_per_day": settings.max_google_cse_calls_per_day,
        "max_google_cse_calls_per_month": settings.max_google_cse_calls_per_month,
        "tavily_calls_day": usage.get("tavily_calls_day", 0),
        "tavily_calls_month": usage.get("tavily_calls_month", 0),
        "max_tavily_calls_per_day": effective_max_tavily_day,
        "max_tavily_calls_per_month": effective_max_tavily_month,
        "grok_calls_day": usage.get("grok_calls_day", 0),
        "grok_calls_month": usage.get("grok_calls_month", 0),
        "grok_usd_day": usage.get("grok_usd_day", 0.0),
        "grok_usd_month": usage.get("grok_usd_month", 0.0),
        "max_grok_calls_per_day": settings.max_grok_calls_per_day,
        "max_grok_calls_per_month": settings.max_grok_calls_per_month,
        "max_grok_usd_per_month": settings.max_grok_usd_per_month,
        "linkup_calls_day": usage.get("linkup_calls_day", 0),
        "linkup_calls_month": usage.get("linkup_calls_month", 0),
        "max_linkup_calls_per_day": effective_max_linkup_day,
        "max_linkup_calls_per_month": effective_max_linkup_month,
        "duckduckgo_calls_day": usage.get("duckduckgo_calls_day", 0),
        "duckduckgo_calls_month": usage.get("duckduckgo_calls_month", 0),
        "max_duckduckgo_calls_per_day": settings.max_duckduckgo_calls_per_day,
        "max_duckduckgo_calls_per_month": settings.max_duckduckgo_calls_per_month,
        "boost_is_running": bool(boost_state.get("is_running", False)),
        "boost_last_started_at": boost_state.get("last_started_at") or "-",
        "boost_last_finished_at": boost_state.get("last_finished_at") or "-",
        "boost_last_market_focus": boost_state.get("last_market_focus") or "international",
        "boost_last_budget_usd": float(boost_state.get("last_budget_usd", 0.0) or 0.0),
        "boost_last_spent_usd": float(boost_state.get("last_spent_usd", 0.0) or 0.0),
        "boost_last_calls": int(boost_state.get("last_calls", 0) or 0),
        "boost_last_stations_processed": int(boost_state.get("last_stations_processed", 0) or 0),
        "boost_last_results": int(boost_state.get("last_results", 0) or 0),
        "boost_last_submissions_added": int(boost_state.get("last_submissions_added", 0) or 0),
        "boost_last_contacts_added": int(boost_state.get("last_contacts_added", 0) or 0),
        "boost_last_candidate_submissions": int(boost_state.get("last_candidate_submissions", 0) or 0),
        "boost_last_candidate_contacts": int(boost_state.get("last_candidate_contacts", 0) or 0),
        "boost_month": boost_state.get("month") or datetime.utcnow().strftime("%Y-%m"),
        "boost_month_spent_usd": float(boost_state.get("month_spent_usd", 0.0) or 0.0),
        "boost_month_calls": int(boost_state.get("month_calls", 0) or 0),
        "boost_last_error": boost_state.get("last_error") or "",
        "boost_current_calls": int(boost_state.get("current_calls", 0) or 0),
        "boost_current_stations_processed": int(boost_state.get("current_stations_processed", 0) or 0),
        "boost_current_results": int(boost_state.get("current_results", 0) or 0),
        "boost_current_submissions_added": int(boost_state.get("current_submissions_added", 0) or 0),
        "boost_current_contacts_added": int(boost_state.get("current_contacts_added", 0) or 0),
        "boost_current_candidate_submissions": int(boost_state.get("current_candidate_submissions", 0) or 0),
        "boost_current_candidate_contacts": int(boost_state.get("current_candidate_contacts", 0) or 0),
        "boost_default_budget_usd": settings.brave_boost_default_budget_usd,
        "boost_effective_usd_per_call": settings.brave_boost_usd_per_call,
        "boost_dach_eligible": dach_eligible,
        "boost_dach_unique_seen": dach_unique,
        "boost_dach_progress_ratio": dach_ratio,
        "boost_last_market_eligible": last_eligible,
        "boost_last_market_unique_seen": last_unique,
        "boost_last_market_progress_ratio": last_ratio,
        "boost_no_repeat_days": settings.brave_boost_no_repeat_days,
        "boost_recent_seen_count": len(boost_state.get("seen_recent", {}) or {}),
        "boost_last_market_cursor": int((boost_state.get("per_market_cursor", {}) or {}).get(last_boost_market, 0) or 0),
        "high_priority_is_running": bool(high_priority_state.get("is_running", False)),
        "high_priority_last_started_at": high_priority_state.get("last_started_at") or "-",
        "high_priority_last_finished_at": high_priority_state.get("last_finished_at") or "-",
        "high_priority_last_stations": int(high_priority_state.get("last_stations", 0) or 0),
        "high_priority_last_urls": int(high_priority_state.get("last_urls", 0) or 0),
        "high_priority_last_results": int(high_priority_state.get("last_results", 0) or 0),
        "high_priority_last_submissions_added": int(high_priority_state.get("last_submissions_added", 0) or 0),
        "high_priority_last_contacts_added": int(high_priority_state.get("last_contacts_added", 0) or 0),
        "high_priority_last_llm_calls": int(high_priority_state.get("last_llm_calls", 0) or 0),
        "high_priority_last_targets": ", ".join(high_priority_state.get("last_targets", []) or []),
        "high_priority_last_error": high_priority_state.get("last_error") or "",
        "high_priority_default_station_limit": settings.high_priority_deep_dive_station_limit,
        "scan_focus_enabled": scan_focus_enabled,
        "scan_focus_market": scan_focus_market,
        "scan_focus_updated_at": scan_focus_state.get("updated_at") or "-",
        "scan_focus_updated_by": scan_focus_state.get("updated_by") or "-",
        "scan_focus_country_count": 0 if scan_focus_countries is None else len(scan_focus_countries),
        "scan_focus_eligible_total": focus_eligible_total,
        "scan_focus_processed_ever": focus_processed_ever,
        "scan_focus_pending": focus_pending,
        "scan_focus_progress_ratio": focus_progress_ratio,
        "service_active": _run_command(["systemctl", "is-active", "radio-db.service"]),
        "timer_active": _run_command(["systemctl", "is-active", "radio-db.timer"]),
        "timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db.timer"]),
        "timer_next": _run_command(
            ["systemctl", "list-timers", "radio-db.timer", "--no-pager", "--all", "--legend=0"]
        ),
        "internal_service_active": _run_command(["systemctl", "is-active", "radio-db-internal.service"]),
        "internal_timer_active": _run_command(["systemctl", "is-active", "radio-db-internal.timer"]),
        "internal_timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-internal.timer"]),
        "internal_timer_next": _run_command(
            ["systemctl", "list-timers", "radio-db-internal.timer", "--no-pager", "--all", "--legend=0"]
        ),
        "people_service_active": _run_command(["systemctl", "is-active", "radio-db-people.service"]),
        "people_timer_active": _run_command(["systemctl", "is-active", "radio-db-people.timer"]),
        "people_timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-people.timer"]),
        "people_timer_next": _run_command(
            ["systemctl", "list-timers", "radio-db-people.timer", "--no-pager", "--all", "--legend=0"]
        ),
        "verify_service_active": _run_command(["systemctl", "is-active", "radio-db-verify.service"]),
        "verify_timer_active": _run_command(["systemctl", "is-active", "radio-db-verify.timer"]),
        "verify_timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-verify.timer"]),
        "verify_timer_next": _run_command(
            ["systemctl", "list-timers", "radio-db-verify.timer", "--no-pager", "--all", "--legend=0"]
        ),
        "openai_reconcile_service_active": _run_command(["systemctl", "is-active", "radio-db-openai-reconcile.service"]),
        "openai_reconcile_timer_active": _run_command(["systemctl", "is-active", "radio-db-openai-reconcile.timer"]),
        "openai_reconcile_timer_enabled": _run_command(["systemctl", "is-enabled", "radio-db-openai-reconcile.timer"]),
        "openai_reconcile_timer_next": _run_command(
            ["systemctl", "list-timers", "radio-db-openai-reconcile.timer", "--no-pager", "--all", "--legend=0"]
        ),
        "running_processes": _run_command(
            [
                "bash",
                "-lc",
                "ps -eo pid,user,etimes,cmd --sort=-etimes | rg -i 'radio-db|run_cycle.sh|run_verify_cycle.sh|uvicorn' | head -n 20 || true",
            ]
        ),
        "recent_errors": _run_command(
            ["journalctl", "-u", "radio-db.service", "-n", "30", "-p", "err", "--no-pager", "--output=short-iso"]
        ),
    }


def _agent_snapshot(session: Session) -> dict[str, Any]:
    env_map = _load_env_map()
    manual_state = _agent_manual_state()
    intelligence = load_country_discovery_intelligence(limit=25)
    country_state = intelligence.get("state", {}) if isinstance(intelligence, dict) else {}
    memory = intelligence.get("memory", {}) if isinstance(intelligence, dict) else {}
    recent_runs = intelligence.get("recent_runs", []) if isinstance(intelligence, dict) else []
    countries = [str(c).upper() for c in country_state.get("countries", []) if str(c).strip()]
    idx = int(country_state.get("index", 0)) if countries else 0
    next_country = countries[idx % len(countries)] if countries else "-"
    last_run = recent_runs[-1] if recent_runs else {}

    last_cycle_evidence = session.scalar(
        select(Evidence)
        .where(Evidence.source_id == "country_discovery_cycle")
        .order_by(Evidence.created_at.desc())
        .limit(1)
    )
    cycle_24h = session.scalar(
        select(func.count(Evidence.id)).where(
            Evidence.source_id == "country_discovery_cycle",
            Evidence.created_at >= datetime.utcnow() - timedelta(hours=24),
        )
    ) or 0

    country_timer_next = _run_command(
        ["systemctl", "list-timers", "radio-db-country-discovery.timer", "--no-pager", "--all", "--legend=0"]
    )
    country_timer_active = _run_command(["systemctl", "is-active", "radio-db-country-discovery.timer"])
    country_timer_enabled = _run_command(["systemctl", "is-enabled", "radio-db-country-discovery.timer"])
    country_service_active = _run_command(["systemctl", "is-active", "radio-db-country-discovery.service"])
    form_service_active = _run_command(["systemctl", "is-active", "radio-db-country-form-cycle.service"])
    running_processes = _run_command(
        [
            "bash",
            "-lc",
            "ps -eo pid,user,etimes,cmd --sort=-etimes | rg -i 'run_country_discovery_cycle|run-country-discovery-cycle|scan-submission-forms|run-country-form-cycle' | head -n 20 || true",
        ]
    )
    return {
        "now_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "country_cursor_index": idx,
        "country_count": len(countries),
        "countries": countries,
        "next_country": next_country,
        "query_cursor": country_state.get("query_cursor", {}),
        "country_state_updated_at": country_state.get("updated_at", "-"),
        "last_plan": last_run.get("plan", {}),
        "last_review": last_run.get("review", {}),
        "last_per_template": last_run.get("per_template", {}),
        "recent_runs": recent_runs[-10:],
        "memory_countries": memory.get("countries", {}) if isinstance(memory, dict) else {},
        "cycle_events_24h": cycle_24h,
        "last_cycle_at": getattr(last_cycle_evidence, "created_at", None),
        "last_cycle_title": getattr(last_cycle_evidence, "raw_title", None),
        "last_cycle_url": getattr(last_cycle_evidence, "source_url", None),
        "manual_state": manual_state,
        "country_service_active": country_service_active,
        "country_timer_active": country_timer_active,
        "country_timer_enabled": country_timer_enabled,
        "country_timer_next": country_timer_next,
        "form_service_active": form_service_active,
        "running_processes": running_processes,
        "agent_mode": (env_map.get("AGENT_MODE", "on") or "on").strip().lower(),
        "env": {k: env_map.get(k, "") for k in AGENT_CONFIG_KEYS},
    }


def _history_channel_label(ev: Evidence) -> str:
    source_id = (ev.source_id or "").strip().lower()
    source_type = ev.source_type.value if ev.source_type else "unknown"
    raw_title = (ev.raw_title or "").strip().lower()

    if source_id in {"country_discovery_cycle", "station_enrich_search", "station_enrich_touch"}:
        return "enrichment_cycle"
    if source_id == "quality_verify":
        return "verify_cycle"
    if "internal station source" in raw_title:
        return "internal_scan"
    if source_type in {"brave_search", "google_cse", "tavily", "grok_search", "linkup", "duckduckgo"}:
        return "external_search"
    if source_type == "website":
        return "website_crawl"
    return "other"


def _payload_summary(raw_payload: str | None) -> str:
    if not raw_payload:
        return "-"
    try:
        data = json.loads(raw_payload)
        if isinstance(data, dict):
            keys = sorted(str(k) for k in data.keys())
            if not keys:
                return "{}"
            return ", ".join(keys[:8]) + (" ..." if len(keys) > 8 else "")
        if isinstance(data, list):
            return f"list[{len(data)}]"
    except Exception:
        pass
    compact = " ".join(raw_payload.split())
    return compact[:140] + ("..." if len(compact) > 140 else "")


def _read_api_call_history(limit: int, hours: int) -> list[dict[str, Any]]:
    path = Path(".radio_db_state") / "api_call_history.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts_raw = str(row.get("ts_utc") or "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            else:
                ts = ts.astimezone(UTC)
        except Exception:
            ts = None
        if ts is not None and ts < cutoff:
            break
        out.append(row)
        if len(out) >= limit:
            break
    return out


def create_app() -> FastAPI:
    init_db()
    _ensure_dashboard_schema()
    app = FastAPI(title="Radio DB Dashboard")
    cors_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(stations_api_router)
    app.include_router(agent_api_router)
    app.include_router(browser_api_router)
    app.include_router(forms_api_router)
    app.include_router(contacts_api_router)
    app.include_router(control_api_router)
    app.include_router(contact_center_api_router)
    app.include_router(outreach_campaigns_api_router)
    app.include_router(data_control_api_router)
    app.include_router(station_groups_api_router)
    frontend_assets_dir = _frontend_dist_dir() / "assets"
    if frontend_assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(frontend_assets_dir)), name="spa-assets")

    @app.get("/api/v1/stats")
    def api_stats() -> dict[str, Any]:
        with SessionLocal() as db:
            return pipeline_stats(db)

    @app.get("/agent-artifact")
    def agent_artifact(path: str = Query(...)) -> FileResponse:
        resolved = Path(path).resolve()
        state_root = Path(".radio_db_state").resolve()
        snapshot_root = Path(settings.browser_snapshot_dir).resolve()
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="artifact_not_found")
        if resolved != state_root and state_root not in resolved.parents and snapshot_root not in resolved.parents:
            raise HTTPException(status_code=404, detail="artifact_not_found")
        return FileResponse(resolved)

    @app.get("/agent-lab/start")
    def agent_lab_start(
        station_id: int = Query(..., ge=1),
        target_url: str = Query(default=""),
        max_pages: int = Query(default=1, ge=1, le=5),
        force_rescan: bool = Query(default=False),
    ) -> RedirectResponse:
        href = "/agent-lab?" + urlencode({"station_id": station_id})
        return _read_only_redirect(href, msg_key="legacy_msg")

    @app.get("/agent-lab", response_class=HTMLResponse)
    def agent_lab(
        station_id: int | None = Query(default=None, ge=1),
        run_id: int | None = Query(default=None, ge=1),
        q: str = Query(default=""),
        max_station_results: int = Query(default=25, ge=1, le=100),
        legacy_msg: str = Query(default=""),
    ) -> str:
        q = q.strip()
        with SessionLocal() as session:
            station_results = session.scalars(
                _station_query(
                    session=session,
                    q=q,
                    country="",
                    status="",
                    genre="",
                    has_submission=None,
                    has_people=None,
                    min_confidence=0.0,
                )
                .order_by(Station.updated_at.desc())
                .limit(max_station_results)
            ).all()
            selected_station = None
            if station_id is not None:
                selected_station = session.scalar(select(Station).where(Station.id == station_id))
            elif station_results:
                selected_station = station_results[0]
                station_id = selected_station.id

            run_snapshot = None
            selected_run = None
            if run_id is not None:
                run_snapshot = agent_run_snapshot(session, run_id)
                selected_run = run_snapshot["run"] if run_snapshot else None
                if selected_station is None and run_snapshot:
                    selected_station = run_snapshot["station"]
                    station_id = selected_station.id

            recent_runs: list[SubmissionAgentRun] = []
            station_forms: list[SubmissionForm] = []
            station_assessment: StationSubmissionAssessment | None = None
            recent_recipes: list[FormRecipe] = []
            station_submissions: list[SubmissionChannel] = []
            if selected_station is not None:
                if selected_run is None:
                    selected_run = session.scalar(
                        select(SubmissionAgentRun)
                        .where(SubmissionAgentRun.station_id == selected_station.id)
                        .order_by(SubmissionAgentRun.started_at.desc())
                    )
                    if selected_run is not None:
                        run_snapshot = agent_run_snapshot(session, selected_run.id)
                recent_runs = session.scalars(
                    select(SubmissionAgentRun)
                    .where(SubmissionAgentRun.station_id == selected_station.id)
                    .order_by(SubmissionAgentRun.started_at.desc())
                    .limit(12)
                ).all()
                station_forms = session.scalars(
                    select(SubmissionForm)
                    .where(SubmissionForm.station_id == selected_station.id)
                    .order_by(SubmissionForm.updated_at.desc())
                    .limit(12)
                ).all()
                station_assessment = session.scalar(
                    select(StationSubmissionAssessment)
                    .where(
                        StationSubmissionAssessment.station_id == selected_station.id,
                        StationSubmissionAssessment.assessment_kind == "manual_scan",
                    )
                    .order_by(StationSubmissionAssessment.updated_at.desc())
                )
                station_submissions = session.scalars(
                    select(SubmissionChannel)
                    .where(SubmissionChannel.station_id == selected_station.id)
                    .order_by(SubmissionChannel.id.desc())
                    .limit(20)
                ).all()
                if station_forms:
                    form_ids = [form.id for form in station_forms]
                    recent_recipes = session.scalars(
                        select(FormRecipe)
                        .where(FormRecipe.form_id.in_(form_ids))
                        .order_by(FormRecipe.created_at.desc())
                        .limit(20)
                    ).all()

        station_rows = []
        for station in station_results:
            href = "/agent-lab?" + urlencode({"station_id": station.id, "q": q})
            station_rows.append(
                "<tr>"
                f"<td>{station.id}</td>"
                f"<td><a href=\"{escape(href)}\">{escape(station.canonical_name)}</a></td>"
                f"<td>{escape(station.country_code or '-')}</td>"
                f"<td>{station.status.value}</td>"
                f"<td>{station.confidence_score:.2f}</td>"
                f"<td>{escape(station.website_url or '-')}</td>"
                "</tr>"
            )

        selected_card = ""
        start_controls = ""
        submissions_html = "<tr><td colspan='4'>Keine Submission-Channels</td></tr>"
        forms_html = "<tr><td colspan='6'>Keine bekannten Formulare</td></tr>"
        recipes_html = "<tr><td colspan='5'>Keine Recipes</td></tr>"
        runs_html = "<tr><td colspan='6'>Keine Agent-Runs</td></tr>"
        assessment_block = "<div class='mono'>Noch keine Bewertung vorhanden.</div>"

        if selected_station is not None:
            selected_card = (
                "<div class='card'>"
                "<div class='section-title'>Ausgewaehlter Sender</div>"
                f"<div class='hero'>{escape(selected_station.canonical_name)}</div>"
                f"<div class='muted'>ID {selected_station.id} | {escape(selected_station.country_code or '-')} | "
                f"{selected_station.status.value} | Confidence {selected_station.confidence_score:.2f}</div>"
                f"<div class='muted' style='margin-top:6px;'>Website: {escape(selected_station.website_url or '-')}</div>"
                "</div>"
            )
            start_controls = f"""
            <form class="inline-form" action="/agent-lab/start" method="get">
              <input type="hidden" name="station_id" value="{selected_station.id}" />
              <div>
                <label>Ziel-URL</label>
                <input name="target_url" placeholder="optional konkrete Submission-URL" />
              </div>
              <div>
                <label>Pages</label>
                <input type="number" name="max_pages" min="1" max="5" value="1" />
              </div>
              <div>
                <label>Rescan</label>
                <select name="force_rescan">
                  <option value="false">false</option>
                  <option value="true">true</option>
                </select>
              </div>
              <div><button type="submit">Manuellen Scan Starten</button></div>
            </form>
            """
            if station_assessment is not None:
                assessment_payload = _parse_json(station_assessment.evidence_json, {})
                assessment_block = (
                    "<div class='grid three'>"
                    f"<div class='mini-card'><div class='k'>Real Station</div><div class='v'>{'yes' if station_assessment.is_real_station else 'no'}</div></div>"
                    f"<div class='mini-card'><div class='k'>Editorial Surface</div><div class='v'>{'yes' if station_assessment.has_real_editorial_surface else 'no'}</div></div>"
                    f"<div class='mini-card'><div class='k'>Music Submission</div><div class='v'>{'yes' if station_assessment.accepts_music_submissions else 'no'}</div></div>"
                    f"<div class='mini-card'><div class='k'>New Artists</div><div class='v'>{'yes' if station_assessment.accepts_new_artists else 'no'}</div></div>"
                    f"<div class='mini-card'><div class='k'>Readiness</div><div class='v'>{station_assessment.automation_readiness:.2f}</div></div>"
                    f"<div class='mini-card'><div class='k'>Risk</div><div class='v'>{station_assessment.risk_score:.2f}</div></div>"
                    "</div>"
                    f"<div class='mono' style='margin-top:10px;'>{escape(station_assessment.notes or '-')}</div>"
                    f"<div class='mono' style='margin-top:10px;'>{escape(json.dumps(assessment_payload, indent=2, ensure_ascii=False))}</div>"
                )
            submissions_html = "".join(
                "<tr>"
                f"<td>{sub.method.value}</td>"
                f"<td>{escape(sub.url or '-')}</td>"
                f"<td>{escape(sub.email or '-')}</td>"
                f"<td>{'yes' if sub.accepts_newcomers else 'no'}</td>"
                "</tr>"
                for sub in station_submissions
            ) or submissions_html
            forms_html = "".join(
                "<tr>"
                f"<td>{form.id}</td>"
                f"<td>{escape(form.form_type.value)}</td>"
                f"<td>{escape(form.status.value)}</td>"
                f"<td>{form.confidence:.2f}</td>"
                f"<td>{escape(form.url)}</td>"
                f"<td>{escape(form.snapshot_path or '-')}</td>"
                "</tr>"
                for form in station_forms
            ) or forms_html
            recipes_html = "".join(
                "<tr>"
                f"<td>{recipe.id}</td>"
                f"<td>{recipe.form_id}</td>"
                f"<td>{recipe.version}</td>"
                f"<td>{escape(recipe.mode)}</td>"
                f"<td>{recipe.confidence_score:.2f}</td>"
                "</tr>"
                for recipe in recent_recipes
            ) or recipes_html
            runs_html = "".join(
                "<tr>"
                f"<td><a href=\"/agent-lab?{escape(urlencode({'station_id': selected_station.id, 'run_id': run.id, 'q': q}))}\">{run.id}</a></td>"
                f"<td>{escape(run.mode)}</td>"
                f"<td>{escape(run.status)}</td>"
                f"<td>{escape(run.current_state)}</td>"
                f"<td>{run.confidence:.2f}</td>"
                f"<td>{escape(str(run.started_at))}</td>"
                "</tr>"
                for run in recent_runs
            ) or runs_html

        run_panel = "<div class='card'><div class='section-title'>Run Detail</div><div class='mono'>Noch kein Run ausgewaehlt.</div></div>"
        if run_snapshot is not None:
            run = run_snapshot["run"]
            steps: list[SubmissionAgentStep] = run_snapshot["steps"]
            issues: list[SubmissionAgentIssue] = run_snapshot["issues"]
            form = run_snapshot["form"]
            latest_step = steps[-1] if steps else None
            screenshot_html = "<div class='empty-shot'>Kein Screenshot vorhanden.</div>"
            if latest_step and latest_step.screenshot_path:
                screenshot_html = (
                    f"<img class='shot' src=\"{escape(_artifact_href(latest_step.screenshot_path))}\" "
                    f"alt=\"run screenshot {run.id}\" />"
                )
            step_rows = "".join(
                "<tr>"
                f"<td>{step.step_index}</td>"
                f"<td>{escape(step.state_before)}</td>"
                f"<td>{escape(step.state_after)}</td>"
                f"<td>{step.confidence:.2f}</td>"
                f"<td>{step.latency_ms}</td>"
                "</tr>"
                for step in steps
            ) or "<tr><td colspan='5'>Keine Steps</td></tr>"
            issue_rows = "".join(
                "<tr>"
                f"<td>{escape(issue.severity)}</td>"
                f"<td>{escape(issue.issue_type)}</td>"
                f"<td>{escape(issue.title)}</td>"
                f"<td>{escape(issue.details or '-')}</td>"
                "</tr>"
                for issue in issues
            ) or "<tr><td colspan='4'>Keine Issues</td></tr>"
            latest_observation = _pretty_json(latest_step.agent_observation_json if latest_step else None)
            latest_action = _pretty_json(latest_step.proposed_action_json if latest_step else None)
            latest_result = _pretty_json(latest_step.execution_result_json if latest_step else None)
            run_panel = f"""
            <div class="card">
              <div class="section-title">Run Detail</div>
              <div class="hero">Run #{run.id}</div>
              <div class="muted">Mode {escape(run.mode)} | Status {escape(run.status)} | State {escape(run.current_state)} | Confidence {run.confidence:.2f}</div>
              <div class="muted" style="margin-top:6px;">Target URL: {escape(run.target_url or '-')}</div>
              <div class="muted" style="margin-top:6px;">Known Form: {form.id if form else '-'} {escape(form.url if form else '')}</div>
              <div class="shot-wrap">{screenshot_html}</div>
            </div>
            <div class="split">
              <div class="card">
                <div class="section-title">Steps</div>
                <div style="overflow:auto; max-height:260px;">
                  <table>
                    <thead><tr><th>#</th><th>Before</th><th>After</th><th>Conf</th><th>ms</th></tr></thead>
                    <tbody>{step_rows}</tbody>
                  </table>
                </div>
              </div>
              <div class="card">
                <div class="section-title">Issues</div>
                <div style="overflow:auto; max-height:260px;">
                  <table>
                    <thead><tr><th>Severity</th><th>Type</th><th>Title</th><th>Details</th></tr></thead>
                    <tbody>{issue_rows}</tbody>
                  </table>
                </div>
              </div>
            </div>
            <div class="split">
              <div class="card"><div class="section-title">Observation</div><div class="mono">{escape(latest_observation)}</div></div>
              <div class="card"><div class="section-title">Proposed Action</div><div class="mono">{escape(latest_action)}</div></div>
            </div>
            <div class="card"><div class="section-title">Execution Result</div><div class="mono">{escape(latest_result)}</div></div>
            """

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Radio DB Agent Lab</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: rgba(255,255,255,0.92);
      --ink: #142116;
      --muted: #5a6b5e;
      --line: #d6ddd1;
      --accent: #0f6d48;
      --soft: #eef4ec;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(150deg, #edf3ec, #f8f1e6); color: var(--ink); }}
    .wrap {{ max-width: 1500px; margin: 18px auto; padding: 0 16px 24px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .topbar {{ display:flex; justify-content:space-between; align-items:end; gap:12px; margin-bottom:14px; }}
    .title {{ font-size: 28px; font-weight: 700; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .grid {{ display:grid; grid-template-columns: 340px 1fr; gap:12px; }}
    .split {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; margin-top:12px; }}
    .three {{ grid-template-columns: repeat(3, minmax(120px, 1fr)); }}
    .card {{ background: var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow: 0 10px 24px rgba(16,32,21,0.04); }}
    .section-title {{ font-size:12px; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin-bottom:10px; }}
    .hero {{ font-size:22px; font-weight:700; }}
    .inline-form, .search-form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:end; }}
    label {{ display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }}
    input, select, button {{ border:1px solid #bcc8bd; border-radius:10px; padding:9px 10px; background:#fff; font: inherit; }}
    button {{ background: var(--accent); color:#fff; border:0; cursor:pointer; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; border-bottom:1px solid #e3e9df; padding:8px 7px; font-size:13px; vertical-align:top; }}
    th {{ position:sticky; top:0; background:var(--soft); }}
    .mono {{ white-space:pre-wrap; font-family:"IBM Plex Mono","Consolas",monospace; font-size:12px; line-height:1.45; background:#f9fbf7; border:1px solid #e3e9df; border-radius:10px; padding:10px; }}
    .mini-card {{ background:#f8fbf6; border:1px solid #e1e8dc; border-radius:10px; padding:10px; }}
    .k {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; }}
    .v {{ font-size:18px; font-weight:650; }}
    .shot-wrap {{ margin-top:14px; background:#ecf1ea; border:1px dashed #bfcbbf; border-radius:12px; padding:12px; min-height:260px; display:flex; align-items:flex-start; justify-content:center; }}
    .shot {{ width:100%; border-radius:12px; border:1px solid #cfd8cc; background:#fff; }}
    .empty-shot {{ color:var(--muted); font-size:13px; align-self:center; }}
    @media (max-width: 1100px) {{ .grid, .split, .three {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <div class="title">Agent Lab</div>
        <div class="muted">Manuelles Scan-Cockpit fuer visuelle Formularpruefung. Keine autonomen Submits.</div>
        {"<div class='muted' style='color:#7a1f2d;margin-top:6px;'>Legacy ist read-only. Starte neue Runs im SPA-Tab Agent Control.</div>" if legacy_msg == "read_only" else ""}
      </div>
      <div class="muted"><a href="/agent">Zum Agent Dashboard</a> | <a href="/legacy">Zum Legacy-Dashboard</a></div>
    </div>
    <div class="grid">
      <div>
        <div class="card">
          <div class="section-title">Senderauswahl</div>
          <form class="search-form" method="get" action="/agent-lab">
            <div style="flex:1 1 180px;">
              <label>Suche</label>
              <input name="q" value="{escape(q)}" placeholder="Name, Website, Stadt" style="width:100%;" />
            </div>
            <div>
              <label>Limit</label>
              <input type="number" min="1" max="100" name="max_station_results" value="{max_station_results}" />
            </div>
            <div><button type="submit">Suchen</button></div>
          </form>
        </div>
        {selected_card}
        <div class="card">
          <div class="section-title">Manueller Testlauf</div>
          <div class="muted" style="margin-bottom:8px;">Scannt Kandidat-URLs, speichert Formprofile und zeigt Screenshot plus Issues.</div>
          {start_controls or '<div class="mono">Bitte zuerst einen Sender auswaehlen.</div>'}
        </div>
        <div class="card">
          <div class="section-title">Treffer</div>
          <div style="overflow:auto; max-height:420px;">
            <table>
              <thead><tr><th>ID</th><th>Name</th><th>Land</th><th>Status</th><th>Conf</th><th>Website</th></tr></thead>
              <tbody>{''.join(station_rows) if station_rows else "<tr><td colspan='6'>Keine Treffer</td></tr>"}</tbody>
            </table>
          </div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="section-title">Stationsbewertung</div>
          {assessment_block}
        </div>
        {run_panel}
        <div class="split">
          <div class="card">
            <div class="section-title">Known Forms</div>
            <div style="overflow:auto; max-height:260px;">
              <table>
                <thead><tr><th>ID</th><th>Type</th><th>Status</th><th>Conf</th><th>URL</th><th>Snapshot</th></tr></thead>
                <tbody>{forms_html}</tbody>
              </table>
            </div>
          </div>
          <div class="card">
            <div class="section-title">Submission Channels</div>
            <div style="overflow:auto; max-height:260px;">
              <table>
                <thead><tr><th>Method</th><th>URL</th><th>Email</th><th>Newcomers</th></tr></thead>
                <tbody>{submissions_html}</tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="split">
          <div class="card">
            <div class="section-title">Recent Recipes</div>
            <div style="overflow:auto; max-height:220px;">
              <table>
                <thead><tr><th>ID</th><th>Form</th><th>Ver</th><th>Mode</th><th>Conf</th></tr></thead>
                <tbody>{recipes_html}</tbody>
              </table>
            </div>
          </div>
          <div class="card">
            <div class="section-title">Recent Runs</div>
            <div style="overflow:auto; max-height:220px;">
              <table>
                <thead><tr><th>ID</th><th>Mode</th><th>Status</th><th>State</th><th>Conf</th><th>Started</th></tr></thead>
                <tbody>{runs_html}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""

    @app.get("/boost/start")
    def boost_start(
        budget_usd: float = Query(default=settings.brave_boost_default_budget_usd, ge=0.1, le=100.0),
        market_focus: str = Query(default="international"),
    ) -> RedirectResponse:
        return _read_only_redirect("/legacy", msg_key="boost_msg")

    @app.get("/scan-focus/set")
    def scan_focus_set(mode: str = Query(default="off")) -> RedirectResponse:
        return _read_only_redirect("/legacy", msg_key="scan_msg")

    @app.get("/high-priority/start")
    def high_priority_start(
        station_limit: int = Query(default=settings.high_priority_deep_dive_station_limit, ge=1, le=50),
    ) -> RedirectResponse:
        return _read_only_redirect("/legacy", msg_key="hp_msg")

    @app.get("/station/manual-confirm")
    def station_manual_confirm(
        station_id: int = Query(..., ge=1),
        value: str = Query(default="true"),
        next_url: str = Query(default="/"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/") else "/"
        return _read_only_redirect(target)

    @app.get("/station/delete")
    def station_delete(
        station_id: int = Query(..., ge=1),
        next_url: str = Query(default="/"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/") else "/"
        return _read_only_redirect(target)

    @app.get("/email/manual-confirm")
    def email_manual_confirm(
        source: str = Query(...),
        entry_id: int = Query(..., ge=1),
        value: str = Query(default="true"),
        next_url: str = Query(default="/email"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/email") else "/email"
        return _read_only_redirect(target, msg_key="email_msg")

    @app.get("/agent/run-country-now")
    def agent_run_country_now() -> RedirectResponse:
        return _read_only_redirect("/agent", msg_key="agent_msg")

    @app.get("/agent/mode/set")
    def agent_mode_set(mode: str = Query(default="on")) -> RedirectResponse:
        return _read_only_redirect("/agent", msg_key="agent_msg")

    @app.get("/agent/config/save")
    def agent_config_save(
        priority_countries: str = Query(default=""),
        country_discovery_search_mode: str = Query(default="openai_first"),
        country_discovery_auto_translate_queries: str = Query(default="true"),
        query_translate_model: str = Query(default="gpt-4.1-mini"),
        country_discovery_max_queries_per_run: int = Query(default=8, ge=1, le=100),
        country_discovery_max_results_per_query: int = Query(default=15, ge=1, le=50),
        country_discovery_min_confidence: float = Query(default=0.35, ge=0.0, le=1.0),
        country_discovery_include_linkup: str = Query(default="true"),
        enable_openai_web_search: str = Query(default="true"),
        openai_web_search_model: str = Query(default="gpt-4.1-mini"),
        max_openai_web_calls_per_day: int = Query(default=300, ge=0, le=200000),
        max_openai_web_calls_per_month: int = Query(default=9000, ge=0, le=5000000),
        max_daily_usd: float = Query(default=3.0, ge=0.0, le=1000.0),
        max_llm_calls_per_day: int = Query(default=400, ge=1, le=50000),
        max_llm_calls_per_run: int = Query(default=25, ge=1, le=5000),
        max_llm_calls_per_domain_per_run: int = Query(default=2, ge=1, le=100),
        max_page_fetches_per_run: int = Query(default=60, ge=1, le=20000),
        max_codex_calls_per_day: int = Query(default=120, ge=1, le=50000),
        max_codex_daily_usd: float = Query(default=5.0, ge=0.0, le=1000.0),
        browser_worker_enabled: str = Query(default="true"),
        browser_max_stations_per_run: int = Query(default=50, ge=1, le=10000),
        browser_max_forms_per_station: int = Query(default=5, ge=1, le=50),
    ) -> RedirectResponse:
        return _read_only_redirect("/agent", msg_key="agent_msg")

    @app.get("/agent", response_class=HTMLResponse)
    def agent_dashboard(agent_msg: str = Query(default="")) -> str:
        with SessionLocal() as session:
            monitor = _monitor_snapshot(session)
            agent = _agent_snapshot(session)

        search_mode = (agent["env"].get("COUNTRY_DISCOVERY_SEARCH_MODE", "openai_first") or "openai_first").lower()
        auto_translate_true = (
            agent["env"].get("COUNTRY_DISCOVERY_AUTO_TRANSLATE_QUERIES", "true").lower() == "true"
        )
        include_linkup_true = agent["env"].get("COUNTRY_DISCOVERY_INCLUDE_LINKUP", "true").lower() == "true"
        openai_web_true = agent["env"].get("ENABLE_OPENAI_WEB_SEARCH", "true").lower() == "true"
        browser_enabled_true = agent["env"].get("BROWSER_WORKER_ENABLED", "true").lower() == "true"
        countries_text = ", ".join(agent["countries"]) if agent["countries"] else "-"
        manual = agent["manual_state"]
        agent_mode = (agent.get("agent_mode") or "on").strip().lower()
        output = escape(str(manual.get("last_output") or "-"))
        last_error = escape(str(manual.get("last_error") or "-"))
        last_plan = agent.get("last_plan", {}) or {}
        last_review = agent.get("last_review", {}) or {}
        last_per_template = agent.get("last_per_template", {}) or {}
        plan_text = escape(json.dumps(last_plan, indent=2, ensure_ascii=False)) if last_plan else "-"
        review_text = escape(json.dumps(last_review, indent=2, ensure_ascii=False)) if last_review else "-"
        template_lines = []
        for name, stats in sorted(last_per_template.items()):
            template_lines.append(
                f"{name}: q={stats.get('queries',0)} results={stats.get('results',0)} new={stats.get('new_stations',0)} dup={stats.get('duplicates',0)} noise={stats.get('skipped_noise',0)} existing={stats.get('skipped_existing',0)}"
            )
        template_text = escape("\n".join(template_lines)) if template_lines else "-"
        recent_rows = []
        for row in reversed(agent.get("recent_runs", [])[-10:]):
            recent_rows.append(
                "<tr>"
                f"<td>{escape(str(row.get('run_at', '-')))}</td>"
                f"<td>{escape(str(row.get('country', '-')))}</td>"
                f"<td>{int(row.get('queries', 0) or 0)}</td>"
                f"<td>{int(row.get('results', 0) or 0)}</td>"
                f"<td>{int(row.get('new_stations', 0) or 0)}</td>"
                f"<td>{escape(str((row.get('review') or {}).get('quality_grade', '-')))}</td>"
                f"<td>{float((row.get('review') or {}).get('novelty_rate', 0.0) or 0.0):.2%}</td>"
                "</tr>"
            )
        recent_table = "".join(recent_rows) if recent_rows else "<tr><td colspan='7'>Keine Läufe</td></tr>"

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="20" />
  <title>Radio DB Agent Dashboard</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: #ffffff;
      --ink: #102015;
      --muted: #516356;
      --line: #d8dfd3;
      --accent: #126d47;
      --warn: #7a1f2d;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(140deg, #eef4ec, #f9f3eb); color: var(--ink); }}
    .wrap {{ max-width: 1400px; margin: 22px auto; padding: 0 16px; }}
    .topnav {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .topnav a {{ text-decoration:none; color:#fff; background: var(--accent); padding:8px 12px; border-radius:8px; font-size:13px; }}
    .topnav a.secondary {{ background:#42584a; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(220px, 1fr)); gap:10px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .k {{ font-size: 12px; color: var(--muted); }}
    .v {{ font-size: 20px; font-weight: 650; }}
    .split {{ display:grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top:10px; }}
    label {{ font-size: 12px; color: var(--muted); display:block; }}
    input, select, button, textarea {{ border: 1px solid #bfcabf; border-radius: 8px; padding: 8px; background: #fff; width: 100%; box-sizing: border-box; }}
    button {{ background: var(--accent); color: #fff; border: 0; cursor: pointer; width:auto; }}
    .mono {{ font-family: "IBM Plex Mono", "Consolas", monospace; font-size: 12px; white-space: pre-wrap; }}
    .form-grid {{ display:grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap:8px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #e4eadf; text-align: left; padding: 6px 8px; font-size: 12px; }}
    th {{ background: #eef3e8; }}
    @media (max-width: 900px) {{ .grid, .split, .form-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topnav">
      <a href="/legacy">Radio DB</a>
      <a class="secondary" href="/email">E-Mail</a>
      <a class="secondary" href="/markets">Markets</a>
      <a class="secondary" href="/history">History</a>
      <a class="secondary" href="/agent">Agent Dashboard</a>
      <a class="secondary" href="/agent-lab">Agent Lab</a>
      <a href="/agent/run-country-now">Country Cycle Jetzt Starten</a>
    </div>
    <h1>Agent Dashboard</h1>
    <div class="k">Auto-Refresh alle 20s | UTC {agent["now_utc"]}</div>
    {"<div class='k' style='color:var(--accent);margin-top:6px;'>Country-Cycle manuell gestartet.</div>" if agent_msg == "started" else ""}
    {"<div class='k' style='color:var(--warn);margin-top:6px;'>Country-Cycle läuft bereits.</div>" if agent_msg == "already_running" else ""}
    {"<div class='k' style='color:var(--accent);margin-top:6px;'>Konfiguration gespeichert (wirksam ab nächstem Run).</div>" if agent_msg == "config_saved" else ""}
    {"<div class='k' style='color:var(--accent);margin-top:6px;'>Agent Mode gesetzt: OFF.</div>" if agent_msg == "mode_off" else ""}
    {"<div class='k' style='color:var(--accent);margin-top:6px;'>Agent Mode gesetzt: MINIMAL.</div>" if agent_msg == "mode_minimal" else ""}
    {"<div class='k' style='color:var(--accent);margin-top:6px;'>Agent Mode gesetzt: ON.</div>" if agent_msg == "mode_on" else ""}
    {"<div class='k' style='color:var(--warn);margin-top:6px;'>Legacy ist read-only. Verwende die SPA fuer Agent-Aktionen.</div>" if agent_msg == "read_only" else ""}

    <div class="grid">
      <div class="card"><div class="k">Agent Mode</div><div class="v">{escape(agent_mode)}</div></div>
      <div class="card"><div class="k">Country Timer</div><div class="v">{escape(agent["country_timer_active"])}</div></div>
      <div class="card"><div class="k">Country Timer Enabled</div><div class="v">{escape(agent["country_timer_enabled"])}</div></div>
      <div class="card"><div class="k">Country Service</div><div class="v">{escape(agent["country_service_active"])}</div></div>
      <div class="card"><div class="k">Next Country</div><div class="v">{escape(agent["next_country"])}</div></div>
      <div class="card"><div class="k">Countries in Queue</div><div class="v">{agent["country_count"]}</div></div>
      <div class="card"><div class="k">Cycle Events 24h</div><div class="v">{agent["cycle_events_24h"]}</div></div>
      <div class="card"><div class="k">Manual Run State</div><div class="v">{'running' if manual.get("is_running") else 'idle'}</div></div>
      <div class="card"><div class="k">LLM Budget Today</div><div class="v">${monitor["usd_spent_today_estimate"]:.3f} / ${monitor["max_daily_usd"]:.2f}</div></div>
    </div>

    <div class="card" style="margin-top:10px;">
      <div class="k">Agent Mode</div>
      <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;">
        <a href="/agent/mode/set?mode=off"><button type="button" style="background:{'#7a1f2d' if agent_mode == 'off' else '#42584a'};">OFF</button></a>
        <a href="/agent/mode/set?mode=minimal"><button type="button" style="background:{'#c27818' if agent_mode == 'minimal' else '#42584a'};">MINIMAL</button></a>
        <a href="/agent/mode/set?mode=on"><button type="button" style="background:{'#126d47' if agent_mode == 'on' else '#42584a'};">ON</button></a>
      </div>
      <div class="k" style="margin-top:8px;">
        off = keine Agent-Runs; minimal = seltene LLM-Calls (stark gedrosselt); on = normal.
      </div>
    </div>

    <div class="split">
      <div class="card">
        <div class="k">Fortschritt / Routing</div>
        <div class="mono">Countries: {escape(countries_text)}
Cursor index: {agent["country_cursor_index"]}
Country state updated: {escape(str(agent["country_state_updated_at"]))}
Last cycle evidence at: {escape(str(agent["last_cycle_at"] or "-"))}
Last cycle title: {escape(str(agent["last_cycle_title"] or "-"))}
Last cycle URL: {escape(str(agent["last_cycle_url"] or "-"))}
Timer next: {escape(agent["country_timer_next"] or "-")}
Running processes:
{escape(agent["running_processes"] or "-")}</div>
      </div>
      <div class="card">
        <div class="k">Manual Run Output</div>
        <div class="mono">Last started: {escape(str(manual.get("last_started_at") or "-"))}
Last finished: {escape(str(manual.get("last_finished_at") or "-"))}
Last exit: {escape(str(manual.get("last_exit_code") or "-"))}
Last error:
{last_error}
---
Last output:
{output}</div>
      </div>
    </div>

    <div class="split">
      <div class="card">
        <div class="k">Letzter Agent-Plan</div>
        <div class="mono">{plan_text}</div>
      </div>
      <div class="card">
        <div class="k">Letzte Selbstbewertung (Review)</div>
        <div class="mono">{review_text}</div>
      </div>
    </div>

    <div class="card" style="margin-top:10px;">
      <div class="k">Template-Performance letzter Lauf</div>
      <div class="mono">{template_text}</div>
    </div>

    <div class="card" style="margin-top:10px;">
      <div class="k">Letzte 10 Country-Runs</div>
      <div style="overflow:auto;">
        <table style="width:100%; border-collapse: collapse;">
          <thead><tr><th>Zeit</th><th>Land</th><th>Queries</th><th>Results</th><th>Neu</th><th>Grade</th><th>Novelty</th></tr></thead>
          <tbody>{recent_table}</tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-top:10px;">
      <div class="k">Kontingent & Drosselung</div>
      <form method="get" action="/agent/config/save" style="margin-top:8px;">
        <div class="form-grid">
          <div><label>PRIORITY_COUNTRIES</label><input name="priority_countries" value="{escape(agent["env"].get("PRIORITY_COUNTRIES", ""))}" /></div>
          <div><label>COUNTRY_DISCOVERY_SEARCH_MODE</label><select name="country_discovery_search_mode"><option value="openai_only" {"selected" if search_mode == "openai_only" else ""}>openai_only</option><option value="openai_first" {"selected" if search_mode == "openai_first" else ""}>openai_first</option><option value="external_only" {"selected" if search_mode == "external_only" else ""}>external_only</option></select></div>
          <div><label>COUNTRY_DISCOVERY_AUTO_TRANSLATE_QUERIES</label><select name="country_discovery_auto_translate_queries"><option value="true" {"selected" if auto_translate_true else ""}>true</option><option value="false" {"selected" if not auto_translate_true else ""}>false</option></select></div>
          <div><label>QUERY_TRANSLATE_MODEL</label><input name="query_translate_model" value="{escape(agent["env"].get("QUERY_TRANSLATE_MODEL", "gpt-4.1-mini"))}" /></div>
          <div><label>COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN</label><input type="number" min="1" max="100" name="country_discovery_max_queries_per_run" value="{escape(agent["env"].get("COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN", "8"))}" /></div>
          <div><label>COUNTRY_DISCOVERY_MAX_RESULTS_PER_QUERY</label><input type="number" min="1" max="50" name="country_discovery_max_results_per_query" value="{escape(agent["env"].get("COUNTRY_DISCOVERY_MAX_RESULTS_PER_QUERY", "15"))}" /></div>
          <div><label>COUNTRY_DISCOVERY_MIN_CONFIDENCE</label><input type="number" step="0.01" min="0" max="1" name="country_discovery_min_confidence" value="{escape(agent["env"].get("COUNTRY_DISCOVERY_MIN_CONFIDENCE", "0.35"))}" /></div>
          <div><label>COUNTRY_DISCOVERY_INCLUDE_LINKUP</label><select name="country_discovery_include_linkup"><option value="true" {"selected" if include_linkup_true else ""}>true</option><option value="false" {"selected" if not include_linkup_true else ""}>false</option></select></div>
          <div><label>ENABLE_OPENAI_WEB_SEARCH</label><select name="enable_openai_web_search"><option value="true" {"selected" if openai_web_true else ""}>true</option><option value="false" {"selected" if not openai_web_true else ""}>false</option></select></div>
          <div><label>OPENAI_WEB_SEARCH_MODEL</label><input name="openai_web_search_model" value="{escape(agent["env"].get("OPENAI_WEB_SEARCH_MODEL", "gpt-4.1-mini"))}" /></div>
          <div><label>MAX_OPENAI_WEB_CALLS_PER_DAY</label><input type="number" min="0" name="max_openai_web_calls_per_day" value="{escape(agent["env"].get("MAX_OPENAI_WEB_CALLS_PER_DAY", "300"))}" /></div>
          <div><label>MAX_OPENAI_WEB_CALLS_PER_MONTH</label><input type="number" min="0" name="max_openai_web_calls_per_month" value="{escape(agent["env"].get("MAX_OPENAI_WEB_CALLS_PER_MONTH", "9000"))}" /></div>
          <div><label>MAX_DAILY_USD</label><input type="number" step="0.1" min="0" name="max_daily_usd" value="{escape(agent["env"].get("MAX_DAILY_USD", "3.0"))}" /></div>
          <div><label>MAX_LLM_CALLS_PER_DAY</label><input type="number" min="1" name="max_llm_calls_per_day" value="{escape(agent["env"].get("MAX_LLM_CALLS_PER_DAY", "400"))}" /></div>
          <div><label>MAX_LLM_CALLS_PER_RUN</label><input type="number" min="1" name="max_llm_calls_per_run" value="{escape(agent["env"].get("MAX_LLM_CALLS_PER_RUN", "25"))}" /></div>
          <div><label>MAX_LLM_CALLS_PER_DOMAIN_PER_RUN</label><input type="number" min="1" name="max_llm_calls_per_domain_per_run" value="{escape(agent["env"].get("MAX_LLM_CALLS_PER_DOMAIN_PER_RUN", "2"))}" /></div>
          <div><label>MAX_PAGE_FETCHES_PER_RUN</label><input type="number" min="1" name="max_page_fetches_per_run" value="{escape(agent["env"].get("MAX_PAGE_FETCHES_PER_RUN", "60"))}" /></div>
          <div><label>MAX_CODEX_CALLS_PER_DAY</label><input type="number" min="1" name="max_codex_calls_per_day" value="{escape(agent["env"].get("MAX_CODEX_CALLS_PER_DAY", "120"))}" /></div>
          <div><label>MAX_CODEX_DAILY_USD</label><input type="number" step="0.1" min="0" name="max_codex_daily_usd" value="{escape(agent["env"].get("MAX_CODEX_DAILY_USD", "5.0"))}" /></div>
          <div><label>BROWSER_WORKER_ENABLED</label><select name="browser_worker_enabled"><option value="true" {"selected" if browser_enabled_true else ""}>true</option><option value="false" {"selected" if not browser_enabled_true else ""}>false</option></select></div>
          <div><label>BROWSER_MAX_STATIONS_PER_RUN</label><input type="number" min="1" name="browser_max_stations_per_run" value="{escape(agent["env"].get("BROWSER_MAX_STATIONS_PER_RUN", "50"))}" /></div>
          <div><label>BROWSER_MAX_FORMS_PER_STATION</label><input type="number" min="1" name="browser_max_forms_per_station" value="{escape(agent["env"].get("BROWSER_MAX_FORMS_PER_STATION", "5"))}" /></div>
        </div>
        <div style="margin-top:10px;"><button type="submit">Konfiguration Speichern</button></div>
      </form>
    </div>
  </div>
</body>
</html>"""

    @app.get("/history", response_class=HTMLResponse)
    def history_dashboard(
        limit: int = Query(default=200, ge=20, le=1000),
        hours: int = Query(default=72, ge=1, le=24 * 30),
        channel: str = Query(default=""),
        source_type: str = Query(default=""),
        provider: str = Query(default=""),
        api_status: str = Query(default=""),
        q: str = Query(default=""),
    ) -> str:
        since = datetime.utcnow() - timedelta(hours=hours)
        channel = channel.strip().lower()
        source_type = source_type.strip().lower()
        provider = provider.strip().lower()
        api_status = api_status.strip().lower()
        q = q.strip().lower()

        rows_html: list[str] = []
        api_rows_html: list[str] = []
        channel_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        provider_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        provider_perf: dict[str, dict[str, float]] = {}
        total_rows = 0
        total_api_rows = 0
        scan_total = 0
        scan_status_counts: dict[str, int] = {}
        scan_blocked_reason_counts: dict[str, int] = {}
        scan_total_pages = 0
        scan_total_forms = 0
        scan_total_emails = 0
        scan_total_duration_sec = 0.0
        scan_finished_count = 0
        scan_running_now = 0
        scan_review_open_now = 0
        recent_scan_rows: list[str] = []
        current_jobs_rows: list[str] = []
        scan_status_cards = ""
        blocked_reason_text = "-"

        with SessionLocal() as session:
            monitor = _monitor_snapshot(session)
            stmt = (
                select(Evidence, Station.canonical_name)
                .outerjoin(Station, Station.id == Evidence.station_id)
                .where(Evidence.created_at >= since)
                .order_by(Evidence.created_at.desc())
                .limit(limit * 3)
            )
            rows = session.execute(stmt).all()

            for ev, station_name in rows:
                ch = _history_channel_label(ev)
                stype = ev.source_type.value if ev.source_type else "unknown"
                channel_counts[ch] = channel_counts.get(ch, 0) + 1
                source_counts[stype] = source_counts.get(stype, 0) + 1

                if channel and ch != channel:
                    continue
                if source_type and stype != source_type:
                    continue

                search_blob = " ".join(
                    [
                        str(station_name or ""),
                        str(ev.source_id or ""),
                        str(ev.source_url or ""),
                        str(ev.raw_title or ""),
                        str(ev.raw_snippet or ""),
                    ]
                ).lower()
                if q and q not in search_blob:
                    continue

                total_rows += 1
                rows_html.append(
                    "<tr>"
                    f"<td>{escape(str(ev.created_at))}</td>"
                    f"<td>{escape(ch)}</td>"
                    f"<td>{escape(stype)}</td>"
                    f"<td>{escape(ev.source_id or '-')}</td>"
                    f"<td>{escape(station_name or '-')}</td>"
                    f"<td>{escape((ev.raw_title or '-')[:180])}</td>"
                    f"<td><a href=\"{escape(ev.source_url or '#')}\" target=\"_blank\" rel=\"noopener\">{escape((ev.source_url or '-')[:120])}</a></td>"
                    f"<td>{ev.confidence:.2f}</td>"
                    f"<td>{escape(_payload_summary(ev.extracted_payload_json))}</td>"
                    "</tr>"
                )
                if total_rows >= limit:
                    break

            run_stmt = (
                select(SubmissionAgentRun, Station.canonical_name)
                .outerjoin(Station, Station.id == SubmissionAgentRun.station_id)
                .where(
                    or_(
                        SubmissionAgentRun.started_at >= since,
                        SubmissionAgentRun.finished_at >= since,
                    )
                )
                .order_by(SubmissionAgentRun.started_at.desc())
                .limit(limit * 5)
            )
            run_rows = session.execute(run_stmt).all()
            for run, station_name in run_rows:
                scan_total += 1
                status = str(run.status or "unknown").strip().lower() or "unknown"
                scan_status_counts[status] = scan_status_counts.get(status, 0) + 1
                if status == "blocked":
                    reason = (run.blocked_reason or "unknown").strip() or "unknown"
                    scan_blocked_reason_counts[reason] = scan_blocked_reason_counts.get(reason, 0) + 1

                summary = _parse_json(run.summary_json, {})
                if isinstance(summary, dict):
                    scan_total_pages += int(summary.get("pages_scanned") or 0)
                    scan_total_forms += int(summary.get("forms_saved") or 0)
                    scan_total_emails += int(summary.get("emails_saved") or 0)

                if run.finished_at is not None and run.started_at is not None:
                    duration_sec = (run.finished_at - run.started_at).total_seconds()
                    if duration_sec >= 0:
                        scan_total_duration_sec += duration_sec
                        scan_finished_count += 1

                if len(recent_scan_rows) < 25:
                    duration_cell = "-"
                    if run.finished_at is not None and run.started_at is not None:
                        dur = (run.finished_at - run.started_at).total_seconds()
                        if dur >= 0:
                            duration_cell = f"{dur:.1f}s"
                    recent_scan_rows.append(
                        "<tr>"
                        f"<td>{run.id}</td>"
                        f"<td>{run.station_id}</td>"
                        f"<td>{escape(station_name or '-')}</td>"
                        f"<td>{escape(status)}</td>"
                        f"<td>{escape(run.current_state or '-')}</td>"
                        f"<td>{escape(run.blocked_reason or '-')}</td>"
                        f"<td>{run.confidence:.2f}</td>"
                        f"<td>{duration_cell}</td>"
                        f"<td>{escape(str(run.started_at or '-'))}</td>"
                        "</tr>"
                    )

            scan_running_now = session.scalar(
                select(func.count(SubmissionAgentRun.id)).where(SubmissionAgentRun.status == "running")
            ) or 0
            scan_review_open_now = session.scalar(
                select(func.count(SubmissionAgentRun.id)).where(
                    SubmissionAgentRun.status.in_(("running", "awaiting_review"))
                )
            ) or 0

        scan_status_cards = "".join(
            f"<div class='card'><div class='k'>Scan {escape(name)}</div><div class='v'>{count}</div></div>"
            for name, count in sorted(scan_status_counts.items(), key=lambda x: (-x[1], x[0]))[:6]
        ) or "<div class='card'><div class='k'>Scan Status</div><div class='v'>0</div></div>"

        if scan_blocked_reason_counts:
            blocked_reason_text = " | ".join(
                f"{name}: {count}"
                for name, count in sorted(scan_blocked_reason_counts.items(), key=lambda x: (-x[1], x[0]))[:8]
            )

        scan_awaiting_review = scan_status_counts.get("awaiting_review", 0)
        scan_blocked = scan_status_counts.get("blocked", 0)
        scan_success_rate = (100.0 * scan_awaiting_review / max(1, scan_total)) if scan_total else 0.0
        scan_blocked_rate = (100.0 * scan_blocked / max(1, scan_total)) if scan_total else 0.0
        scan_per_hour = (scan_total / max(1, hours)) if scan_total else 0.0
        avg_scan_duration_sec = (scan_total_duration_sec / max(1, scan_finished_count)) if scan_finished_count else 0.0
        avg_pages_per_scan = (scan_total_pages / max(1, scan_total)) if scan_total else 0.0
        avg_forms_per_scan = (scan_total_forms / max(1, scan_total)) if scan_total else 0.0
        avg_emails_per_scan = (scan_total_emails / max(1, scan_total)) if scan_total else 0.0

        def _timer_next(unit: str) -> str:
            return _run_command(["systemctl", "list-timers", unit, "--no-pager", "--all", "--legend=0"])

        job_specs = [
            ("Rejected Scan", "radio-db-rejected-scan.service", "service"),
            ("Rejected Scan Timer", "radio-db-rejected-scan.timer", "timer"),
            ("Country Discovery", "radio-db-country-discovery.service", "service"),
            ("Country Discovery Timer", "radio-db-country-discovery.timer", "timer"),
            ("Verify Cycle", "radio-db-verify.service", "service"),
            ("Verify Timer", "radio-db-verify.timer", "timer"),
        ]
        for label, unit, kind in job_specs:
            active = _run_command(["systemctl", "is-active", unit])
            enabled = _run_command(["systemctl", "is-enabled", unit]) if kind == "timer" else "-"
            next_run = _timer_next(unit) if kind == "timer" else "-"
            current_jobs_rows.append(
                "<tr>"
                f"<td>{escape(label)}</td>"
                f"<td>{escape(unit)}</td>"
                f"<td>{escape(active)}</td>"
                f"<td>{escape(enabled)}</td>"
                f"<td>{escape(next_run)}</td>"
                "</tr>"
            )
        jobs_processes = _run_command(
            [
                "bash",
                "-lc",
                "ps -eo pid,user,etimes,cmd --sort=-etimes | rg -i 'radio-db-rejected-scan|run_rejected_scan_cycle|run_country_discovery_cycle|run_verify_cycle' | head -n 20 || true",
            ]
        )

        api_rows = _read_api_call_history(limit=limit * 3, hours=hours)
        for row in api_rows:
            pvd = str(row.get("provider") or "unknown").strip().lower()
            st = str(row.get("status") or "unknown").strip().lower()
            provider_counts[pvd] = provider_counts.get(pvd, 0) + 1
            status_counts[st] = status_counts.get(st, 0) + 1
            if provider and pvd != provider:
                continue
            if api_status and st != api_status:
                continue
            blob = " ".join(
                [
                    str(row.get("provider") or ""),
                    str(row.get("operation") or ""),
                    str(row.get("query") or ""),
                    str(row.get("country") or ""),
                    str(row.get("status") or ""),
                    str(row.get("error") or ""),
                ]
            ).lower()
            if q and q not in blob:
                continue
            total_api_rows += 1
            perf = provider_perf.setdefault(
                pvd,
                {
                    "calls": 0.0,
                    "success_calls": 0.0,
                    "results_total": 0.0,
                    "quota_or_cooldown_skips": 0.0,
                },
            )
            perf["calls"] += 1
            if st == "success":
                perf["success_calls"] += 1
                perf["results_total"] += float(row.get("result_count") or 0)
            if st in {"skipped_quota", "skipped_cooldown"}:
                perf["quota_or_cooldown_skips"] += 1
            api_rows_html.append(
                "<tr>"
                f"<td>{escape(str(row.get('ts_utc') or '-'))}</td>"
                f"<td>{escape(str(row.get('provider') or '-'))}</td>"
                f"<td>{escape(str(row.get('operation') or '-'))}</td>"
                f"<td>{escape(str(row.get('status') or '-'))}</td>"
                f"<td>{escape(str(row.get('country') or '-'))}</td>"
                f"<td>{escape(str(row.get('result_count') if row.get('result_count') is not None else '-'))}</td>"
                f"<td>{escape(str(row.get('query') or '-')[:180])}</td>"
                f"<td>{escape(str(row.get('error') or '-')[:180])}</td>"
                "</tr>"
            )
            if total_api_rows >= limit:
                break

        provider_perf_rows = []
        for name, perf in sorted(
            provider_perf.items(),
            key=lambda item: (
                -item[1].get("results_total", 0.0) / max(1.0, item[1].get("calls", 0.0)),
                -item[1].get("success_calls", 0.0),
            ),
        ):
            calls = int(perf.get("calls", 0.0))
            success_calls = int(perf.get("success_calls", 0.0))
            results_total = int(perf.get("results_total", 0.0))
            value_per_call = results_total / max(1, calls)
            success_rate = (100.0 * success_calls / max(1, calls))
            skipped_quota_or_cooldown = int(perf.get("quota_or_cooldown_skips", 0.0))
            provider_perf_rows.append(
                "<tr>"
                f"<td>{escape(name)}</td>"
                f"<td>{calls}</td>"
                f"<td>{success_calls}</td>"
                f"<td>{results_total}</td>"
                f"<td>{value_per_call:.2f}</td>"
                f"<td>{success_rate:.1f}%</td>"
                f"<td>{skipped_quota_or_cooldown}</td>"
                "</tr>"
            )

        channel_cards = "".join(
            f"<div class='card'><div class='k'>{escape(name)}</div><div class='v'>{count}</div></div>"
            for name, count in sorted(channel_counts.items(), key=lambda x: (-x[1], x[0]))[:8]
        ) or "<div class='card'><div class='k'>Keine Daten</div><div class='v'>0</div></div>"

        source_options = "<option value=''>all</option>" + "".join(
            f"<option value='{escape(name)}' {'selected' if source_type == name else ''}>{escape(name)}</option>"
            for name in sorted(source_counts.keys())
        )
        channel_options = "<option value=''>all</option>" + "".join(
            f"<option value='{escape(name)}' {'selected' if channel == name else ''}>{escape(name)}</option>"
            for name in sorted(channel_counts.keys())
        )
        provider_options = "<option value=''>all</option>" + "".join(
            f"<option value='{escape(name)}' {'selected' if provider == name else ''}>{escape(name)}</option>"
            for name in sorted(provider_counts.keys())
        )
        status_options = "<option value=''>all</option>" + "".join(
            f"<option value='{escape(name)}' {'selected' if api_status == name else ''}>{escape(name)}</option>"
            for name in sorted(status_counts.keys())
        )

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Radio DB History</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: #ffffff;
      --ink: #102015;
      --muted: #516356;
      --line: #d8dfd3;
      --accent: #126d47;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(140deg, #eef4ec, #f9f3eb); color: var(--ink); }}
    .wrap {{ max-width: 1500px; margin: 22px auto; padding: 0 16px; }}
    .topnav {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .topnav a {{ text-decoration:none; color:#fff; background: var(--accent); padding:8px 12px; border-radius:8px; font-size:13px; }}
    .topnav a.secondary {{ background:#42584a; }}
    .filters {{ margin: 12px 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
    label {{ font-size: 12px; color: var(--muted); display:block; }}
    input, select, button {{ border: 1px solid #bfcabf; border-radius: 8px; padding: 8px; background: #fff; }}
    button {{ background: var(--accent); color: #fff; border: 0; cursor: pointer; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:10px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .k {{ font-size: 12px; color: var(--muted); }}
    .v {{ font-size: 21px; font-weight: 650; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); }}
    th, td {{ border-bottom: 1px solid #e4eadf; text-align: left; padding: 7px 8px; font-size: 12px; vertical-align: top; }}
    th {{ background: #eef3e8; position: sticky; top: 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topnav">
      <a href="/legacy">Radio DB</a>
      <a class="secondary" href="/email">E-Mail</a>
      <a class="secondary" href="/markets">Markets</a>
      <a class="secondary" href="/history">History</a>
      <a class="secondary" href="/agent">Agent Dashboard</a>
    </div>
    <h1>History</h1>
    <div class="k">Welche Daten wurden zuletzt geholt, wann, und über welchen Kanal | UTC {monitor["now_utc"]}</div>

    <form class="filters" method="get">
      <div><label>Stunden zurück</label><input type="number" min="1" max="{24*30}" name="hours" value="{hours}" /></div>
      <div><label>Zeilen</label><input type="number" min="20" max="1000" name="limit" value="{limit}" /></div>
      <div><label>Kanal</label><select name="channel">{channel_options}</select></div>
      <div><label>Source Type</label><select name="source_type">{source_options}</select></div>
      <div><label>API Provider</label><select name="provider">{provider_options}</select></div>
      <div><label>API Status</label><select name="api_status">{status_options}</select></div>
      <div><label>Suche</label><input name="q" value="{escape(q)}" placeholder="station, source_id, url, title..." /></div>
      <div><button type="submit">Filtern</button></div>
    </form>

    <div class="grid">
      <div class='card'><div class='k'>Gefilterte Rows</div><div class='v'>{total_rows}</div></div>
      <div class='card'><div class='k'>Gefilterte API Calls</div><div class='v'>{total_api_rows}</div></div>
      <div class='card'><div class='k'>Window (h)</div><div class='v'>{hours}</div></div>
      <div class='card'><div class='k'>LLM Calls Today</div><div class='v'>{monitor["llm_calls_today"]} / {monitor["max_llm_calls_per_day"]}</div></div>
      <div class='card'><div class='k'>LLM USD Today</div><div class='v'>${monitor["usd_spent_today_estimate"]:.4f} / ${monitor["max_daily_usd"]:.2f}</div></div>
      {channel_cards}
    </div>

    <div class="grid" style="margin-top:10px;">
      <div class='card'><div class='k'>Scans im Window</div><div class='v'>{scan_total}</div></div>
      <div class='card'><div class='k'>Running Now</div><div class='v'>{scan_running_now}</div></div>
      <div class='card'><div class='k'>Open Review Queue</div><div class='v'>{scan_review_open_now}</div></div>
      <div class='card'><div class='k'>Scans / h</div><div class='v'>{scan_per_hour:.2f}</div></div>
      <div class='card'><div class='k'>Avg Duration</div><div class='v'>{avg_scan_duration_sec:.1f}s</div></div>
      <div class='card'><div class='k'>Success (awaiting_review)</div><div class='v'>{scan_success_rate:.1f}%</div></div>
      <div class='card'><div class='k'>Blocked Rate</div><div class='v'>{scan_blocked_rate:.1f}%</div></div>
      <div class='card'><div class='k'>Avg Pages/Scan</div><div class='v'>{avg_pages_per_scan:.2f}</div></div>
      <div class='card'><div class='k'>Avg Forms/Scan</div><div class='v'>{avg_forms_per_scan:.2f}</div></div>
      <div class='card'><div class='k'>Avg Emails/Scan</div><div class='v'>{avg_emails_per_scan:.2f}</div></div>
      {scan_status_cards}
    </div>

    <div style="margin-top:10px;">
      <h3>Scan Summary</h3>
      <div class="card">
        <div class="k">Top Blocked Reasons</div>
        <div style="font-size:13px; margin-top:6px;">{escape(blocked_reason_text)}</div>
      </div>
    </div>

    <div style="overflow:auto; max-height: 44vh; margin-top:10px;">
      <h3>Current Jobs</h3>
      <table>
        <thead>
          <tr>
            <th>Job</th><th>Unit</th><th>Active</th><th>Enabled</th><th>Next</th>
          </tr>
        </thead>
        <tbody>
          {''.join(current_jobs_rows) if current_jobs_rows else "<tr><td colspan='5'>Keine Job-Daten</td></tr>"}
        </tbody>
      </table>
      <div class="card" style="margin-top:8px;">
        <div class="k">Job Processes</div>
        <div style="font-family:IBM Plex Mono,Consolas,monospace; font-size:12px; white-space:pre-wrap; margin-top:6px;">{escape(jobs_processes or "-")}</div>
      </div>
    </div>

    <div style="overflow:auto; max-height: 44vh; margin-top:10px;">
      <h3>Scan Run History (Window)</h3>
      <table>
        <thead>
          <tr>
            <th>Run</th><th>Station ID</th><th>Station</th><th>Status</th><th>State</th><th>Blocked Reason</th><th>Conf</th><th>Dur</th><th>Started</th>
          </tr>
        </thead>
        <tbody>
          {''.join(recent_scan_rows) if recent_scan_rows else "<tr><td colspan='9'>Keine Scan-Runs im Window</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style="overflow:auto; max-height: 72vh; margin-top:10px;">
      <h3>API Call History</h3>
      <table>
        <thead>
          <tr>
            <th>Zeit (UTC)</th><th>Provider</th><th>Operation</th><th>Status</th><th>Country</th><th>Results</th><th>Query</th><th>Error</th>
          </tr>
        </thead>
        <tbody>
          {''.join(api_rows_html) if api_rows_html else "<tr><td colspan='8'>Keine API-Calls für den Filter</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style="overflow:auto; max-height: 42vh; margin-top:10px;">
      <h3>API Value per Call (gefilterte API-Zeilen)</h3>
      <table>
        <thead>
          <tr>
            <th>Provider</th><th>Calls</th><th>Success Calls</th><th>Total Results</th><th>Value/Call</th><th>Success Rate</th><th>Quota/Cooldown Skips</th>
          </tr>
        </thead>
        <tbody>
          {''.join(provider_perf_rows) if provider_perf_rows else "<tr><td colspan='7'>Keine API-Calls für den Filter</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style="overflow:auto; max-height: 72vh; margin-top:10px;">
      <h3>Evidence History</h3>
      <table>
        <thead>
          <tr>
            <th>Zeit (UTC)</th><th>Kanal</th><th>Source Type</th><th>Source ID</th><th>Station</th><th>Title</th><th>URL</th><th>Conf</th><th>Payload</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html) if rows_html else "<tr><td colspan='9'>Keine History-Daten für den Filter</td></tr>"}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""

    @app.get("/markets", response_class=HTMLResponse)
    def markets_dashboard(
        market: str = Query(default=""),
        q: str = Query(default=""),
        market_msg: str = Query(default=""),
        focus_market: str = Query(default=""),
        focus_network_key: str = Query(default=""),
    ) -> str:
        safe_market = (market or "").strip().upper()
        safe_q = (q or "").strip().lower()
        safe_focus_market = (focus_market or "").strip().upper()
        safe_focus_network_key = (focus_network_key or "").strip()
        market_notice_map = {
            "market_added": "Market-Eintrag gespeichert.",
            "market_updated": "Market-Eintrag aktualisiert.",
            "network_added": "Network-Eintrag gespeichert.",
            "network_updated": "Network-Eintrag aktualisiert.",
            "invalid_market_code": "Market Code ist ungueltig.",
            "invalid_network_key": "Network Key fehlt.",
            "read_only": "Legacy ist read-only. Bearbeite Markets nur noch ueber die neue App/API.",
        }
        market_notice = market_notice_map.get(market_msg, "")

        with SessionLocal() as session:
            monitor = _monitor_snapshot(session)
            market_stmt = select(MarketIntelligence).order_by(MarketIntelligence.market_code.asc())
            if safe_market:
                market_stmt = market_stmt.where(MarketIntelligence.market_code == safe_market)
            markets = session.scalars(market_stmt).all()
            focused_market_row = session.scalar(
                select(MarketIntelligence).where(MarketIntelligence.market_code == safe_focus_market)
            ) if safe_focus_market else None

            network_stmt = select(DistributionNetwork).order_by(
                DistributionNetwork.market_code.asc(),
                DistributionNetwork.confidence.desc(),
                DistributionNetwork.name.asc(),
            )
            if safe_market:
                network_stmt = network_stmt.where(DistributionNetwork.market_code == safe_market)
            networks = session.scalars(network_stmt).all()
            focused_network_row = session.scalar(
                select(DistributionNetwork).where(DistributionNetwork.network_key == safe_focus_network_key)
            ) if safe_focus_network_key else None
            if safe_q:
                networks = [
                    row for row in networks
                    if safe_q in " ".join(
                        [
                            row.name or "",
                            row.market_code or "",
                            row.network_type or "",
                            row.coverage_note or "",
                            row.rules_summary or "",
                        ]
                    ).lower()
                ]

            network_ids = [row.id for row in networks]
            link_rows: list[tuple[StationNetworkLink, Station, DistributionNetwork]] = []
            if network_ids:
                link_stmt = (
                    select(StationNetworkLink, Station, DistributionNetwork)
                    .join(Station, Station.id == StationNetworkLink.station_id)
                    .join(DistributionNetwork, DistributionNetwork.id == StationNetworkLink.network_id)
                    .where(StationNetworkLink.network_id.in_(network_ids))
                    .order_by(
                        DistributionNetwork.market_code.asc(),
                        DistributionNetwork.name.asc(),
                        StationNetworkLink.confidence.desc(),
                        Station.confidence_score.desc(),
                    )
                )
                link_rows = session.execute(link_stmt).all()
                if safe_q:
                    link_rows = [
                        row for row in link_rows
                        if safe_q in " ".join(
                            [
                                row[1].canonical_name or "",
                                row[1].city or "",
                                row[2].name or "",
                                row[0].relationship_type or "",
                                row[0].notes or "",
                            ]
                        ).lower()
                    ]

        focus_market_card = ""
        if focused_market_row is not None:
            focus_market_card = (
                "<div class='card' style='margin-top:10px; border-color:#126d47;'>"
                "<div class='k'>Market Fokus</div>"
                f"<div style='font-size:18px;font-weight:650;'>{escape(focused_market_row.market_name)} ({escape(focused_market_row.market_code)})</div>"
                f"<div style='margin-top:6px;font-size:13px;'>{escape((focused_market_row.submission_norms or '-')[:260])}</div>"
                "</div>"
            )
        elif focused_network_row is not None:
            focus_market_card = (
                "<div class='card' style='margin-top:10px; border-color:#126d47;'>"
                "<div class='k'>Network Fokus</div>"
                f"<div style='font-size:18px;font-weight:650;'>{escape(focused_network_row.name)}</div>"
                f"<div style='margin-top:6px;font-size:13px;'>Market: {escape(focused_network_row.market_code)} | Type: {escape(focused_network_row.network_type)}</div>"
                f"<div style='margin-top:6px;font-size:13px;'>{escape((focused_network_row.rules_summary or focused_network_row.coverage_note or '-')[:260])}</div>"
                "</div>"
            )

        market_rows = []
        for row in markets:
            try:
                key_networks = json.loads(row.key_networks_json or "[]")
            except Exception:
                key_networks = []
            market_rows.append(
                "<tr>"
                f"<td>{escape(row.market_code)}</td>"
                f"<td>{escape(row.market_name)}</td>"
                f"<td>{escape(', '.join(key_networks)[:220] or '-')}</td>"
                f"<td>{escape((row.language_context or '-')[:240])}</td>"
                f"<td>{escape((row.submission_norms or '-')[:240])}</td>"
                f"<td>{escape((row.outreach_style or '-')[:220])}</td>"
                f"<td>{row.confidence:.2f}</td>"
                "</tr>"
            )

        network_rows = []
        for row in networks:
            link = (
                f'<a href="{escape(row.submission_url)}" target="_blank" rel="noopener">{escape((row.submission_url or "")[:90])}</a>'
                if row.submission_url else "-"
            )
            network_focus_href = "/markets?" + urlencode(
                {
                    "market": safe_market,
                    "q": q,
                    "focus_network_key": row.network_key,
                }
            )
            network_rows.append(
                "<tr>"
                f"<td>{escape(row.market_code)}</td>"
                f"<td>{escape(row.name)}</td>"
                f"<td>{escape(row.network_type)}</td>"
                f"<td>{link}</td>"
                f"<td>{escape(row.submission_email or '-')}</td>"
                f"<td>{escape((row.coverage_note or '-')[:220])}</td>"
                f"<td>{escape((row.rules_summary or '-')[:240])}</td>"
                f"<td>{row.confidence:.2f}</td>"
                f"<td><a href=\"{escape(network_focus_href)}\">Fokus</a></td>"
                "</tr>"
            )

        linked_station_rows = []
        for link_row, station_row, network_row in link_rows[:300]:
            focus_href = "/?" + urlencode({"focus_station_id": station_row.id})
            linked_station_rows.append(
                "<tr>"
                f"<td>{escape(network_row.market_code)}</td>"
                f"<td>{escape(network_row.name)}</td>"
                f"<td>{station_row.id}</td>"
                f"<td>{escape(station_row.canonical_name)}</td>"
                f"<td>{escape(station_row.city or '-')}</td>"
                f"<td>{escape(link_row.relationship_type)}</td>"
                f"<td>{escape((link_row.notes or '-')[:140])}</td>"
                f"<td>{link_row.confidence:.2f}</td>"
                f"<td><a href=\"{escape(focus_href)}\">Fokus</a></td>"
                "</tr>"
            )

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Radio DB Markets</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: #ffffff;
      --ink: #102015;
      --muted: #516356;
      --line: #d8dfd3;
      --accent: #126d47;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(140deg, #eef4ec, #f9f3eb); color: var(--ink); }}
    .wrap {{ max-width: 1600px; margin: 22px auto; padding: 0 16px; }}
    .topnav {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .topnav a {{ text-decoration:none; color:#fff; background: var(--accent); padding:8px 12px; border-radius:8px; font-size:13px; }}
    .topnav a.secondary {{ background:#42584a; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:10px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .k {{ font-size: 12px; color: var(--muted); }}
    .v {{ font-size: 21px; font-weight: 650; }}
    .filters {{ margin: 12px 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
    label {{ font-size: 12px; color: var(--muted); display:block; }}
    input, button {{ border: 1px solid #bfcabf; border-radius: 8px; padding: 8px; background: #fff; }}
    button {{ background: var(--accent); color: #fff; border: 0; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); }}
    th, td {{ border-bottom: 1px solid #e4eadf; text-align: left; padding: 7px 8px; font-size: 12px; vertical-align: top; }}
    th {{ background: #eef3e8; position: sticky; top: 0; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topnav">
      <a href="/legacy">Radio DB</a>
      <a class="secondary" href="/email">E-Mail</a>
      <a class="secondary" href="/markets">Markets</a>
      <a class="secondary" href="/history">History</a>
      <a class="secondary" href="/agent">Agent Dashboard</a>
    </div>
    <h1>Markets</h1>
    <div class="k">Laender-, Netzwerk- und Verteilkontext fuer Submission-Strategien | UTC {monitor["now_utc"]}</div>

    <form class="filters" method="get">
      <div><label>Market</label><input name="market" value="{escape(safe_market)}" placeholder="CA, DE, US..." /></div>
      <div><label>Suche</label><input name="q" value="{escape(q)}" placeholder="network, rules, station..." /></div>
      <div><button type="submit">Filtern</button></div>
    </form>

    <div class="grid">
      <div class="card"><div class="k">Markets</div><div class="v">{len(markets)}</div></div>
      <div class="card"><div class="k">Networks</div><div class="v">{len(networks)}</div></div>
      <div class="card"><div class="k">Linked Stations</div><div class="v">{len(link_rows)}</div></div>
      <div class="card"><div class="k">Visible High-Priority</div><div class="v">{monitor["visible_high_priority"]}</div></div>
    </div>
    {"<div class='card' style='margin-top:10px; border-color:#126d47;'><div class='k'>Status</div><div style='font-size:14px;'>" + escape(market_notice) + "</div></div>" if market_notice else ""}
    {focus_market_card}

    <div class="card" style="margin-top:10px;">
      <div class="k">Market manuell hinzufügen / editieren</div>
      <form method="get" action="/markets/market/add" style="margin-top:8px; display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:8px; align-items:end;">
        <input type="hidden" name="next_url" value="/markets?{escape(urlencode({'market': safe_market, 'q': q}))}" />
        <div><label>Market Code</label><input name="market_code" value="{escape(focused_market_row.market_code if focused_market_row else safe_market)}" placeholder="DE, US..." required /></div>
        <div><label>Market Name</label><input name="market_name" value="{escape(focused_market_row.market_name if focused_market_row else '')}" placeholder="Germany" required /></div>
        <div><label>Language Context</label><input name="language_context" value="{escape(focused_market_row.language_context if focused_market_row and focused_market_row.language_context else '')}" /></div>
        <div><label>Key Networks (CSV)</label><input name="key_networks_csv" value="{escape(', '.join(json.loads(focused_market_row.key_networks_json or '[]')) if focused_market_row else '')}" /></div>
        <div><label>Submission Norms</label><input name="submission_norms" value="{escape(focused_market_row.submission_norms if focused_market_row and focused_market_row.submission_norms else '')}" /></div>
        <div><label>Editorial Notes</label><input name="editorial_notes" value="{escape(focused_market_row.editorial_notes if focused_market_row and focused_market_row.editorial_notes else '')}" /></div>
        <div><label>Outreach Style</label><input name="outreach_style" value="{escape(focused_market_row.outreach_style if focused_market_row and focused_market_row.outreach_style else '')}" /></div>
        <div><label>Confidence</label><input type="number" step="0.05" min="0" max="1" name="confidence" value="{focused_market_row.confidence if focused_market_row else 0.75}" /></div>
        <div><button type="submit">Market Speichern</button></div>
      </form>
    </div>

    <div class="card" style="margin-top:10px;">
      <div class="k">Network manuell hinzufügen / editieren</div>
      <form method="get" action="/markets/network/add" style="margin-top:8px; display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:8px; align-items:end;">
        <input type="hidden" name="next_url" value="/markets?{escape(urlencode({'market': safe_market, 'q': q}))}" />
        <div><label>Network Key</label><input name="network_key" value="{escape(focused_network_row.network_key if focused_network_row else '')}" placeholder="de_campusradio" required /></div>
        <div><label>Market Code</label><input name="market_code" value="{escape(focused_network_row.market_code if focused_network_row else safe_market)}" placeholder="DE" required /></div>
        <div><label>Name</label><input name="name" value="{escape(focused_network_row.name if focused_network_row else '')}" placeholder="Network name" required /></div>
        <div><label>Type</label><input name="network_type" value="{escape(focused_network_row.network_type if focused_network_row else 'association')}" /></div>
        <div><label>Submission URL</label><input name="submission_url" value="{escape(focused_network_row.submission_url if focused_network_row and focused_network_row.submission_url else '')}" /></div>
        <div><label>Submission Email</label><input name="submission_email" value="{escape(focused_network_row.submission_email if focused_network_row and focused_network_row.submission_email else '')}" /></div>
        <div><label>Coverage Note</label><input name="coverage_note" value="{escape(focused_network_row.coverage_note if focused_network_row and focused_network_row.coverage_note else '')}" /></div>
        <div><label>Rules Summary</label><input name="rules_summary" value="{escape(focused_network_row.rules_summary if focused_network_row and focused_network_row.rules_summary else '')}" /></div>
        <div><label>Source URL</label><input name="source_url" value="{escape(focused_network_row.source_url if focused_network_row and focused_network_row.source_url else '')}" /></div>
        <div><label>Confidence</label><input type="number" step="0.05" min="0" max="1" name="confidence" value="{focused_network_row.confidence if focused_network_row else 0.75}" /></div>
        <div><button type="submit">Network Speichern</button></div>
      </form>
    </div>

    <div style="overflow:auto; max-height: 34vh; margin-top:10px;">
      <h3>Market Intelligence</h3>
      <table>
        <thead>
          <tr><th>Code</th><th>Market</th><th>Key Networks</th><th>Language Context</th><th>Submission Norms</th><th>Outreach Style</th><th>Conf</th></tr>
        </thead>
        <tbody>
          {"".join(market_rows) if market_rows else "<tr><td colspan='7'>Keine Market-Daten</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style="overflow:auto; max-height: 34vh; margin-top:10px;">
      <h3>Distribution Networks</h3>
      <table>
        <thead>
          <tr><th>Market</th><th>Name</th><th>Type</th><th>Submission URL</th><th>Email</th><th>Coverage</th><th>Rules</th><th>Conf</th><th>Fokus</th></tr>
        </thead>
        <tbody>
          {"".join(network_rows) if network_rows else "<tr><td colspan='9'>Keine Netzwerk-Daten</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style="overflow:auto; max-height: 40vh; margin-top:10px;">
      <h3>Linked Stations</h3>
      <table>
        <thead>
          <tr><th>Market</th><th>Network</th><th>ID</th><th>Station</th><th>City</th><th>Relationship</th><th>Notes</th><th>Conf</th><th>Fokus</th></tr>
        </thead>
        <tbody>
          {"".join(linked_station_rows) if linked_station_rows else "<tr><td colspan='9'>Keine verknuepften Sender</td></tr>"}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""

    @app.get("/markets/market/add")
    def market_add(
        market_code: str = Query(...),
        market_name: str = Query(...),
        language_context: str = Query(default=""),
        submission_norms: str = Query(default=""),
        editorial_notes: str = Query(default=""),
        outreach_style: str = Query(default=""),
        key_networks_csv: str = Query(default=""),
        confidence: float = Query(default=0.75, ge=0.0, le=1.0),
        next_url: str = Query(default="/markets"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/markets") else "/markets"
        code = (market_code or "").strip().upper()
        if not code or len(code) > 8:
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}market_msg=invalid_market_code", status_code=303)
        return _read_only_redirect(f"{target}{'&' if '?' in target else '?'}focus_market={code}", msg_key="market_msg")

    @app.get("/markets/network/add")
    def network_add(
        network_key: str = Query(...),
        market_code: str = Query(...),
        name: str = Query(...),
        network_type: str = Query(default="association"),
        submission_url: str = Query(default=""),
        submission_email: str = Query(default=""),
        coverage_note: str = Query(default=""),
        rules_summary: str = Query(default=""),
        source_url: str = Query(default=""),
        confidence: float = Query(default=0.75, ge=0.0, le=1.0),
        next_url: str = Query(default="/markets"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/markets") else "/markets"
        key = (network_key or "").strip()
        code = (market_code or "").strip().upper()
        if not key:
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}market_msg=invalid_network_key", status_code=303)
        if not code or len(code) > 8:
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}market_msg=invalid_market_code", status_code=303)
        return _read_only_redirect(f"{target}{'&' if '?' in target else '?'}focus_network_key={key}", msg_key="market_msg")

    @app.get("/email/add")
    def email_add(
        station_id_raw: str = Query(default="", alias="station_id"),
        entry_type: str = Query(...),
        email: str = Query(default=""),
        auto_create_station: str = Query(default="false"),
        station_name: str = Query(default=""),
        station_country: str = Query(default=""),
        station_city: str = Query(default=""),
        station_website: str = Query(default=""),
        name: str = Query(default=""),
        role: str = Query(default="unknown"),
        show_name: str = Query(default=""),
        contact_url: str = Query(default=""),
        notes: str = Query(default=""),
        requirements: str = Query(default=""),
        accepts_newcomers: str = Query(default="false"),
        next_url: str = Query(default="/email"),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/email") else "/email"
        safe_type = (entry_type or "").strip().lower()
        safe_email = (email or "").strip().lower()
        safe_url = (contact_url or "").strip() or None
        station_id: int | None = None
        raw_station_id = (station_id_raw or "").strip()
        if raw_station_id:
            try:
                parsed_station_id = int(raw_station_id)
            except ValueError:
                return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=invalid_station", status_code=303)
            if parsed_station_id < 1:
                return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=invalid_station", status_code=303)
            station_id = parsed_station_id
        if safe_type not in {"submission", "contact", "people"}:
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=invalid_type", status_code=303)
        if safe_type == "submission":
            if not safe_email and not safe_url:
                return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=missing_required", status_code=303)
        elif not _is_valid_email(safe_email):
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=invalid_email", status_code=303)
        return _read_only_redirect(target, msg_key="email_msg")

    @app.get("/email/delete")
    def email_delete(
        email: str = Query(...),
        next_url: str = Query(default="/email"),
        reason: str = Query(default="Manual dashboard delete + blacklist"),
        source: str = Query(default=""),
        entry_id: int = Query(default=0),
    ) -> RedirectResponse:
        target = next_url if next_url.startswith("/email") else "/email"
        safe_email = (email or "").strip().lower()
        if entry_id <= 0 and not _is_valid_email(safe_email):
            return RedirectResponse(url=f"{target}{'&' if '?' in target else '?'}email_msg=invalid_email", status_code=303)
        return _read_only_redirect(target, msg_key="email_msg")

    @app.get("/email", response_class=HTMLResponse)
    def email_dashboard(
        q: str = Query(default=""),
        country: str = Query(default=""),
        source: str = Query(default="all"),
        newcomer_only: str = Query(default="any"),
        manual_only: str = Query(default="any"),
        min_confidence: float = Query(default=0.6, ge=0.0, le=1.0),
        limit: int = Query(default=300, ge=20, le=2000),
        email_msg: str = Query(default=""),
        focus_station_id: int | None = Query(default=None),
        focus_email: str = Query(default=""),
        focus_entry_type: str = Query(default=""),
        focus_name: str = Query(default=""),
        focus_role: str = Query(default=""),
        focus_show_name: str = Query(default=""),
        focus_contact_url: str = Query(default=""),
        focus_requirements: str = Query(default=""),
        focus_notes: str = Query(default=""),
        focus_accepts_newcomers: str = Query(default="false"),
    ) -> str:
        q = q.strip()
        country = country.strip().upper()
        source = source.strip().lower() or "all"
        newcomer_only = newcomer_only.strip().lower() or "any"
        manual_only = manual_only.strip().lower() or "any"
        focus_email = focus_email.strip().lower()
        focus_entry_type = focus_entry_type.strip().lower()
        focus_name = focus_name.strip()
        focus_role = focus_role.strip().lower() or "unknown"
        focus_show_name = focus_show_name.strip()
        focus_contact_url = focus_contact_url.strip()
        focus_requirements = focus_requirements.strip()
        focus_notes = focus_notes.strip()
        focus_accepts_newcomers = focus_accepts_newcomers.strip().lower() or "false"

        email_notice_map = {
            "added": "E-Mail-Eintrag gespeichert.",
            "updated": "Vorhandener E-Mail-Eintrag aktualisiert.",
            "station_created": "Neue Station angelegt und Kontakt gespeichert.",
            "deleted": "E-Mail-Eintrag geloescht und blacklisted.",
            "blacklisted": "Diese E-Mail ist blacklisted und kann nicht erneut gespeichert werden.",
            "invalid_station": "Station ID wurde nicht gefunden.",
            "invalid_email": "E-Mail-Adresse ist ungueltig.",
            "missing_required": "Pflichtfelder fehlen.",
            "invalid_type": "Eintragstyp ist ungueltig.",
            "read_only": "Legacy ist read-only. Nutze Contact Center oder API fuer Aenderungen.",
        }
        email_notice = email_notice_map.get(email_msg, "")
        focused_station: Station | None = None

        with SessionLocal() as session:
            monitor = _monitor_snapshot(session)
            blacklisted_emails = _load_blacklisted_emails(session)
            if focus_station_id is not None:
                focused_station = session.get(Station, focus_station_id)
            station_filters = [
                Station.status == StationStatus.VERIFIED,
                Station.priority_tier > 0,
                Station.confidence_score >= min_confidence,
            ]
            if country:
                station_filters.append(func.upper(Station.country_code) == country)

            email_rows: list[dict[str, Any]] = []
            seen: set[tuple[Any, ...]] = set()

            if source in {"all", "submission"}:
                submission_stmt = (
                    select(SubmissionChannel, Station)
                    .join(Station, Station.id == SubmissionChannel.station_id)
                    .where(
                        *station_filters,
                        or_(
                            and_(
                                SubmissionChannel.email.is_not(None),
                                func.length(func.trim(func.coalesce(SubmissionChannel.email, ""))) > 0,
                            ),
                            and_(
                                SubmissionChannel.url.is_not(None),
                                func.length(func.trim(func.coalesce(SubmissionChannel.url, ""))) > 0,
                            ),
                        ),
                    )
                    .order_by(
                        Station.priority_tier.desc(),
                        Station.confidence_score.desc(),
                        SubmissionChannel.accepts_newcomers.desc(),
                        SubmissionChannel.id.desc(),
                    )
                )
                if newcomer_only == "yes":
                    submission_stmt = submission_stmt.where(SubmissionChannel.accepts_newcomers.is_(True))
                if manual_only == "yes":
                    submission_stmt = submission_stmt.where(SubmissionChannel.manual_confirmed.is_(True))
                elif manual_only == "no":
                    submission_stmt = submission_stmt.where(
                        or_(SubmissionChannel.manual_confirmed.is_(False), SubmissionChannel.manual_confirmed.is_(None))
                    )
                if q:
                    needle = f"%{q.lower()}%"
                    submission_stmt = submission_stmt.where(
                        or_(
                            func.lower(Station.canonical_name).like(needle),
                            func.lower(func.coalesce(Station.city, "")).like(needle),
                            func.lower(func.coalesce(Station.website_url, "")).like(needle),
                            func.lower(func.coalesce(SubmissionChannel.email, "")).like(needle),
                            func.lower(func.coalesce(SubmissionChannel.url, "")).like(needle),
                            func.lower(func.coalesce(SubmissionChannel.requirements, "")).like(needle),
                        )
                    )
                for sub, station in session.execute(submission_stmt.limit(limit * 3)).all():
                    email_value = (sub.email or "").strip().lower()
                    target_value = email_value or (sub.url or "").strip()
                    dedupe_key = (station.id, "submission", target_value, sub.method.value, sub.url or "")
                    if not target_value or (email_value and email_value in blacklisted_emails) or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    email_rows.append(
                        {
                            "entry_id": sub.id,
                            "station_id": station.id,
                            "station_name": station.canonical_name,
                            "country": station.country_code or "-",
                            "city": station.city or "-",
                            "priority_tier": station.priority_tier,
                            "station_confidence": station.confidence_score,
                            "manual_confirmed": bool(getattr(sub, "manual_confirmed", False)),
                            "email": email_value,
                            "target_value": target_value,
                            "source": "submission",
                            "role_or_method": sub.method.value,
                            "contact_name": "-",
                            "show_name": "-",
                            "newcomers": "yes" if sub.accepts_newcomers else "no",
                            "detail": (sub.requirements or "-")[:220],
                            "contact_url": sub.url or station.website_url or "",
                        }
                    )

            if source in {"all", "people"}:
                people_stmt = (
                    select(StationPerson, Station)
                    .join(Station, Station.id == StationPerson.station_id)
                    .where(
                        *station_filters,
                        StationPerson.email.is_not(None),
                        func.length(func.trim(func.coalesce(StationPerson.email, ""))) > 0,
                    )
                    .order_by(
                        Station.priority_tier.desc(),
                        Station.confidence_score.desc(),
                        StationPerson.confidence.desc(),
                        StationPerson.updated_at.desc(),
                    )
                )
                if manual_only == "yes":
                    people_stmt = people_stmt.where(StationPerson.manual_confirmed.is_(True))
                elif manual_only == "no":
                    people_stmt = people_stmt.where(
                        or_(StationPerson.manual_confirmed.is_(False), StationPerson.manual_confirmed.is_(None))
                    )
                if q:
                    needle = f"%{q.lower()}%"
                    people_stmt = people_stmt.where(
                        or_(
                            func.lower(Station.canonical_name).like(needle),
                            func.lower(func.coalesce(Station.city, "")).like(needle),
                            func.lower(func.coalesce(Station.website_url, "")).like(needle),
                            func.lower(func.coalesce(StationPerson.email, "")).like(needle),
                            func.lower(func.coalesce(StationPerson.name, "")).like(needle),
                            func.lower(func.coalesce(StationPerson.show_name, "")).like(needle),
                            func.lower(func.coalesce(StationPerson.musical_preferences, "")).like(needle),
                            func.lower(func.coalesce(StationPerson.notes, "")).like(needle),
                        )
                    )
                for person, station in session.execute(people_stmt.limit(limit * 3)).all():
                    email_value = (person.email or "").strip().lower()
                    dedupe_key = (
                        station.id,
                        "people",
                        email_value,
                        person.name or "",
                        person.role.value,
                        person.show_name or "",
                    )
                    if not email_value or email_value in blacklisted_emails or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    email_rows.append(
                        {
                            "entry_id": person.id,
                            "station_id": station.id,
                            "station_name": station.canonical_name,
                            "country": station.country_code or "-",
                            "city": station.city or "-",
                            "priority_tier": station.priority_tier,
                            "station_confidence": station.confidence_score,
                            "manual_confirmed": bool(getattr(person, "manual_confirmed", False)),
                            "email": email_value,
                            "target_value": email_value,
                            "source": "people",
                            "role_or_method": person.role.value,
                            "contact_name": person.name or "-",
                            "show_name": person.show_name or "-",
                            "newcomers": "-",
                            "detail": ((person.musical_preferences or person.notes or "-")[:220]),
                            "contact_url": person.contact_url or person.linkedin_url or station.website_url or "",
                            "contact_confidence": person.confidence,
                        }
                    )

            if source in {"all", "contact"}:
                contact_stmt = (
                    select(StationContact, Station)
                    .join(Station, Station.id == StationContact.station_id)
                    .where(
                        *station_filters,
                        StationContact.email.is_not(None),
                        func.length(func.trim(func.coalesce(StationContact.email, ""))) > 0,
                    )
                    .order_by(
                        Station.priority_tier.desc(),
                        Station.confidence_score.desc(),
                        StationContact.confidence.desc(),
                        StationContact.id.desc(),
                    )
                )
                if manual_only == "yes":
                    contact_stmt = contact_stmt.where(StationContact.manual_confirmed.is_(True))
                elif manual_only == "no":
                    contact_stmt = contact_stmt.where(
                        or_(StationContact.manual_confirmed.is_(False), StationContact.manual_confirmed.is_(None))
                    )
                if q:
                    needle = f"%{q.lower()}%"
                    contact_stmt = contact_stmt.where(
                        or_(
                            func.lower(Station.canonical_name).like(needle),
                            func.lower(func.coalesce(Station.city, "")).like(needle),
                            func.lower(func.coalesce(Station.website_url, "")).like(needle),
                            func.lower(func.coalesce(StationContact.email, "")).like(needle),
                            func.lower(func.coalesce(StationContact.name, "")).like(needle),
                            func.lower(func.coalesce(StationContact.show_name, "")).like(needle),
                            func.lower(func.coalesce(StationContact.notes, "")).like(needle),
                        )
                    )
                for contact, station in session.execute(contact_stmt.limit(limit * 3)).all():
                    email_value = (contact.email or "").strip().lower()
                    dedupe_key = (
                        station.id,
                        "contact",
                        email_value,
                        contact.name or "",
                        contact.role.value,
                        contact.show_name or "",
                    )
                    if not email_value or email_value in blacklisted_emails or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    email_rows.append(
                        {
                            "entry_id": contact.id,
                            "station_id": station.id,
                            "station_name": station.canonical_name,
                            "country": station.country_code or "-",
                            "city": station.city or "-",
                            "priority_tier": station.priority_tier,
                            "station_confidence": station.confidence_score,
                            "manual_confirmed": bool(getattr(contact, "manual_confirmed", False)),
                            "email": email_value,
                            "target_value": email_value,
                            "source": "contact",
                            "role_or_method": contact.role.value,
                            "contact_name": contact.name or "-",
                            "show_name": contact.show_name or "-",
                            "newcomers": "-",
                            "detail": ((contact.notes or "-")[:220]),
                            "contact_url": contact.contact_url or station.website_url or "",
                            "contact_confidence": contact.confidence,
                        }
                    )

        email_rows.sort(
            key=lambda row: (
                -int(row.get("priority_tier") or 0),
                row.get("country") or "",
                -(1 if row.get("newcomers") == "yes" else 0),
                -float(row.get("contact_confidence") or 0.0),
                -float(row.get("station_confidence") or 0.0),
                row.get("station_name") or "",
                row.get("target_value") or "",
            )
        )
        email_rows = email_rows[:limit]

        unique_station_ids = {int(row["station_id"]) for row in email_rows}
        unique_emails = {str(row["email"]).strip().lower() for row in email_rows}
        unique_targets = {str(row.get("target_value") or "").strip() for row in email_rows if str(row.get("target_value") or "").strip()}
        source_counts = {
            "submission": sum(1 for row in email_rows if row["source"] == "submission"),
            "people": sum(1 for row in email_rows if row["source"] == "people"),
            "contact": sum(1 for row in email_rows if row["source"] == "contact"),
        }
        newcomer_hits = sum(1 for row in email_rows if row["newcomers"] == "yes")

        rows_html = []
        for row in email_rows:
            email_return_url = "/email?" + urlencode(
                {
                    "q": q,
                    "country": country,
                    "source": source,
                    "newcomer_only": newcomer_only,
                    "manual_only": manual_only,
                    "min_confidence": min_confidence,
                    "limit": limit,
                    "focus_station_id": row["station_id"],
                    "focus_email": row["email"],
                    "focus_entry_type": row["source"],
                    "focus_name": row["contact_name"] if row["contact_name"] != "-" else "",
                    "focus_role": row["role_or_method"] if row["source"] != "submission" else "unknown",
                    "focus_show_name": row["show_name"] if row["show_name"] != "-" else "",
                    "focus_contact_url": row.get("contact_url") or "",
                    "focus_requirements": row["detail"] if row["source"] == "submission" else "",
                    "focus_notes": row["detail"] if row["source"] != "submission" else "",
                    "focus_accepts_newcomers": "true" if row["newcomers"] == "yes" else "false",
                }
            )
            confirm_href = "/email/manual-confirm?" + urlencode(
                {
                    "source": row["source"],
                    "entry_id": row["entry_id"],
                    "value": "false" if bool(row.get("manual_confirmed")) else "true",
                    "next_url": email_return_url,
                }
            )
            delete_href = "/email/delete?" + urlencode(
                {
                    "email": row["email"],
                    "source": row["source"],
                    "entry_id": row["entry_id"],
                    "next_url": email_return_url,
                }
            )
            confirm_checked = "checked" if bool(row.get("manual_confirmed")) else ""
            focus_href = "/email?" + urlencode(
                {
                    "q": q,
                    "country": country,
                    "source": source,
                    "newcomer_only": newcomer_only,
                    "manual_only": manual_only,
                    "min_confidence": min_confidence,
                    "limit": limit,
                    "focus_station_id": row["station_id"],
                    "focus_email": row["email"],
                    "focus_entry_type": row["source"],
                    "focus_name": row["contact_name"] if row["contact_name"] != "-" else "",
                    "focus_role": row["role_or_method"] if row["source"] != "submission" else "unknown",
                    "focus_show_name": row["show_name"] if row["show_name"] != "-" else "",
                    "focus_contact_url": row.get("contact_url") or "",
                    "focus_requirements": row["detail"] if row["source"] == "submission" else "",
                    "focus_notes": row["detail"] if row["source"] != "submission" else "",
                    "focus_accepts_newcomers": "true" if row["newcomers"] == "yes" else "false",
                }
            )
            contact_url = str(row.get("contact_url") or "").strip()
            contact_link = (
                f'<a href="{escape(contact_url)}" target="_blank" rel="noopener">{escape(contact_url[:90])}</a>'
                if contact_url
                else "-"
            )
            rows_html.append(
                "<tr>"
                f"<td><a href=\"{escape(delete_href)}\" onclick=\"navigatePreserveScroll('{escape(delete_href)}'); return false;\" style=\"color:#7a1f2d; font-weight:700; text-decoration:none;\">X</a></td>"
                f"<td>{row['station_id']}</td>"
                f"<td>{escape(str(row['station_name']))}</td>"
                f"<td>{escape(str(row['country']))}</td>"
                f"<td>{escape(str(row['city']))}</td>"
                f"<td><input type=\"checkbox\" onclick=\"navigatePreserveScroll('{escape(confirm_href)}')\" {confirm_checked} /></td>"
                f"<td><span style='font-family:IBM Plex Mono,Consolas,monospace;'>{escape(str(row.get('target_value') or '-'))}</span></td>"
                f"<td>{escape(str(row['source']))}</td>"
                f"<td>{escape(str(row['role_or_method']))}</td>"
                f"<td>{escape(str(row['contact_name']))}</td>"
                f"<td>{escape(str(row['show_name']))}</td>"
                f"<td>{escape(str(row['newcomers']))}</td>"
                f"<td>{float(row['station_confidence']):.2f}</td>"
                f"<td>{float(row.get('contact_confidence') or 0.0):.2f}</td>"
                f"<td>{escape(str(row['detail']))}</td>"
                f"<td>{contact_link}</td>"
                f"<td><a href=\"{escape(focus_href)}\">Fokus</a></td>"
                "</tr>"
            )

        focus_card = ""
        if focused_station is not None:
            focus_card = (
                "<div class='card' style='margin-top:10px; border-color:#126d47;'>"
                "<div class='k'>Edit Fokus</div>"
                f"<div style='font-size:18px;font-weight:650;'>{escape(focused_station.canonical_name)} (ID {focused_station.id})</div>"
                f"<div style='margin-top:6px;font-size:13px;'>Country: {escape(focused_station.country_code or '-')} | City: {escape(focused_station.city or '-')} | Status: {focused_station.status.value} | Confidence: {focused_station.confidence_score:.2f}</div>"
                f"<div style='margin-top:6px;font-size:13px;'>Aktueller Eintrag: <span style='font-family:IBM Plex Mono,Consolas,monospace;'>{escape(focus_email or focus_contact_url or '-')}</span> | Typ: {escape(focus_entry_type or '-')}</div>"
                "</div>"
            )

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Radio DB E-Mail</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: #ffffff;
      --ink: #102015;
      --muted: #516356;
      --line: #d8dfd3;
      --accent: #126d47;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(140deg, #eef4ec, #f9f3eb); color: var(--ink); }}
    .wrap {{ max-width: 1600px; margin: 22px auto; padding: 0 16px; }}
    .topnav {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .topnav a {{ text-decoration:none; color:#fff; background: var(--accent); padding:8px 12px; border-radius:8px; font-size:13px; }}
    .topnav a.secondary {{ background:#42584a; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:10px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .k {{ font-size: 12px; color: var(--muted); }}
    .v {{ font-size: 21px; font-weight: 650; }}
    .filters {{ margin: 12px 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
    label {{ font-size: 12px; color: var(--muted); display:block; }}
    input, select, button {{ border: 1px solid #bfcabf; border-radius: 8px; padding: 8px; background: #fff; }}
    button {{ background: var(--accent); color: #fff; border: 0; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); }}
    th, td {{ border-bottom: 1px solid #e4eadf; text-align: left; padding: 7px 8px; font-size: 12px; vertical-align: top; }}
    th {{ background: #eef3e8; position: sticky; top: 0; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
  <script>
    function navigatePreserveScroll(url) {{
      const current = window.scrollY || window.pageYOffset || 0;
      const target = new URL(url, window.location.origin);
      target.searchParams.set("scroll_y", String(current));
      window.location.href = target.pathname + target.search + target.hash;
    }}
    window.addEventListener("load", () => {{
      const params = new URLSearchParams(window.location.search);
      const raw = params.get("scroll_y");
      if (!raw) return;
      const y = parseInt(raw, 10);
      if (!Number.isFinite(y) || y < 0) return;
      window.scrollTo(0, y);
      params.delete("scroll_y");
      const next = window.location.pathname + (params.toString() ? "?" + params.toString() : "") + window.location.hash;
      window.history.replaceState(null, "", next);
    }});
  </script>
</head>
<body>
  <div class="wrap">
    <div class="topnav">
      <a href="/legacy">Radio DB</a>
      <a class="secondary" href="/email">E-Mail</a>
      <a class="secondary" href="/history">History</a>
      <a class="secondary" href="/agent">Agent Dashboard</a>
    </div>
    <h1>E-Mail</h1>
    <div class="k">Relevante Kontaktliste aus Submission-, People- und Contact-Daten; Submission-Eintraege koennen E-Mail oder Formular-Link sein | UTC {monitor["now_utc"]}</div>

    <form class="filters" method="get">
      <div><label>Suche</label><input name="q" value="{escape(q)}" placeholder="station, email, person, show, notes..." /></div>
      <div><label>Country</label><input name="country" value="{escape(country)}" placeholder="DE, US..." /></div>
      <div>
        <label>Quelle</label>
        <select name="source">
          <option value="all" {"selected" if source == "all" else ""}>all</option>
          <option value="submission" {"selected" if source == "submission" else ""}>submission</option>
          <option value="people" {"selected" if source == "people" else ""}>people</option>
          <option value="contact" {"selected" if source == "contact" else ""}>contact</option>
        </select>
      </div>
      <div>
        <label>Newcomers</label>
        <select name="newcomer_only">
          <option value="any" {"selected" if newcomer_only == "any" else ""}>any</option>
          <option value="yes" {"selected" if newcomer_only == "yes" else ""}>yes</option>
        </select>
      </div>
      <div>
        <label>Manual</label>
        <select name="manual_only">
          <option value="any" {"selected" if manual_only == "any" else ""}>any</option>
          <option value="yes" {"selected" if manual_only == "yes" else ""}>yes</option>
          <option value="no" {"selected" if manual_only == "no" else ""}>no</option>
        </select>
      </div>
      <div><label>Min Confidence</label><input type="number" step="0.05" min="0" max="1" name="min_confidence" value="{min_confidence}" /></div>
      <div><label>Limit</label><input type="number" min="20" max="2000" name="limit" value="{limit}" /></div>
      <div><button type="submit">Filtern</button></div>
    </form>

    <div class="grid">
      <div class="card"><div class="k">Rows</div><div class="v">{len(email_rows)}</div></div>
      <div class="card"><div class="k">Unique Targets</div><div class="v">{len(unique_targets)}</div></div>
      <div class="card"><div class="k">Stations</div><div class="v">{len(unique_station_ids)}</div></div>
      <div class="card"><div class="k">Newcomer Hits</div><div class="v">{newcomer_hits}</div></div>
      <div class="card"><div class="k">Submission Targets</div><div class="v">{source_counts["submission"]}</div></div>
      <div class="card"><div class="k">People Emails</div><div class="v">{source_counts["people"]}</div></div>
      <div class="card"><div class="k">Contact Emails</div><div class="v">{source_counts["contact"]}</div></div>
      <div class="card"><div class="k">Visible High-Priority</div><div class="v">{monitor["visible_high_priority"]}</div></div>
    </div>

    {"<div class='card' style='margin-top:10px; border-color:#126d47;'><div class='k'>Status</div><div style='font-size:14px;'>" + escape(email_notice) + "</div></div>" if email_notice else ""}
    {focus_card}

    <div class="card" style="margin-top:10px;">
      <div class="k">Manuell korrekten Kontakt hinzufügen</div>
      <form method="get" action="/email/add" style="margin-top:8px; display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap:8px; align-items:end;">
        <input type="hidden" name="next_url" value="/email?{escape(urlencode({'q': q, 'country': country, 'source': source, 'newcomer_only': newcomer_only, 'manual_only': manual_only, 'min_confidence': min_confidence, 'limit': limit}))}" />
        <div><label>Station ID</label><input type="number" min="1" name="station_id" value="{focus_station_id or ''}" placeholder="leer fuer neue Station" /></div>
        <div>
          <label>Neue Station anlegen</label>
          <select name="auto_create_station">
            <option value="false" {"selected" if focus_station_id else ""}>false</option>
            <option value="true" {"selected" if not focus_station_id else ""}>true</option>
          </select>
        </div>
        <div><label>Station Name</label><input name="station_name" value="{escape(focused_station.canonical_name if focused_station is not None else '')}" placeholder="Pflicht fuer neue Station" /></div>
        <div><label>Station Country</label><input name="station_country" value="{escape(focused_station.country_code if focused_station is not None else country)}" placeholder="DE, US..." /></div>
        <div><label>Station City</label><input name="station_city" value="{escape(focused_station.city if focused_station is not None and focused_station.city else '')}" placeholder="optional" /></div>
        <div><label>Station Website</label><input name="station_website" value="{escape(focused_station.website_url if focused_station is not None and focused_station.website_url else '')}" placeholder="https://..." /></div>
        <div>
          <label>Typ</label>
          <select name="entry_type">
            <option value="submission" {"selected" if focus_entry_type == "submission" else ""}>submission</option>
            <option value="contact" {"selected" if focus_entry_type == "contact" else ""}>contact</option>
            <option value="people" {"selected" if focus_entry_type == "people" else ""}>people</option>
          </select>
        </div>
        <div><label>E-Mail</label><input type="email" name="email" value="{escape(focus_email)}" placeholder="music@station.tld" /></div>
        <div><label>Name</label><input name="name" value="{escape(focus_name)}" placeholder="optional" /></div>
        <div>
          <label>Rolle</label>
          <select name="role">
            <option value="unknown" {"selected" if focus_role == "unknown" else ""}>unknown</option>
            <option value="music_director" {"selected" if focus_role == "music_director" else ""}>music_director</option>
            <option value="program_director" {"selected" if focus_role == "program_director" else ""}>program_director</option>
            <option value="editor" {"selected" if focus_role == "editor" else ""}>editor</option>
            <option value="host" {"selected" if focus_role == "host" else ""}>host</option>
            <option value="dj" {"selected" if focus_role == "dj" else ""}>dj</option>
            <option value="producer" {"selected" if focus_role == "producer" else ""}>producer</option>
          </select>
        </div>
        <div><label>Show</label><input name="show_name" value="{escape(focus_show_name)}" placeholder="optional" /></div>
        <div><label>Kontakt-URL</label><input name="contact_url" value="{escape(focus_contact_url)}" placeholder="https://..." /></div>
        <div><label>Requirements / Notiz</label><input name="requirements" value="{escape(focus_requirements)}" placeholder="manuelle Info, Kontext..." /></div>
        <div><label>Zusatznotiz</label><input name="notes" value="{escape(focus_notes)}" placeholder="optional" /></div>
        <div>
          <label>Accepts Newcomers</label>
          <select name="accepts_newcomers">
            <option value="false" {"selected" if focus_accepts_newcomers != "true" else ""}>false</option>
            <option value="true" {"selected" if focus_accepts_newcomers == "true" else ""}>true</option>
          </select>
        </div>
        <div><button type="submit">Kontakt Speichern</button></div>
      </form>
    </div>

    <div style="overflow:auto; max-height: 74vh; margin-top:10px;">
      <table>
        <thead>
          <tr>
            <th>X</th><th>ID</th><th>Station</th><th>Country</th><th>City</th><th>Manual</th><th>Target</th><th>Quelle</th><th>Role/Method</th><th>Name</th><th>Show</th><th>Newcomers</th><th>Station Conf</th><th>Contact Conf</th><th>Info</th><th>Link</th><th>Fokus</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows_html) if rows_html else "<tr><td colspan='17'>Keine Kontakt-Eintraege fuer den Filter</td></tr>"}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""

    @app.get("/legacy", response_class=HTMLResponse)
    def home(
        q: str = Query(default=""),
        country: str = Query(default=""),
        status: str = Query(default=""),
        genre: str = Query(default=""),
        has_submission: str = Query(default="any"),
        has_people: str = Query(default="any"),
        focus_station_id_raw: str | None = Query(default=None, alias="focus_station_id"),
        boost_msg: str = Query(default=""),
        scan_msg: str = Query(default=""),
        hp_msg: str = Query(default=""),
        legacy_msg: str = Query(default=""),
        min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> str:
        q = q.strip()
        country = country.strip()
        status = status.strip()
        genre = genre.strip()
        has_submission_filter: bool | None = None
        if has_submission == "yes":
            has_submission_filter = True
        if has_submission == "no":
            has_submission_filter = False
        has_people_filter: bool | None = None
        if has_people == "yes":
            has_people_filter = True
        if has_people == "no":
            has_people_filter = False
        focus_station_id: int | None = None
        if focus_station_id_raw is not None:
            raw = focus_station_id_raw.strip()
            if raw:
                try:
                    parsed = int(raw)
                    if parsed >= 1:
                        focus_station_id = parsed
                except ValueError:
                    focus_station_id = None

        with SessionLocal() as session:
            monitor = _monitor_snapshot(session)
            enforced_confidence = max(min_confidence, 0.6)
            stmt = _station_query(
                session=session,
                q=q,
                country=country,
                status=status,
                genre=genre,
                has_submission=has_submission_filter,
                has_people=has_people_filter,
                min_confidence=enforced_confidence,
            )
            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            stations = session.scalars(stmt.order_by(Station.updated_at.desc()).limit(limit)).all()

            station_ids = [s.id for s in stations]
            genre_rows = session.execute(
                select(StationGenre.station_id, StationGenre.genre).where(StationGenre.station_id.in_(station_ids))
            ).all() if station_ids else []
            genres_by_station: dict[int, list[str]] = {}
            for sid, g in genre_rows:
                genres_by_station.setdefault(sid, []).append(g)

            sub_counts_rows = session.execute(
                select(SubmissionChannel.station_id, func.count(SubmissionChannel.id))
                .where(SubmissionChannel.station_id.in_(station_ids))
                .group_by(SubmissionChannel.station_id)
            ).all() if station_ids else []
            sub_counts = {sid: count for sid, count in sub_counts_rows}
            people_counts_rows = session.execute(
                select(StationPerson.station_id, func.count(StationPerson.id))
                .where(StationPerson.station_id.in_(station_ids))
                .group_by(StationPerson.station_id)
            ).all() if station_ids else []
            people_counts = {sid: count for sid, count in people_counts_rows}

            focus_station = None
            focus_aliases: list[str] = []
            focus_genres: list[str] = []
            focus_submissions: list[SubmissionChannel] = []
            focus_people: list[StationPerson] = []
            focus_programs: list[StationProgram] = []
            focus_evidence: list[Evidence] = []
            focus_profile: StationProfileSnapshot | None = None
            focus_market_context: MarketIntelligence | None = None
            focus_network_links: list[tuple[StationNetworkLink, DistributionNetwork]] = []
            if focus_station_id is not None:
                focus_station = session.scalar(select(Station).where(Station.id == focus_station_id))
                if focus_station:
                    focus_aliases = session.scalars(
                        select(StationAlias.alias).where(StationAlias.station_id == focus_station.id).order_by(StationAlias.alias.asc())
                    ).all()
                    focus_genres = session.scalars(
                        select(StationGenre.genre).where(StationGenre.station_id == focus_station.id).order_by(StationGenre.genre.asc())
                    ).all()
                    focus_submissions = session.scalars(
                        select(SubmissionChannel)
                        .where(SubmissionChannel.station_id == focus_station.id)
                        .order_by(SubmissionChannel.id.desc())
                        .limit(50)
                    ).all()
                    focus_people = session.scalars(
                        select(StationPerson)
                        .where(StationPerson.station_id == focus_station.id)
                        .order_by(StationPerson.confidence.desc(), StationPerson.updated_at.desc())
                        .limit(100)
                    ).all()
                    focus_programs = session.scalars(
                        select(StationProgram)
                        .where(StationProgram.station_id == focus_station.id)
                        .order_by(StationProgram.id.desc())
                        .limit(100)
                    ).all()
                    focus_evidence = session.scalars(
                        select(Evidence)
                        .where(Evidence.station_id == focus_station.id)
                        .order_by(Evidence.created_at.desc())
                        .limit(60)
                    ).all()
                    focus_profile = session.scalar(
                        select(StationProfileSnapshot)
                        .where(StationProfileSnapshot.station_id == focus_station.id)
                        .order_by(StationProfileSnapshot.created_at.desc())
                        .limit(1)
                    )
                    if focus_station.country_code:
                        focus_market_context = session.scalar(
                            select(MarketIntelligence).where(
                                MarketIntelligence.market_code == (focus_station.country_code or "").upper()
                            )
                        )
                    focus_network_links = session.execute(
                        select(StationNetworkLink, DistributionNetwork)
                        .join(DistributionNetwork, DistributionNetwork.id == StationNetworkLink.network_id)
                        .where(StationNetworkLink.station_id == focus_station.id)
                        .order_by(StationNetworkLink.confidence.desc(), DistributionNetwork.name.asc())
                        .limit(20)
                    ).all()

        rows = []
        base_params = {
            "q": q,
            "country": country,
            "status": status,
            "genre": genre,
            "has_submission": has_submission,
            "has_people": has_people,
            "boost_msg": boost_msg,
            "scan_msg": scan_msg,
            "hp_msg": hp_msg,
            "min_confidence": min_confidence,
            "limit": limit,
        }
        for s in stations:
            genre_text = ", ".join(sorted(set(genres_by_station.get(s.id, []))))[:120] or "-"
            website_text = (s.website_url or "-")[:120]
            focus_href = "/?" + urlencode({**base_params, "focus_station_id": s.id})
            return_url = "/" + ("?" + urlencode({**base_params, "focus_station_id": focus_station_id}) if any(base_params.values()) or focus_station_id else "")
            confirm_href = "/station/manual-confirm?" + urlencode(
                {
                    "station_id": s.id,
                    "value": "false" if bool(getattr(s, "manual_confirmed", False)) else "true",
                    "next_url": return_url,
                }
            )
            delete_href = "/station/delete?" + urlencode(
                {
                    "station_id": s.id,
                    "next_url": return_url,
                }
            )
            confirm_checked = "checked" if bool(getattr(s, "manual_confirmed", False)) else ""
            rows.append(
                "<tr>"
                f"<td><a href=\"{escape(delete_href)}\" onclick=\"navigatePreserveScroll('{escape(delete_href)}'); return false;\" style=\"color:#7a1f2d; font-weight:700; text-decoration:none;\">X</a></td>"
                f"<td>{s.id}</td>"
                f"<td>{escape(s.canonical_name)}</td>"
                f"<td>{escape(s.country_code or '-')}</td>"
                f"<td>{escape(s.city or '-')}</td>"
                f"<td>{s.status.value}</td>"
                f"<td>{s.confidence_score:.2f}</td>"
                f"<td>{getattr(s, 'priority_tier', 0)}</td>"
                f"<td><input type=\"checkbox\" onclick=\"navigatePreserveScroll('{escape(confirm_href)}')\" {confirm_checked} /></td>"
                f"<td>{escape(genre_text)}</td>"
                f"<td>{people_counts.get(s.id, 0)}</td>"
                f"<td>{sub_counts.get(s.id, 0)}</td>"
                f"<td>{escape(website_text)}</td>"
                f"<td><a href=\"{escape(focus_href)}\">Fokus</a></td>"
                "</tr>"
            )

        focus_panel = ""
        if focus_station_id is not None and focus_station is None:
            focus_panel = (
                "<div class='card'>"
                "<div class='k' style='color: var(--warn);'>Fokus</div>"
                f"<div>Sender mit ID {focus_station_id} wurde nicht gefunden.</div>"
                "</div>"
            )
        elif focus_station is not None:
            def _link(url: str | None) -> str:
                if not url:
                    return "-"
                safe = escape(url)
                return f'<a href="{safe}" target="_blank" rel="noopener">{safe}</a>'

            profile_style_tags: list[str] = []
            profile_editorial_signals: list[str] = []
            market_key_networks: list[str] = []
            if focus_profile:
                try:
                    profile_style_tags = json.loads(focus_profile.style_tags_json or "[]")
                except Exception:
                    profile_style_tags = []
                try:
                    profile_editorial_signals = json.loads(focus_profile.editorial_signals_json or "[]")
                except Exception:
                    profile_editorial_signals = []
            if focus_market_context:
                try:
                    market_key_networks = json.loads(focus_market_context.key_networks_json or "[]")
                except Exception:
                    market_key_networks = []

            submissions_html = "".join(
                "<tr>"
                f"<td>{s.method.value}</td>"
                f"<td>{_link(s.url)}</td>"
                f"<td>{escape(s.email or '-')}</td>"
                f"<td>{'yes' if s.accepts_newcomers else 'no'}</td>"
                f"<td>{escape((s.requirements or '-')[:240])}</td>"
                "</tr>"
                for s in focus_submissions
            ) or "<tr><td colspan='5'>Keine Submission-Daten</td></tr>"

            people_html = "".join(
                "<tr>"
                f"<td>{escape(p.name or '-')}</td>"
                f"<td>{p.role.value}</td>"
                f"<td>{escape(p.show_name or '-')}</td>"
                f"<td>{escape(p.email or '-')}</td>"
                f"<td>{_link(p.linkedin_url or p.contact_url)}</td>"
                f"<td>{escape((p.musical_preferences or '-')[:120])}</td>"
                f"<td>{p.confidence:.2f}</td>"
                "</tr>"
                for p in focus_people
            ) or "<tr><td colspan='7'>Keine People-Daten</td></tr>"

            programs_html = "".join(
                "<tr>"
                f"<td>{escape(p.name)}</td>"
                f"<td>{escape((p.schedule or '-')[:80])}</td>"
                f"<td>{escape((p.description or '-')[:200])}</td>"
                "</tr>"
                for p in focus_programs
            ) or "<tr><td colspan='3'>Keine Programm-Daten</td></tr>"

            evidence_html = "".join(
                "<tr>"
                f"<td>{e.source_type.value}</td>"
                f"<td>{_link(e.source_url)}</td>"
                f"<td>{escape((e.raw_title or '-')[:120])}</td>"
                f"<td>{e.confidence:.2f}</td>"
                f"<td>{escape(str(e.created_at))}</td>"
                "</tr>"
                for e in focus_evidence
            ) or "<tr><td colspan='5'>Keine Evidence</td></tr>"

            market_context_html = ""
            if focus_market_context or focus_network_links:
                network_list = "".join(
                    f"<tr><td>{escape(network.name)}</td><td>{escape(link.relationship_type)}</td><td>{escape((network.coverage_note or link.notes or '-')[:180])}</td><td>{link.confidence:.2f}</td></tr>"
                    for link, network in focus_network_links
                ) or "<tr><td colspan='4'>Keine Netzwerk-Links</td></tr>"
                market_context_html = f"""
      <div class="card">
        <div class="k">Market Context</div>
        <div style="font-size:13px; margin-top:6px;">Market: {escape(focus_market_context.market_name if focus_market_context else (focus_station.country_code or '-'))}</div>
        <div style="font-size:13px; margin-top:6px;">Key Networks: {escape(', '.join(market_key_networks) or '-')}</div>
        <div style="font-size:13px; margin-top:6px;">Norms: {escape((focus_market_context.submission_norms or '-')[:220] if focus_market_context else '-')}</div>
        <div style="font-size:13px; margin-top:6px;">Outreach: {escape((focus_market_context.outreach_style or '-')[:220] if focus_market_context else '-')}</div>
        <div style="overflow:auto; max-height:180px; margin-top:8px;">
          <table>
            <thead><tr><th>Network</th><th>Relation</th><th>Coverage</th><th>Conf</th></tr></thead>
            <tbody>{network_list}</tbody>
          </table>
        </div>
      </div>
"""

            focus_panel = f"""
    <div class="card" style="margin-top:10px;">
      <div class="k">Fokus Sender</div>
      <div class="v" style="font-size:20px;">{escape(focus_station.canonical_name)} (ID {focus_station.id})</div>
      <div style="margin-top:6px;font-size:13px;">
        Country: {escape(focus_station.country_code or '-')} |
        City: {escape(focus_station.city or '-')} |
        Status: {focus_station.status.value} |
        Confidence: {focus_station.confidence_score:.2f} |
        Manual Confirmed: {"yes" if bool(getattr(focus_station, "manual_confirmed", False)) else "no"}
      </div>
      <div style="margin-top:6px;font-size:13px;">Website: {_link(focus_station.website_url)}<br/>Stream: {_link(focus_station.stream_url)}</div>
      <div style="margin-top:6px;font-size:13px;">Aliases: {escape(', '.join(focus_aliases)[:400] or '-')}</div>
      <div style="margin-top:6px;font-size:13px;">Genres: {escape(', '.join(focus_genres)[:400] or '-')}</div>
      <div style="margin-top:6px;font-size:13px;">Profile Style Tags: {escape(', '.join(profile_style_tags) or '-')}</div>
      <div style="margin-top:6px;font-size:13px;">Profile Editorial Signals: {escape(', '.join(profile_editorial_signals) or '-')}</div>
    </div>
    <div class="split">
      <div class="card">
        <div class="k">Submissions ({len(focus_submissions)})</div>
        <div style="overflow:auto; max-height:280px;">
          <table>
            <thead><tr><th>Method</th><th>URL</th><th>Email</th><th>Newcomers</th><th>Requirements</th></tr></thead>
            <tbody>{submissions_html}</tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <div class="k">People ({len(focus_people)})</div>
        <div style="overflow:auto; max-height:280px;">
          <table>
            <thead><tr><th>Name</th><th>Role</th><th>Show</th><th>Email</th><th>Contact</th><th>Prefs</th><th>Conf</th></tr></thead>
            <tbody>{people_html}</tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <div class="k">Programs ({len(focus_programs)})</div>
        <div style="overflow:auto; max-height:240px;">
          <table>
            <thead><tr><th>Name</th><th>Schedule</th><th>Description</th></tr></thead>
            <tbody>{programs_html}</tbody>
          </table>
        </div>
      </div>
      {market_context_html}
      <div class="card">
        <div class="k">Evidence ({len(focus_evidence)})</div>
        <div style="overflow:auto; max-height:240px;">
          <table>
            <thead><tr><th>Source</th><th>URL</th><th>Title</th><th>Conf</th><th>Created</th></tr></thead>
            <tbody>{evidence_html}</tbody>
          </table>
        </div>
      </div>
    </div>
"""

        return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Radio DB Dashboard</title>
  <style>
    :root {{
      --bg: #f5f7f2;
      --card: #ffffff;
      --ink: #102015;
      --muted: #516356;
      --line: #d8dfd3;
      --accent: #126d47;
      --warn: #7a1f2d;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: linear-gradient(140deg, #eef4ec, #f9f3eb); color: var(--ink); }}
    .wrap {{ max-width: 1400px; margin: 22px auto; padding: 0 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    .k {{ font-size: 12px; color: var(--muted); }}
    .v {{ font-size: 21px; font-weight: 650; }}
    .filters {{ margin: 12px 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
    label {{ font-size: 12px; color: var(--muted); display: block; }}
    input, select, button {{ border: 1px solid #bfcabf; border-radius: 8px; padding: 8px; background: #fff; }}
    button {{ background: var(--accent); color: #fff; border: 0; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); }}
    th, td {{ border-bottom: 1px solid #e4eadf; text-align: left; padding: 7px 8px; font-size: 13px; vertical-align: top; }}
    th {{ background: #eef3e8; position: sticky; top: 0; }}
    .mono {{ font-family: "IBM Plex Mono", "Consolas", monospace; font-size: 12px; white-space: pre-wrap; }}
    .split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0; }}
    .boost {{ display: grid; grid-template-columns: 2fr 1fr; gap: 10px; margin: 12px 0; }}
    .ok {{ color: var(--accent); }}
    .bad {{ color: var(--warn); }}
    .topnav {{ display:flex; gap:10px; align-items:center; margin-bottom:10px; }}
    .topnav a {{ text-decoration:none; color:#fff; background: var(--accent); padding:8px 12px; border-radius:8px; font-size:13px; }}
    .topnav a.secondary {{ background:#42584a; }}
    @media (max-width: 900px) {{ .grid, .split, .boost {{ grid-template-columns: 1fr; }} }}
  </style>
  <script>
    function navigatePreserveScroll(url) {{
      const current = window.scrollY || window.pageYOffset || 0;
      const target = new URL(url, window.location.origin);
      target.searchParams.set("scroll_y", String(current));
      window.location.href = target.pathname + target.search + target.hash;
    }}
    window.addEventListener("load", () => {{
      const params = new URLSearchParams(window.location.search);
      const raw = params.get("scroll_y");
      if (!raw) return;
      const y = parseInt(raw, 10);
      if (!Number.isFinite(y) || y < 0) return;
      window.scrollTo(0, y);
      params.delete("scroll_y");
      const next = window.location.pathname + (params.toString() ? "?" + params.toString() : "") + window.location.hash;
      window.history.replaceState(null, "", next);
    }});
  </script>
</head>
<body>
  <div class="wrap">
    <div class="topnav">
      <a href="/legacy">Radio DB</a>
      <a class="secondary" href="/email">E-Mail</a>
      <a class="secondary" href="/markets">Markets</a>
      <a class="secondary" href="/history">History</a>
      <a class="secondary" href="/agent">Agent Dashboard</a>
      <a href="/agent/run-country-now">Country Cycle Jetzt Starten</a>
    </div>
    <h1>Radio DB Dashboard</h1>
    <div class="k">Auto-Refresh alle 30s | UTC {monitor["now_utc"]}</div>
    {"<div class='k bad' style='margin-top:6px;'>Legacy ist read-only. Verwende die neue SPA fuer alle Aenderungen.</div>" if legacy_msg == "read_only" else ""}
    <div class="grid">
      <div class="card"><div class="k">Stations Active</div><div class="v">{monitor["stations_active"]}</div></div>
      <div class="card"><div class="k">Verified</div><div class="v">{monitor["stations_verified"]} ({monitor["verified_ratio_active"]:.1%})</div></div>
      <div class="card"><div class="k">Visible High-Priority</div><div class="v">{monitor["visible_high_priority"]}</div></div>
      <div class="card"><div class="k">Manual Confirmed</div><div class="v">{monitor["manual_confirmed_count"]}</div></div>
      <div class="card"><div class="k">Rejected (Noise)</div><div class="v">{monitor["stations_rejected"]}</div></div>
      <div class="card"><div class="k">Archived</div><div class="v">{monitor["stations_archived"]}</div></div>
      <div class="card"><div class="k">Stations Total (Raw)</div><div class="v">{monitor["stations_total"]}</div></div>
      <div class="card"><div class="k">Submission Coverage (Active)</div><div class="v">{monitor["stations_with_submission"]} / {monitor["stations_active"]} ({monitor["submission_coverage_active"]:.1%})</div></div>
      <div class="card"><div class="k">Without Submission</div><div class="v">{monitor["stations_without_submission"]}</div></div>
      <div class="card"><div class="k">Verified w/ Submission</div><div class="v">{monitor["verified_with_submission"]} ({monitor["verified_submission_coverage"]:.1%})</div></div>
      <div class="card"><div class="k">Submission Channels</div><div class="v">{monitor["submission_channels"]}</div></div>
      <div class="card"><div class="k">Stations w/ Submission</div><div class="v">{monitor["stations_with_submission"]}</div></div>
      <div class="card"><div class="k">Stations w/ Newcomer Signal</div><div class="v">{monitor["stations_with_newcomer_signal"]}</div></div>
      <div class="card"><div class="k">Stations w/ Decision Maker</div><div class="v">{monitor["stations_with_decision_maker"]}</div></div>
      <div class="card"><div class="k">Pitch Ready</div><div class="v">{monitor["pitch_ready_stations"]} ({monitor["pitch_ready_ratio"]:.1%})</div></div>
      <div class="card"><div class="k">People Entities</div><div class="v">{monitor["people_entities"]}</div></div>
      <div class="card"><div class="k">Profile Snapshots</div><div class="v">{monitor["profile_snapshots"]}</div></div>
      <div class="card"><div class="k">Evidence Today</div><div class="v">{monitor["evidence_24h"]}</div></div>
      <div class="card"><div class="k">Internal URLs Today</div><div class="v">{monitor["internal_urls_today"]}</div></div>
      <div class="card"><div class="k">Internal Stations Today</div><div class="v">{monitor["internal_stations_today"]}</div></div>
      <div class="card"><div class="k">Brave Calls</div><div class="v">{monitor["brave_calls_month"]} / {monitor["max_brave_calls_per_month"]}</div></div>
      <div class="card"><div class="k">Brave Answer Day</div><div class="v">{monitor["brave_answer_calls_day"]} / {monitor["max_brave_answer_calls_per_day"]}</div></div>
      <div class="card"><div class="k">Brave Answer Month</div><div class="v">{monitor["brave_answer_calls_month"]} / {monitor["max_brave_answer_calls_per_month"]}</div></div>
      <div class="card"><div class="k">Google CSE Day</div><div class="v">{monitor["google_cse_calls_day"]} / {monitor["max_google_cse_calls_per_day"]}</div></div>
      <div class="card"><div class="k">Google CSE Month</div><div class="v">{monitor["google_cse_calls_month"]} / {monitor["max_google_cse_calls_per_month"]}</div></div>
      <div class="card"><div class="k">Tavily Day</div><div class="v">{monitor["tavily_calls_day"]} / {monitor["max_tavily_calls_per_day"]}</div></div>
      <div class="card"><div class="k">Tavily Month</div><div class="v">{monitor["tavily_calls_month"]} / {monitor["max_tavily_calls_per_month"]}</div></div>
      <div class="card"><div class="k">Grok Day</div><div class="v">{monitor["grok_calls_day"]} / {monitor["max_grok_calls_per_day"]}</div></div>
      <div class="card"><div class="k">Grok Month</div><div class="v">{monitor["grok_calls_month"]} / {monitor["max_grok_calls_per_month"]}</div></div>
      <div class="card"><div class="k">Grok USD Day</div><div class="v">${monitor["grok_usd_day"]:.4f}</div></div>
      <div class="card"><div class="k">Grok USD Month</div><div class="v">${monitor["grok_usd_month"]:.4f} / ${monitor["max_grok_usd_per_month"]:.2f}</div></div>
      <div class="card"><div class="k">Linkup Day</div><div class="v">{monitor["linkup_calls_day"]} / {monitor["max_linkup_calls_per_day"]}</div></div>
      <div class="card"><div class="k">Linkup Month</div><div class="v">{monitor["linkup_calls_month"]} / {monitor["max_linkup_calls_per_month"]}</div></div>
      <div class="card"><div class="k">DuckDuckGo Day</div><div class="v">{monitor["duckduckgo_calls_day"]} / {monitor["max_duckduckgo_calls_per_day"]}</div></div>
      <div class="card"><div class="k">DuckDuckGo Month</div><div class="v">{monitor["duckduckgo_calls_month"]} / {monitor["max_duckduckgo_calls_per_month"]}</div></div>
      <div class="card"><div class="k">BOOST Status</div><div class="v">{'running' if monitor["boost_is_running"] else 'idle'}</div></div>
      <div class="card"><div class="k">BOOST Month ({escape(monitor["boost_month"])})</div><div class="v">${monitor["boost_month_spent_usd"]:.2f} / {monitor["boost_month_calls"]} calls</div></div>
      <div class="card"><div class="k">BOOST Last Run</div><div class="v">${monitor["boost_last_spent_usd"]:.2f} | {monitor["boost_last_calls"]} calls</div></div>
      <div class="card"><div class="k">BOOST Results</div><div class="v">{monitor["boost_last_results"]} hits | +{monitor["boost_last_submissions_added"]} subs</div></div>
      <div class="card"><div class="k">BOOST Live</div><div class="v">{monitor["boost_current_calls"]} calls | {monitor["boost_current_results"]} hits</div></div>
      <div class="card"><div class="k">BOOST DACH Coverage</div><div class="v">{monitor["boost_dach_unique_seen"]}/{monitor["boost_dach_eligible"]} ({monitor["boost_dach_progress_ratio"]:.1%})</div></div>
      <div class="card"><div class="k">BOOST Last Market Coverage</div><div class="v">{monitor["boost_last_market_unique_seen"]}/{monitor["boost_last_market_eligible"]} ({monitor["boost_last_market_progress_ratio"]:.1%})</div></div>
      <div class="card"><div class="k">High-Priority Status</div><div class="v">{'running' if monitor["high_priority_is_running"] else 'idle'}</div></div>
      <div class="card"><div class="k">High-Priority Last Run</div><div class="v">{monitor["high_priority_last_stations"]} stations | +{monitor["high_priority_last_submissions_added"]} subs</div></div>
      <div class="card"><div class="k">Scan Focus</div><div class="v">{'off' if not monitor["scan_focus_enabled"] else monitor["scan_focus_market"]}</div></div>
      <div class="card"><div class="k">Focus Progress</div><div class="v">{monitor["scan_focus_processed_ever"]}/{monitor["scan_focus_eligible_total"]} ({monitor["scan_focus_progress_ratio"]:.1%})</div></div>
      <div class="card"><div class="k">LLM Calls Today</div><div class="v">{monitor["llm_calls_today"]} / {monitor["max_llm_calls_per_day"]}</div></div>
      <div class="card"><div class="k">LLM Mode</div><div class="v">{escape(str(monitor["llm_provider_mode"]))} (xai key: {"yes" if monitor["xai_configured"] else "no"})</div></div>
      <div class="card"><div class="k">LLM USD Today</div><div class="v">${monitor["usd_spent_today_estimate"]:.4f} / ${monitor["max_daily_usd"]:.2f}</div></div>
      <div class="card"><div class="k">OpenAI Reconcile</div><div class="v">{'ok' if monitor["openai_reconcile_ok"] else 'error'} ({escape(monitor["openai_reconcile_sync_mode"])})</div></div>
      <div class="card"><div class="k">OpenAI Daily USD (API)</div><div class="v">${monitor["openai_reconcile_daily_total_usd"]:.4f}</div></div>
      <div class="card"><div class="k">Frontier Due</div><div class="v">{monitor["frontier_due"]}</div></div>
      <div class="card"><div class="k">Verify Candidates</div><div class="v">{monitor["candidate_stations"]}</div></div>
      <div class="card"><div class="k">Verify Untested</div><div class="v">{monitor["untested_candidate_stations"]}</div></div>
      <div class="card"><div class="k">Verify Progress</div><div class="v">{monitor["tested_candidate_stations"]}/{monitor["candidate_stations"]} ({monitor["verify_progress_ratio"]:.1%})</div></div>
      <div class="card"><div class="k">Verify Checkpoint</div><div class="v">ID {monitor["verify_checkpoint_last_station_id"]} ({'done' if monitor["verify_checkpoint_completed"] else 'running'})</div></div>
      <div class="card"><div class="k">Active w/o Website</div><div class="v">{monitor["active_without_website"]}</div></div>
      <div class="card"><div class="k">Touched ({monitor["station_enrich_cooldown_hours"]}h)</div><div class="v">{monitor["station_enrich_touched_recent"]}</div></div>
      <div class="card"><div class="k">Eligible Pending</div><div class="v">{monitor["station_enrich_pending_after_cooldown"]}</div></div>
    </div>

    <div class="split">
      <div class="card">
        <div class="k">Service</div>
        <div class="mono">service: {monitor["service_active"]}
timer: {monitor["timer_active"]} (enabled: {monitor["timer_enabled"]})
next: {monitor["timer_next"] or "-"}
internal service: {monitor["internal_service_active"]}
internal timer: {monitor["internal_timer_active"]} (enabled: {monitor["internal_timer_enabled"]})
internal next: {monitor["internal_timer_next"] or "-"}
people service: {monitor["people_service_active"]}
people timer: {monitor["people_timer_active"]} (enabled: {monitor["people_timer_enabled"]})
people next: {monitor["people_timer_next"] or "-"}
verify service: {monitor["verify_service_active"]}
verify timer: {monitor["verify_timer_active"]} (enabled: {monitor["verify_timer_enabled"]})
verify next: {monitor["verify_timer_next"] or "-"}
openai reconcile service: {monitor["openai_reconcile_service_active"]}
openai reconcile timer: {monitor["openai_reconcile_timer_active"]} (enabled: {monitor["openai_reconcile_timer_enabled"]})
openai reconcile next: {monitor["openai_reconcile_timer_next"] or "-"}
openai reconcile updated: {monitor["openai_reconcile_updated_at"]}
openai reconcile error: {monitor["openai_reconcile_error"] or "-"}
verify checkpoint updated: {monitor["verify_checkpoint_updated_at"]}</div>
      </div>
      <div class="card">
        <div class="k">Running Processes</div>
        <div class="mono">{monitor["running_processes"] or "none"}</div>
      </div>
      <div class="card" style="grid-column: 1 / -1;">
        <div class="k" style="color: var(--warn);">Recent Errors (journalctl radio-db.service)</div>
        <div class="mono">{monitor["recent_errors"] or "none / not accessible"}</div>
      </div>
    </div>

    <div class="boost">
      <div class="card">
        <div class="k">High-Priority Deep Dive (LLM, curated major stations)</div>
        <div style="margin-top: 6px; font-size: 13px;">
          Letzter Lauf: {monitor["high_priority_last_stations"]} stations, {monitor["high_priority_last_urls"]} urls, {monitor["high_priority_last_results"]} results,
          +{monitor["high_priority_last_submissions_added"]} submissions, +{monitor["high_priority_last_contacts_added"]} contacts, LLM calls: {monitor["high_priority_last_llm_calls"]}.
          Targets: {escape(str(monitor["high_priority_last_targets"] or "-"))}
        </div>
        {"<div class='k ok' style='margin-top:6px;'>High-priority run gestartet.</div>" if hp_msg == "started" else ""}
        {"<div class='k bad' style='margin-top:6px;'>High-priority run läuft bereits.</div>" if hp_msg == "already_running" else ""}
        {"<div class='k bad' style='margin-top:6px;'>Legacy ist read-only. Starte High-priority nur noch ueber neue Controls.</div>" if hp_msg == "read_only" else ""}
        {"<div class='k bad' style='margin-top:6px;'>Letzter Fehler: " + escape(str(monitor["high_priority_last_error"])) + "</div>" if monitor["high_priority_last_error"] else ""}
        <form method="get" action="/high-priority/start" style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:end;">
          <div>
            <label>Station Limit</label>
            <input type="number" min="1" max="50" name="station_limit" value="{monitor["high_priority_default_station_limit"]}" />
          </div>
          <div><button type="submit">Run High Priority</button></div>
        </form>
      </div>
      <div class="card">
        <div class="k">Global Scan Focus (API/Internal Scans)</div>
        <div style="margin-top: 6px; font-size: 13px;">
          Aktiver Fokus: <b>{'off' if not monitor["scan_focus_enabled"] else escape(str(monitor["scan_focus_market"]))}</b>
          | eligible: {monitor["scan_focus_eligible_total"]}
          | processed: {monitor["scan_focus_processed_ever"]}
          | pending: {monitor["scan_focus_pending"]}
          | updated: {escape(str(monitor["scan_focus_updated_at"]))}
        </div>
        {"<div class='k ok' style='margin-top:6px;'>Scan Focus aktualisiert.</div>" if scan_msg.startswith("set_") else ""}
        {"<div class='k bad' style='margin-top:6px;'>Scan Focus deaktiviert.</div>" if scan_msg == "disabled" else ""}
        {"<div class='k bad' style='margin-top:6px;'>Legacy ist read-only. Setze Scan Focus ueber Data Control.</div>" if scan_msg == "read_only" else ""}
        <form method="get" action="/scan-focus/set" style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:end;">
          <div>
            <label>Region</label>
            <select name="mode">
              <option value="off" {"selected" if not monitor["scan_focus_enabled"] else ""}>Off</option>
              <option value="international" {"selected" if monitor["scan_focus_enabled"] and monitor["scan_focus_market"] == "international" else ""}>International</option>
              <option value="dach" {"selected" if monitor["scan_focus_enabled"] and monitor["scan_focus_market"] == "dach" else ""}>DACH (DE/AT/CH)</option>
              <option value="anglo" {"selected" if monitor["scan_focus_enabled"] and monitor["scan_focus_market"] == "anglo" else ""}>Anglo (US/GB/CA/AU/NZ/IE)</option>
              <option value="eu_core" {"selected" if monitor["scan_focus_enabled"] and monitor["scan_focus_market"] == "eu_core" else ""}>EU Core</option>
            </select>
          </div>
          <div><button type="submit">Apply Focus</button></div>
        </form>
      </div>
      <div class="card">
        <div class="k">BOOST! Brave Priority Run (separates Budget, unabhängig vom Monatskontingent)</div>
        <div style="margin-top: 6px; font-size: 13px;">
          Startet einen gezielten Prioritätslauf (1 sinnvolle Brave-Abfrage je Sender-Domain, Qualitätfilter aktiv).
          Kostenmodell: ca. ${monitor["boost_effective_usd_per_call"]:.4f} pro Call.
          Letzter Fokus: {escape(str(monitor["boost_last_market_focus"]))} | Last Start: {escape(str(monitor["boost_last_started_at"]))} | Last Finish: {escape(str(monitor["boost_last_finished_at"]))}
        </div>
        <div style="margin-top: 6px; font-size: 13px;">
          Unique Progress DACH: {monitor["boost_dach_unique_seen"]}/{monitor["boost_dach_eligible"]} ({monitor["boost_dach_progress_ratio"]:.1%})
          | Last Market: {monitor["boost_last_market_unique_seen"]}/{monitor["boost_last_market_eligible"]} ({monitor["boost_last_market_progress_ratio"]:.1%})
        </div>
        <div style="margin-top: 6px; font-size: 13px;">
          No-repeat window: {monitor["boost_no_repeat_days"]} Tage | recent seen: {monitor["boost_recent_seen_count"]} | cursor(last market): ID {monitor["boost_last_market_cursor"]}
        </div>
        {"<div class='k ok' style='margin-top:6px;'>BOOST gestartet.</div>" if boost_msg == "started" else ""}
        {"<div class='k bad' style='margin-top:6px;'>BOOST läuft bereits.</div>" if boost_msg == "already_running" else ""}
        {"<div class='k bad' style='margin-top:6px;'>Legacy ist read-only. Starte Boost nur noch ueber neue Controls.</div>" if boost_msg == "read_only" else ""}
        {"<div class='k bad' style='margin-top:6px;'>Letzter Fehler: " + escape(str(monitor["boost_last_error"])) + "</div>" if monitor["boost_last_error"] else ""}
        <form method="get" action="/boost/start" style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:end;">
          <div>
            <label>Budget USD</label>
            <input type="number" step="0.1" min="0.1" max="100" name="budget_usd" value="{monitor["boost_default_budget_usd"]}" />
          </div>
          <div>
            <label>Marktfokus</label>
            <select name="market_focus">
              <option value="international">International</option>
              <option value="top_major">TOP MAJOR (kuratierte Liste)</option>
              <option value="dach">DACH (DE/AT/CH)</option>
              <option value="anglo">Anglo (US/GB/CA/AU/NZ/IE)</option>
              <option value="eu_core">EU Core</option>
            </select>
          </div>
          <div><button type="submit">BOOST!</button></div>
        </form>
      </div>
      <div class="card">
        <div class="k">BOOST Live / Last Delta</div>
        <div class="mono">live calls: {monitor["boost_current_calls"]}
live processed stations: {monitor["boost_current_stations_processed"]}
live results kept: {monitor["boost_current_results"]}
live submissions added: {monitor["boost_current_submissions_added"]}
live contacts added: {monitor["boost_current_contacts_added"]}
live candidate submissions: {monitor["boost_current_candidate_submissions"]}
live candidate contacts: {monitor["boost_current_candidate_contacts"]}
---
last calls: {monitor["boost_last_calls"]}
last processed stations: {monitor["boost_last_stations_processed"]}
last results kept: {monitor["boost_last_results"]}
last submissions added: {monitor["boost_last_submissions_added"]}
last contacts added: {monitor["boost_last_contacts_added"]}
last candidate submissions: {monitor["boost_last_candidate_submissions"]}
last candidate contacts: {monitor["boost_last_candidate_contacts"]}
last spent usd: ${monitor["boost_last_spent_usd"]:.4f}</div>
      </div>
    </div>

    <form class="filters" method="get">
      <div><label>Suche</label><input name="q" value="{escape(q)}" placeholder="name, city, website" /></div>
      <div><label>Country</label><input name="country" value="{escape(country)}" placeholder="DE, US..." /></div>
      <div>
        <label>Status</label>
        <select name="status">
          <option value="" {"selected" if not status else ""}>all</option>
          <option value="candidate" {"selected" if status == "candidate" else ""}>candidate</option>
          <option value="verified" {"selected" if status == "verified" else ""}>verified</option>
          <option value="archived" {"selected" if status == "archived" else ""}>archived</option>
          <option value="rejected" {"selected" if status == "rejected" else ""}>rejected</option>
        </select>
      </div>
      <div><label>Genre enthält</label><input name="genre" value="{escape(genre)}" placeholder="rock, jazz..." /></div>
      <div>
        <label>Submission</label>
        <select name="has_submission">
          <option value="any" {"selected" if has_submission == "any" else ""}>any</option>
          <option value="yes" {"selected" if has_submission == "yes" else ""}>yes</option>
          <option value="no" {"selected" if has_submission == "no" else ""}>no</option>
        </select>
      </div>
      <div>
        <label>People</label>
        <select name="has_people">
          <option value="any" {"selected" if has_people == "any" else ""}>any</option>
          <option value="yes" {"selected" if has_people == "yes" else ""}>yes</option>
          <option value="no" {"selected" if has_people == "no" else ""}>no</option>
        </select>
      </div>
      <div><label>Min Confidence (0-1)</label><input type="number" step="0.05" min="0" max="1" name="min_confidence" value="{min_confidence}" /></div>
      <div><label>Limit</label><input type="number" min="1" max="500" name="limit" value="{limit}" /></div>
      <div><label>Fokus Sender ID</label><input type="number" min="1" name="focus_station_id" value="{focus_station_id or ''}" /></div>
      <div><button type="submit">Filtern</button></div>
    </form>

    {focus_panel}

    <div class="k">Treffer: {len(stations)} / {total}</div>
    <div style="overflow:auto; max-height: 62vh;">
      <table>
        <thead>
          <tr>
            <th>X</th><th>ID</th><th>Name</th><th>Country</th><th>City</th><th>Status</th><th>Confidence</th><th>Priority</th><th>Manual</th><th>Genres</th><th>People</th><th>Submissions</th><th>Website</th><th>Fokus</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows) if rows else "<tr><td colspan='14'>Keine Treffer</td></tr>"}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return _serve_spa_file()

    @app.head("/", include_in_schema=False)
    def spa_root_head() -> FileResponse:
        return _serve_spa_file()

    @app.get("/{tracking_code}", include_in_schema=False, response_model=None)
    def short_press_release_redirect(
        tracking_code: str,
        request: Request,
        s: int | None = Query(default=None, ge=1),
    ):
        with SessionLocal() as db:
            redirect = redirect_short_press_release_code(tracking_code, request, s, db)
        if redirect is not None:
            return redirect
        return _serve_spa_file()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_catchall(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not_found")
        frontend_dist = _frontend_dist_dir().resolve()
        target = (frontend_dist / full_path).resolve()
        if target.is_file() and (target == frontend_dist or frontend_dist in target.parents):
            return FileResponse(target)
        return _serve_spa_file()

    return app

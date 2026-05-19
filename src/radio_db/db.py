from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from radio_db.config import settings


class Base(DeclarativeBase):
    pass


def is_sqlite_url(url: str | object) -> bool:
    return str(url).startswith("sqlite")


def is_postgres_url(url: str | object) -> bool:
    return str(url).startswith(("postgresql", "postgres"))


def is_sqlite_engine(candidate: Engine) -> bool:
    return candidate.dialect.name == "sqlite"


_engine_kwargs: dict[str, object] = {"future": True}
if is_sqlite_url(settings.database_url):
    _engine_kwargs["connect_args"] = {"timeout": 300}
elif is_postgres_url(settings.database_url):
    _engine_kwargs["pool_pre_ping"] = True
engine = create_engine(settings.database_url, **_engine_kwargs)

if is_sqlite_url(settings.database_url):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=300000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)


def init_db() -> None:
    from radio_db.models import model_registry

    model_registry()
    Base.metadata.create_all(bind=engine)
    _ensure_legacy_schema_compatibility()


def _ensure_legacy_schema_compatibility() -> None:
    # SQLite-only compatibility patch for older DB snapshots that miss
    # recently added manual confirmation fields.
    if not is_sqlite_engine(engine):
        return
    patch_plan = {
        "stations": [
            ("manual_confirmed", "manual_confirmed BOOLEAN DEFAULT 0"),
            ("manual_confirmed_at", "manual_confirmed_at DATETIME"),
            ("scan_outcome", "scan_outcome VARCHAR(64)"),
            ("scan_last_run_status", "scan_last_run_status VARCHAR(32)"),
            ("scan_last_scanned_at", "scan_last_scanned_at DATETIME"),
            ("scan_claim_token", "scan_claim_token VARCHAR(64)"),
            ("scan_claimed_at", "scan_claimed_at DATETIME"),
            ("scan_attempt_count", "scan_attempt_count INTEGER DEFAULT 0"),
            ("scan_last_error", "scan_last_error TEXT"),
            ("best_submission_route_type", "best_submission_route_type VARCHAR(64)"),
            ("best_submission_route_url", "best_submission_route_url VARCHAR(1024)"),
            ("best_submission_route_email", "best_submission_route_email VARCHAR(320)"),
            ("best_submission_route_confidence", "best_submission_route_confidence FLOAT"),
            ("best_submission_route_reason", "best_submission_route_reason TEXT"),
            ("best_submission_route_updated_at", "best_submission_route_updated_at DATETIME"),
            ("secondary_submission_routes_json", "secondary_submission_routes_json TEXT DEFAULT '[]'"),
        ],
        "submission_channels": [
            ("manual_confirmed", "manual_confirmed BOOLEAN DEFAULT 0"),
            ("manual_confirmed_at", "manual_confirmed_at DATETIME"),
        ],
        "station_contacts": [
            ("manual_confirmed", "manual_confirmed BOOLEAN DEFAULT 0"),
            ("manual_confirmed_at", "manual_confirmed_at DATETIME"),
        ],
        "station_people": [
            ("manual_confirmed", "manual_confirmed BOOLEAN DEFAULT 0"),
            ("manual_confirmed_at", "manual_confirmed_at DATETIME"),
        ],
        "contact_drafts": [
            ("campaign_id", "campaign_id INTEGER"),
        ],
        "outreach_campaigns": [
            ("press_release_url", "press_release_url VARCHAR(2048)"),
            ("tracking_code", "tracking_code VARCHAR(120)"),
        ],
        "candidate_rescan_queue": [
            ("claim_token", "claim_token VARCHAR(64)"),
            ("claimed_at", "claimed_at DATETIME"),
            ("attempt_count", "attempt_count INTEGER DEFAULT 0"),
            ("last_run_id", "last_run_id INTEGER"),
            ("last_assessment_id", "last_assessment_id INTEGER"),
            ("final_decision", "final_decision VARCHAR(32)"),
            ("path_quality", "path_quality VARCHAR(64)"),
            ("result_status", "result_status VARCHAR(32)"),
            ("notes", "notes TEXT"),
            ("evidence_json", "evidence_json TEXT DEFAULT '{}'"),
        ],
        "enrichment_queue": [
            ("api_budget_class", "api_budget_class VARCHAR(16) DEFAULT 'none'"),
            ("reason_codes_json", "reason_codes_json TEXT DEFAULT '[]'"),
            ("free_findings_json", "free_findings_json TEXT DEFAULT '{}'"),
            ("paid_plan_json", "paid_plan_json TEXT DEFAULT '{}'"),
            ("last_outcome", "last_outcome VARCHAR(64)"),
            ("attempt_count", "attempt_count INTEGER DEFAULT 0"),
            ("free_last_run_at", "free_last_run_at DATETIME"),
            ("paid_last_run_at", "paid_last_run_at DATETIME"),
            ("last_run_id", "last_run_id INTEGER"),
            ("error", "error TEXT"),
        ],
        "enrichment_runs": [
            ("api_calls_json", "api_calls_json TEXT DEFAULT '{}'"),
            ("cost_estimate_usd", "cost_estimate_usd FLOAT DEFAULT 0.0"),
            ("metrics_json", "metrics_json TEXT DEFAULT '{}'"),
            ("error", "error TEXT"),
            ("finished_at", "finished_at DATETIME"),
        ],
        "enrichment_findings": [
            ("source_name", "source_name VARCHAR(64)"),
            ("payload_json", "payload_json TEXT DEFAULT '{}'"),
            ("useful", "useful BOOLEAN DEFAULT 0"),
        ],
    }
    with engine.begin() as conn:
        for table_name, columns in patch_plan.items():
            table_info = conn.execute(text(f"PRAGMA table_info({table_name})")).all()
            if not table_info:
                continue
            existing = {str(row[1]) for row in table_info}
            for col_name, ddl in columns:
                if col_name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))

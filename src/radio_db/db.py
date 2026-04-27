from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from radio_db.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, autoflush=False, autocommit=False)


def init_db() -> None:
    from radio_db.models import model_registry

    model_registry()
    Base.metadata.create_all(bind=engine)
    _ensure_legacy_schema_compatibility()


def _ensure_legacy_schema_compatibility() -> None:
    # SQLite-only compatibility patch for older DB snapshots that miss
    # recently added manual confirmation fields.
    if not str(engine.url).startswith("sqlite"):
        return
    patch_plan = {
        "stations": [
            ("manual_confirmed", "manual_confirmed BOOLEAN DEFAULT 0"),
            ("manual_confirmed_at", "manual_confirmed_at DATETIME"),
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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from radio_db.db import Base, is_postgres_url, is_sqlite_url  # noqa: E402
from radio_db.models import model_registry  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy the Radio DB SQLAlchemy schema from SQLite into PostgreSQL."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("SQLITE_DATABASE_URL", "sqlite:///radio.db"),
        help="SQLite source URL. Default: sqlite:///radio.db",
    )
    parser.add_argument(
        "--target",
        default=os.getenv("POSTGRES_DATABASE_URL", ""),
        help="PostgreSQL target URL, or POSTGRES_DATABASE_URL.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Rows per insert batch. Default: 1000",
    )
    parser.add_argument(
        "--drop-target",
        action="store_true",
        help="Drop all ORM-managed target tables before creating and copying.",
    )
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="Truncate ORM-managed target tables before copying.",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Only create the target schema; do not copy rows.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only compare source and target row counts.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation for destructive target operations.",
    )
    return parser.parse_args()


def _chunked(rows: Iterable[dict], batch_size: int) -> Iterable[list[dict]]:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _clean_value(value: object) -> object:
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "")
    return value


def _clean_row(row: dict) -> dict:
    return {key: _clean_value(value) for key, value in row.items()}


def _require_urls(source_url: str, target_url: str) -> None:
    if not is_sqlite_url(source_url):
        raise SystemExit(f"source must be a sqlite URL, got: {source_url}")
    if not target_url:
        raise SystemExit("target is required: pass --target or set POSTGRES_DATABASE_URL")
    if not is_postgres_url(target_url):
        raise SystemExit(f"target must be a PostgreSQL URL, got: {target_url}")
    if source_url.rstrip("/") == target_url.rstrip("/"):
        raise SystemExit("source and target URLs must differ")


def _confirm_destructive(args: argparse.Namespace) -> None:
    if args.yes or (not args.drop_target and not args.truncate_target):
        return
    operation = "drop" if args.drop_target else "truncate"
    answer = input(f"About to {operation} ORM-managed tables in target DB. Continue? [y/N] ")
    if answer.strip().lower() != "y":
        raise SystemExit("aborted")


def _managed_tables() -> list[Table]:
    model_registry()
    return list(Base.metadata.sorted_tables)


def _source_column_names(source_engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(source_engine)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _target_table_names(tables: list[Table]) -> str:
    return ", ".join(f'"{table.name}"' for table in tables)


def _prepare_target(target_engine: Engine, tables: list[Table], args: argparse.Namespace) -> None:
    if args.drop_target:
        Base.metadata.drop_all(bind=target_engine, tables=list(reversed(tables)))
    Base.metadata.create_all(bind=target_engine)
    if args.truncate_target:
        with target_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {_target_table_names(tables)} RESTART IDENTITY CASCADE"))


def _copy_table(source_engine: Engine, target_engine: Engine, table: Table, batch_size: int) -> int:
    source_columns = _source_column_names(source_engine, table.name)
    if not source_columns:
        print(f"skip missing source table: {table.name}")
        return 0
    copy_columns = [column.name for column in table.columns if column.name in source_columns]
    skipped = sorted(source_columns - set(copy_columns))
    if skipped:
        print(f"warning: {table.name}: ignoring source-only columns: {', '.join(skipped)}")

    source_table = Table(table.name, MetaData(), autoload_with=source_engine)
    order_columns = [source_table.c.id] if "id" in source_table.c else []
    stmt = select(*(source_table.c[name] for name in copy_columns))
    if order_columns:
        stmt = stmt.order_by(*order_columns)

    total = 0
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        rows = (
            _clean_row(dict(row))
            for row in source_conn.execution_options(stream_results=True)
            .execute(stmt)
            .mappings()
        )
        for batch in _chunked(rows, batch_size):
            target_conn.execute(table.insert(), batch)
            total += len(batch)
    print(f"copied {table.name}: {total}")
    return total


def _reset_postgres_sequences(target_engine: Engine, tables: list[Table]) -> None:
    with target_engine.begin() as conn:
        for table in tables:
            if "id" not in table.c:
                continue
            sequence_name = conn.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table.name, "column_name": "id"},
            ).scalar()
            if not sequence_name:
                continue
            max_id = conn.execute(select(func.max(table.c.id))).scalar()
            if max_id is None:
                conn.execute(text("SELECT setval(:sequence_name, 1, false)"), {"sequence_name": sequence_name})
            else:
                conn.execute(
                    text("SELECT setval(:sequence_name, :max_id, true)"),
                    {"sequence_name": sequence_name, "max_id": int(max_id)},
                )


def _row_counts(engine: Engine, tables: list[Table]) -> dict[str, int]:
    counts: dict[str, int] = {}
    inspector = inspect(engine)
    with engine.connect() as conn:
        for table in tables:
            if not inspector.has_table(table.name):
                counts[table.name] = 0
                continue
            counts[table.name] = int(conn.execute(select(func.count()).select_from(table)).scalar() or 0)
    return counts


def _validate_counts(source_engine: Engine, target_engine: Engine, tables: list[Table]) -> bool:
    source_counts = _row_counts(source_engine, tables)
    target_counts = _row_counts(target_engine, tables)
    ok = True
    print("\nrow count validation")
    for table in tables:
        source_count = source_counts[table.name]
        target_count = target_counts[table.name]
        status = "ok" if source_count == target_count else "mismatch"
        if status != "ok":
            ok = False
        print(f"{status:8} {table.name:32} source={source_count} target={target_count}")
    return ok


def main() -> int:
    args = parse_args()
    _require_urls(args.source, args.target)
    _confirm_destructive(args)

    source_engine = create_engine(args.source, future=True)
    target_engine = create_engine(args.target, future=True, pool_pre_ping=True)
    tables = _managed_tables()

    try:
        if not args.validate_only:
            _prepare_target(target_engine, tables, args)
        if not args.schema_only and not args.validate_only:
            for table in tables:
                _copy_table(source_engine, target_engine, table, max(1, args.batch_size))
            _reset_postgres_sequences(target_engine, tables)
        if not args.schema_only:
            return 0 if _validate_counts(source_engine, target_engine, tables) else 2
        return 0
    except SQLAlchemyError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from datetime import datetime, timedelta
from math import log, sqrt

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from radio_db.models.entities import CrawlFrontier


def ucb_priority(run_count: int, yield_new: int, total_runs: int, exploration_bonus: float = 2.0) -> float:
    if run_count == 0:
        return 1_000.0
    exploitation = yield_new / max(run_count, 1)
    exploration = exploration_bonus * sqrt(log(max(total_runs, 1) + 1) / run_count)
    return exploitation + exploration


def refresh_priorities(session: Session) -> None:
    all_queries = session.scalars(select(CrawlFrontier)).all()
    total_runs = sum(q.run_count for q in all_queries)
    for query in all_queries:
        query.priority_score = ucb_priority(
            run_count=query.run_count,
            yield_new=query.yield_new,
            total_runs=total_runs,
            exploration_bonus=query.exploration_bonus,
        )
    session.commit()


def claim_queries(session: Session, batch_size: int) -> list[CrawlFrontier]:
    now = datetime.utcnow()
    stmt: Select[tuple[CrawlFrontier]] = (
        select(CrawlFrontier)
        .where((CrawlFrontier.next_run_at.is_(None)) | (CrawlFrontier.next_run_at <= now))
        .order_by(CrawlFrontier.priority_score.desc(), CrawlFrontier.created_at.asc())
        .limit(batch_size)
    )
    queries = session.scalars(stmt).all()
    for q in queries:
        q.last_run_at = now
        q.next_run_at = now + timedelta(days=7)
    session.commit()
    return queries

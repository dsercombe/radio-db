from __future__ import annotations

import json

import typer
from rich import print

from radio_db.db import SessionLocal, init_db
from radio_db.dashboard import create_app
from radio_db.services.editorial_enrichment import run_station_editorial_enrichment
from radio_db.services.people import (
    build_station_people,
    cleanup_people_quality,
    discover_people_from_search,
    enrich_people_preferences,
    search_station_people,
)
from radio_db.services.profiles import build_station_profiles
from radio_db.services.station_quality import run_station_quality_verification
from radio_db.services.verification import verify_real_stations
from radio_db.services.pipeline import (
    enrich_priority_sources,
    cleanup_non_station_records,
    ingest_free_sources_full,
    ingest_free_sources,
    ingest_seed_sources,
    run_discovery,
    run_country_discovery_cycle,
    seed_frontier,
    station_enrich_search,
    stats,
)
from radio_db.services.forms import (
    cleanup_stations_by_focus_keywords,
    cleanup_stations_by_entrypoint_rules,
    cleanup_stations_by_meta_rules,
    run_country_form_cycle,
    scan_submission_forms,
)
from radio_db.services.forms_agent import (
    backfill_rejected_scan_markers,
    release_stale_rejected_scan_claims,
    run_main_scan_cycle,
    run_rejected_scan_cycle,
    run_rejected_quality_review,
    run_rejected_scan_dispatch,
    run_rejected_scan_discovery,
    run_rejected_scan_continuous,
    start_manual_scan_run,
)
from radio_db.services.candidate_rescan import (
    candidate_rescan_stats,
    list_candidate_rescan_queue,
    run_candidate_rescan_batch,
    run_candidate_rescan_continuous,
    seed_candidate_rescan_queue,
    seed_enrichment_reassessment_queue,
)
from radio_db.services.enrichment import (
    enrichment_queue_stats,
    evaluate_enrichment_golden_set,
    run_free_enrichment_scan,
    run_paid_enrichment_scan,
    seed_enrichment_queue,
)
from radio_db.services.dynamic_official_discovery import (
    phase3_dynamic_queue_status,
    run_dynamic_official_discovery_batch,
    run_phase3_dynamic_queue,
    seed_phase3_dynamic_queue,
)
from radio_db.services.route_discovery import (
    route_discovery_candidates,
    run_route_discovery_batch,
)
from radio_db.services.submission_routes import backfill_primary_submission_routes
from radio_db.services.phase2_route_queue import (
    phase2_route_queue_status,
    run_phase2_route_worker,
    seed_phase2_route_queue,
)
from radio_db.services.scan_jobs import (
    SUPPORTED_JOB_TYPES,
    enqueue_scan_job,
    run_scan_worker,
    scan_job_overview,
)

app = typer.Typer(help="Radio Database Agent CLI")


@app.command("init-db")
def init_db_cmd() -> None:
    init_db()
    print("[green]Database initialized[/green]")


@app.command("seed-frontier")
def seed_frontier_cmd() -> None:
    with SessionLocal() as session:
        inserted = seed_frontier(session)
    print(f"[green]Frontier seeded[/green] inserted={inserted}")


@app.command("ingest-seeds")
def ingest_seeds_cmd(limit: int = typer.Option(300, min=1, max=5000)) -> None:
    with SessionLocal() as session:
        result = ingest_seed_sources(session, limit=limit)
    print("[green]Seed ingestion complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("discover")
def discover_cmd(max_queries: int = typer.Option(25, min=1, max=500)) -> None:
    with SessionLocal() as session:
        result = run_discovery(session, max_queries=max_queries)
    print("[green]Discovery run complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("run-country-discovery-cycle")
def run_country_discovery_cycle_cmd(
    max_queries: int = typer.Option(8, min=1, max=100),
    country: str = typer.Option("", help="Optional fixed country (ISO), sonst zyklischer Cursor"),
    countries: str = typer.Option("", help="Optional comma-separated Liste fuer den Cursor"),
    include_linkup: bool = typer.Option(True, help="Linkup in Multi-Source Discovery einbeziehen"),
    min_confidence: float = typer.Option(0.35, min=0.0, max=1.0),
) -> None:
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()] if countries else None
    with SessionLocal() as session:
        result = run_country_discovery_cycle(
            session=session,
            max_queries=max_queries,
            country=country or None,
            countries=country_list,
            include_linkup=include_linkup,
            min_confidence=min_confidence,
        )
    print("[green]Country discovery cycle complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("enqueue-scan-job")
def enqueue_scan_job_cmd(
    job_type: str = typer.Argument(..., help=f"One of: {', '.join(sorted(SUPPORTED_JOB_TYPES))}"),
    payload_json: str = typer.Option("{}", help="JSON payload with bounded job options"),
    priority: int = typer.Option(100, min=1, max=1000),
    requested_by: str = typer.Option("cli"),
) -> None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("payload-json must decode to a JSON object")
    with SessionLocal() as session:
        job = enqueue_scan_job(
            session=session,
            job_type=job_type,
            payload=payload,
            priority=priority,
            requested_by=requested_by,
        )
    print("[green]Scan job queued[/green]")
    print(json.dumps({"id": job.id, "job_type": job.job_type, "status": job.status, "priority": job.priority}, indent=2))


@app.command("scan-worker")
def scan_worker_cmd(
    once: bool = typer.Option(False, help="Process at most one available job and exit"),
    sleep_seconds: int = typer.Option(10, min=1, max=300),
    worker_id: str = typer.Option("", help="Optional stable worker id"),
) -> None:
    result = run_scan_worker(
        SessionLocal,
        once=once,
        sleep_seconds=sleep_seconds,
        worker_id=worker_id or None,
    )
    print(json.dumps(result, indent=2))


@app.command("scan-job-stats")
def scan_job_stats_cmd() -> None:
    with SessionLocal() as session:
        result = scan_job_overview(session)
    print(json.dumps(result, indent=2))


@app.command("ingest-free")
def ingest_free_cmd(limit: int = typer.Option(1000, min=1, max=20000)) -> None:
    with SessionLocal() as session:
        result = ingest_free_sources(session, limit=limit)
    print("[green]Free-source ingestion complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("ingest-free-full")
def ingest_free_full_cmd(
    rb_batch_size: int = typer.Option(1500, min=100, max=10000),
    wikidata_batch_size: int = typer.Option(500, min=50, max=5000),
    max_rb_pages: int | None = typer.Option(None, min=1, max=100000),
    max_wikidata_pages: int | None = typer.Option(None, min=1, max=100000),
    reset_checkpoint: bool = typer.Option(False, help="Start from offset 0 and overwrite checkpoint state"),
) -> None:
    with SessionLocal() as session:
        result = ingest_free_sources_full(
            session=session,
            rb_batch_size=rb_batch_size,
            wikidata_batch_size=wikidata_batch_size,
            max_rb_pages=max_rb_pages,
            max_wikidata_pages=max_wikidata_pages,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Full free-source harvest complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("enrich-priority")
def enrich_priority_cmd(max_queries: int = typer.Option(25, min=1, max=500)) -> None:
    with SessionLocal() as session:
        result = enrich_priority_sources(session=session, max_queries=max_queries)
    print("[green]Priority enrichment run complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("cleanup-nonstations")
def cleanup_nonstations_cmd(
    limit: int = typer.Option(50000, min=1, max=500000),
    apply: bool = typer.Option(False, help="If true, write rejected status to DB"),
) -> None:
    with SessionLocal() as session:
        result = cleanup_non_station_records(session=session, limit=limit, dry_run=not apply)
    print("[green]Non-station cleanup finished[/green]")
    print(json.dumps(result, indent=2))


@app.command("verify-real-stations")
def verify_real_stations_cmd(
    limit: int = typer.Option(2000, min=1, max=50000),
    station_id: int | None = typer.Option(None, min=1),
    only_candidates: bool = typer.Option(True, help="Only verify candidate stations"),
    fetch_homepages: bool = typer.Option(True, help="Fetch station homepages for free signal checks"),
    max_page_fetches: int = typer.Option(300, min=0, max=20000),
    apply: bool = typer.Option(False, help="If true, reject low-score stations"),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor so each station is processed once"),
    reset_checkpoint: bool = typer.Option(False, help="Reset verification cursor back to station_id 0"),
) -> None:
    with SessionLocal() as session:
        result = verify_real_stations(
            session=session,
            limit=limit,
            station_id=station_id,
            only_candidates=only_candidates,
            fetch_homepages=fetch_homepages,
            max_page_fetches=max_page_fetches,
            apply=apply,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Station verification run complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("stats")
def stats_cmd() -> None:
    with SessionLocal() as session:
        result = stats(session)
    print(json.dumps(result, indent=2))


@app.command("build-profiles")
def build_profiles_cmd(
    limit: int = typer.Option(500, min=1, max=50000),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    with SessionLocal() as session:
        result = build_station_profiles(session=session, limit=limit, station_id=station_id)
    print("[green]Profile snapshots built[/green]")
    print(json.dumps(result, indent=2))


@app.command("build-people")
def build_people_cmd(
    limit: int = typer.Option(1000, min=1, max=50000),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    with SessionLocal() as session:
        result = build_station_people(session=session, limit=limit, station_id=station_id)
    print("[green]People index updated[/green]")
    print(json.dumps(result, indent=2))


@app.command("people-search")
def people_search_cmd(
    q: str = typer.Option("", help="Name/show/email/preferences query"),
    role: str = typer.Option("", help="host|dj|producer|music_director|program_director|editor|unknown"),
    station_query: str = typer.Option("", help="Filter by station name"),
    min_confidence: float = typer.Option(0.0, min=0.0, max=1.0),
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    with SessionLocal() as session:
        result = search_station_people(
            session=session,
            q=q,
            role=role,
            station_query=station_query,
            min_confidence=min_confidence,
            limit=limit,
        )
    print(json.dumps(result, indent=2))


@app.command("people-discovery")
def people_discovery_cmd(
    station_limit: int = typer.Option(200, min=1, max=50000),
    queries_per_station: int = typer.Option(2, min=1, max=5),
    min_confidence: float = typer.Option(0.35, min=0.0, max=1.0),
) -> None:
    with SessionLocal() as session:
        result = discover_people_from_search(
            session=session,
            station_limit=station_limit,
            queries_per_station=queries_per_station,
            min_confidence=min_confidence,
        )
    print("[green]People discovery complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("people-enrich-preferences")
def people_enrich_preferences_cmd(
    people_limit: int = typer.Option(120, min=1, max=50000),
    queries_per_person: int = typer.Option(1, min=1, max=2),
    min_hits_for_update: int = typer.Option(1, min=1, max=5),
) -> None:
    with SessionLocal() as session:
        result = enrich_people_preferences(
            session=session,
            people_limit=people_limit,
            queries_per_person=queries_per_person,
            min_hits_for_update=min_hits_for_update,
        )
    print("[green]People preference enrichment complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("station-enrich-search")
def station_enrich_search_cmd(
    station_limit: int = typer.Option(200, min=1, max=50000),
    queries_per_station: int = typer.Option(2, min=1, max=5),
    min_confidence: float = typer.Option(0.35, min=0.0, max=1.0),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    with SessionLocal() as session:
        result = station_enrich_search(
            session=session,
            station_limit=station_limit,
            queries_per_station=queries_per_station,
            min_confidence=min_confidence,
            station_id=station_id,
        )
    print("[green]Station enrichment search complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("scan-submission-forms")
def scan_submission_forms_cmd(
    station_limit: int = typer.Option(50, min=1, max=5000),
    country: str = typer.Option("", help="ISO country code, z. B. DE"),
    station_id: int | None = typer.Option(None, min=1),
    mode: str = typer.Option("read", help="read|fill_simulation"),
    max_forms_per_station: int = typer.Option(5, min=1, max=20),
) -> None:
    with SessionLocal() as session:
        result = scan_submission_forms(
            session=session,
            station_limit=station_limit,
            country=country or None,
            station_id=station_id,
            mode=mode,
            max_forms_per_station=max_forms_per_station,
        )
    print("[green]Submission form scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("run-country-form-cycle")
def run_country_form_cycle_cmd(
    station_limit: int = typer.Option(50, min=1, max=5000),
    mode: str = typer.Option("read", help="read|fill_simulation"),
    max_forms_per_station: int = typer.Option(5, min=1, max=20),
    countries: str = typer.Option("", help="Comma-separated country codes, z. B. DE,AT,CH"),
) -> None:
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()] if countries else None
    with SessionLocal() as session:
        result = run_country_form_cycle(
            session=session,
            station_limit=station_limit,
            mode=mode,
            max_forms_per_station=max_forms_per_station,
            countries=country_list,
        )
    print("[green]Country form cycle complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("agent-manual-scan")
def agent_manual_scan_cmd(
    station_id: int = typer.Option(..., min=1),
    target_url: str = typer.Option("", help="Optional konkrete URL fuer den Scan"),
    max_pages: int = typer.Option(1, min=1, max=5),
    force_rescan: bool = typer.Option(False, help="Neue FormRecipe-Version auch fuer bekannte Formulare erzeugen"),
) -> None:
    with SessionLocal() as session:
        run = start_manual_scan_run(
            session=session,
            station_id=station_id,
            target_url=target_url or None,
            max_pages=max_pages,
            force_rescan=force_rescan,
        )
    print("[green]Manual agent scan complete[/green]")
    print(json.dumps({"run_id": run.id, "status": run.status, "state": run.current_state}, indent=2))


@app.command("scan-rejected-cycle")
def scan_rejected_cycle_cmd(
    station_limit: int = typer.Option(20, min=1, max=5000),
    max_pages: int = typer.Option(3, min=1, max=10),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor over rejected stations"),
    reset_checkpoint: bool = typer.Option(False, help="Reset cursor back to station_id 0"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_rejected_scan_cycle(
            session=session,
            station_limit=station_limit,
            max_pages=max_pages,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Rejected scan cycle complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("scan-rejected-dispatch")
def scan_rejected_dispatch_cmd(
    station_limit: int = typer.Option(45, min=1, max=5000),
    max_pages: int = typer.Option(3, min=1, max=10),
    workers: int = typer.Option(3, min=1, max=12),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor over rejected stations"),
    reset_checkpoint: bool = typer.Option(False, help="Reset cursor back to station_id 0"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_rejected_scan_dispatch(
            session=session,
            station_limit=station_limit,
            max_pages=max_pages,
            workers=workers,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Rejected dispatch scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("scan-rejected-discovery")
def scan_rejected_discovery_cmd(
    station_limit: int = typer.Option(45, min=1, max=5000),
    max_pages: int = typer.Option(3, min=1, max=10),
    workers: int = typer.Option(3, min=1, max=12),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor over rejected stations"),
    reset_checkpoint: bool = typer.Option(False, help="Reset cursor back to station_id 0"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_rejected_scan_discovery(
            session=session,
            station_limit=station_limit,
            max_pages=max_pages,
            workers=workers,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Rejected discovery scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("scan-rejected-continuous")
def scan_rejected_continuous_cmd(
    station_limit: int = typer.Option(45, min=1, max=5000),
    max_pages: int = typer.Option(3, min=1, max=10),
    workers: int = typer.Option(4, min=1, max=12),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor over rejected stations"),
    reset_checkpoint: bool = typer.Option(False, help="Reset cursor back to station_id 0"),
) -> None:
    init_db()
    result = run_rejected_scan_continuous(
        session_factory=SessionLocal,
        station_limit=station_limit,
        max_pages=max_pages,
        workers=workers,
        use_checkpoint=use_checkpoint,
        reset_checkpoint=reset_checkpoint,
    )
    print("[green]Rejected continuous scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("review-rejected-quality")
def review_rejected_quality_cmd(
    limit: int = typer.Option(30, min=1, max=1000),
    provider: str = typer.Option("", help="Optional quality provider override"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_rejected_quality_review(
            session=session,
            limit=limit,
            provider=provider or None,
        )
    print("[green]Rejected quality review complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("reset-rejected-scan-claims")
def reset_rejected_scan_claims_cmd(
    older_than_minutes: int = typer.Option(15, min=1, max=1440, help="Release stale claims older than this age"),
) -> None:
    init_db()
    with SessionLocal() as session:
        released = release_stale_rejected_scan_claims(session=session, older_than_seconds=older_than_minutes * 60)
    print("[green]Rejected scan claims reset complete[/green]")
    print(json.dumps({"released": released, "older_than_minutes": older_than_minutes}, indent=2))


@app.command("backfill-rejected-scan-markers")
def backfill_rejected_scan_markers_cmd(
    limit: int | None = typer.Option(None, min=1, max=1000000, help="Optional cap for backfill rows"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = backfill_rejected_scan_markers(session=session, limit=limit)
    print("[green]Rejected scan marker backfill complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("seed-candidate-rescan-queue")
def seed_candidate_rescan_queue_cmd(
    queue_db_path: str = typer.Option("queue_dbs/radio-rejected-45319.db", help="Rejected queue DB path"),
    pool: str = typer.Option(
        "all",
        help="all|promoted_rejected|needs_review_high_score|needs_review_all|llm_review_all|queue_candidates_unscanned|active_main_rescan",
    ),
    min_review_score: float = typer.Option(70.0, min=0.0, max=100.0),
    reset_existing: bool = typer.Option(False, help="Delete existing rows for this pool before seeding"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = seed_candidate_rescan_queue(
            session=session,
            queue_db_path=queue_db_path,
            pool=pool,
            min_review_score=min_review_score,
            reset_existing=reset_existing,
        )
    print("[green]Candidate rescan queue seeded[/green]")
    print(json.dumps(result, indent=2))


@app.command("candidate-rescan-stats")
def candidate_rescan_stats_cmd() -> None:
    init_db()
    with SessionLocal() as session:
        result = candidate_rescan_stats(session=session)
    print(json.dumps(result, indent=2))


@app.command("seed-enrichment-reassessment-queue")
def seed_enrichment_reassessment_queue_cmd(
    min_new_evidence: int = typer.Option(5, min=1, max=100, help="Minimum new enrichment evidence since last LLM quality assessment"),
    reset_existing: bool = typer.Option(False, help="Delete existing enrichment_reassessment rows before seeding"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = seed_enrichment_reassessment_queue(
            session=session,
            min_new_evidence=min_new_evidence,
            reset_existing=reset_existing,
        )
    print("[green]Enrichment reassessment queue seeded[/green]")
    print(json.dumps(result, indent=2))


@app.command("list-candidate-rescan-queue")
def list_candidate_rescan_queue_cmd(
    status: str = typer.Option("pending", help="Queue status filter; empty for all"),
    pool: str = typer.Option("", help="Optional source_pool filter"),
    limit: int = typer.Option(50, min=1, max=1000),
    include_archived: bool = typer.Option(False, help="Include ARCHIVED main stations"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = list_candidate_rescan_queue(
            session=session,
            status=status,
            pool=pool,
            limit=limit,
            include_archived=include_archived,
        )
    print(json.dumps(result, indent=2))


@app.command("run-candidate-rescan-batch")
def run_candidate_rescan_batch_cmd(
    limit: int = typer.Option(10, min=1, max=500),
    pool: str = typer.Option("", help="Optional source_pool filter"),
    max_pages: int = typer.Option(6, min=1, max=20),
    provider: str = typer.Option("", help="Optional quality provider override"),
    apply_status: bool = typer.Option(False, help="Apply final status changes after rescan"),
    promote_contact_only: bool = typer.Option(
        False,
        help="Allow contact-email-only rows to become VERIFIED with lower confidence; keep false for CANDIDATE marking",
    ),
    dry_run: bool = typer.Option(True, help="Only show the selected rows; do not scan"),
    include_archived: bool = typer.Option(False, help="Include ARCHIVED main stations"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_candidate_rescan_batch(
            session=session,
            limit=limit,
            pool=pool,
            max_pages=max_pages,
            provider=provider or None,
            apply_status=apply_status,
            promote_contact_only=promote_contact_only,
            dry_run=dry_run,
            include_archived=include_archived,
        )
    print("[green]Candidate rescan batch complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("run-candidate-rescan-continuous")
def run_candidate_rescan_continuous_cmd(
    limit: int = typer.Option(12, min=1, max=5000),
    pool: str = typer.Option("", help="Optional source_pool filter"),
    max_pages: int = typer.Option(6, min=1, max=20),
    workers: int = typer.Option(3, min=1, max=12),
    provider: str = typer.Option("", help="Optional quality provider override"),
    apply_status: bool = typer.Option(False, help="Apply final status changes after rescan"),
    promote_contact_only: bool = typer.Option(
        False,
        help="Allow contact-email-only rows to become VERIFIED with lower confidence; keep false for CANDIDATE marking",
    ),
    include_archived: bool = typer.Option(False, help="Include ARCHIVED main stations"),
) -> None:
    init_db()
    result = run_candidate_rescan_continuous(
        session_factory=SessionLocal,
        limit=limit,
        pool=pool,
        max_pages=max_pages,
        workers=workers,
        provider=provider or None,
        apply_status=apply_status,
        promote_contact_only=promote_contact_only,
        include_archived=include_archived,
    )
    print("[green]Candidate rescan continuous complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("seed-enrichment-queue")
def seed_enrichment_queue_cmd(
    limit: int = typer.Option(500, min=1, max=50000),
    min_priority: float = typer.Option(35.0, min=0.0, max=500.0),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = seed_enrichment_queue(session=session, limit=limit, min_priority=min_priority, station_id=station_id)
    print("[green]Enrichment queue seeded[/green]")
    print(json.dumps(result, indent=2))


@app.command("enrichment-free-scan")
def enrichment_free_scan_cmd(
    limit: int = typer.Option(100, min=1, max=5000),
    max_urls_per_station: int = typer.Option(12, min=1, max=100),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_free_enrichment_scan(
            session=session,
            limit=limit,
            max_urls_per_station=max_urls_per_station,
            station_id=station_id,
        )
    print("[green]Free enrichment scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("enrichment-paid-scan")
def enrichment_paid_scan_cmd(
    limit: int = typer.Option(20, min=1, max=1000),
    station_id: int | None = typer.Option(None, min=1),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_paid_enrichment_scan(session=session, limit=limit, station_id=station_id)
    print("[green]Paid enrichment scan complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("enrichment-evaluate-golden-set")
def enrichment_evaluate_golden_set_cmd(
    config_path: str = typer.Option("config/enrichment_golden_set.json"),
    limit: int | None = typer.Option(None, min=1, max=1000),
    run_paid: bool = typer.Option(False),
    max_urls_per_station: int = typer.Option(6, min=1, max=100),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = evaluate_enrichment_golden_set(
            session=session,
            config_path=config_path,
            limit=limit,
            run_paid=run_paid,
            max_urls_per_station=max_urls_per_station,
        )
    print("[green]Enrichment golden set evaluation complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("enrichment-queue-stats")
def enrichment_queue_stats_cmd() -> None:
    init_db()
    with SessionLocal() as session:
        result = enrichment_queue_stats(session=session)
    print(json.dumps(result, indent=2))


@app.command("phase3-dynamic-official-discovery")
def phase3_dynamic_official_discovery_cmd(
    limit: int = typer.Option(5, min=1, max=100),
    station_id: int | None = typer.Option(None, min=1),
    max_links: int = typer.Option(120, min=20, max=500),
    max_deep_pages: int = typer.Option(8, min=1, max=30),
    max_gemini_calls: int = typer.Option(10, min=0, max=200),
    min_confidence: float = typer.Option(0.7, min=0.0, max=1.0),
    fresh_days: int = typer.Option(14, min=1, max=365),
    apply: bool = typer.Option(False, help="Persist stronger best-route fields and submission channel records"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_dynamic_official_discovery_batch(
            session=session,
            limit=limit,
            station_id=station_id,
            max_links=max_links,
            max_deep_pages=max_deep_pages,
            max_gemini_calls=max_gemini_calls,
            min_confidence=min_confidence,
            fresh_days=fresh_days,
            apply=apply,
        )
    print("[green]Phase 3 dynamic official discovery complete[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("seed-phase3-dynamic-queue")
def seed_phase3_dynamic_queue_cmd(
    limit: int = typer.Option(500, min=1, max=5000),
    min_confidence: float = typer.Option(0.65, min=0.0, max=1.0),
    reset_pending: bool = typer.Option(False, help="Delete pending/error Phase-3 dynamic rows before seeding"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = seed_phase3_dynamic_queue(
            session=session,
            limit=limit,
            min_confidence=min_confidence,
            reset_pending=reset_pending,
        )
    print("[green]Phase 3 dynamic queue seeded[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("phase3-dynamic-queue-status")
def phase3_dynamic_queue_status_cmd() -> None:
    init_db()
    with SessionLocal() as session:
        result = phase3_dynamic_queue_status(session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("run-phase3-dynamic-queue")
def run_phase3_dynamic_queue_cmd(
    limit: int = typer.Option(25, min=1, max=1000),
    max_gemini_calls: int = typer.Option(50, min=0, max=5000),
    max_links: int = typer.Option(120, min=20, max=500),
    max_deep_pages: int = typer.Option(8, min=1, max=30),
    apply: bool = typer.Option(True, help="Persist stronger best-route fields and route records"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_phase3_dynamic_queue(
            session=session,
            limit=limit,
            max_gemini_calls=max_gemini_calls,
            max_links=max_links,
            max_deep_pages=max_deep_pages,
            apply=apply,
        )
    print("[green]Phase 3 dynamic queue run complete[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("scan-main-cycle")
def scan_main_cycle_cmd(
    station_limit: int = typer.Option(100, min=1, max=5000),
    max_pages: int = typer.Option(3, min=1, max=10),
    min_confidence: float = typer.Option(0.0, min=0.0, max=1.0),
    use_checkpoint: bool = typer.Option(True, help="Use persistent cursor over main-db stations"),
    reset_checkpoint: bool = typer.Option(False, help="Reset cursor back to station_id 0"),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_main_scan_cycle(
            session=session,
            station_limit=station_limit,
            max_pages=max_pages,
            min_confidence=min_confidence,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=reset_checkpoint,
        )
    print("[green]Main scan cycle complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("verify-station-quality-llm")
def verify_station_quality_llm_cmd(
    limit: int = typer.Option(50, min=1, max=5000),
    station_id: int | None = typer.Option(None, min=1),
    provider: str = typer.Option("gemini", help="gemini|openai"),
    only_unassessed: bool = typer.Option(True, help="Skip stations already assessed with current assessment kind"),
    apply: bool = typer.Option(False, help="Persist assessments to DB"),
) -> None:
    with SessionLocal() as session:
        result = run_station_quality_verification(
            session=session,
            limit=limit,
            station_id=station_id,
            provider=provider,
            only_unassessed=only_unassessed,
            apply=apply,
        )
    print("[green]Station quality LLM verification complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("list-route-discovery-candidates")
def list_route_discovery_candidates_cmd(
    limit: int = typer.Option(50, min=1, max=1000),
    min_confidence: float = typer.Option(0.6, min=0.0, max=1.0),
    min_quality_score: int = typer.Option(60, min=0, max=100),
) -> None:
    with SessionLocal() as session:
        result = route_discovery_candidates(
            session=session,
            limit=limit,
            min_confidence=min_confidence,
            min_quality_score=min_quality_score,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("run-route-discovery-batch")
def run_route_discovery_batch_cmd(
    limit: int = typer.Option(5, min=1, max=100),
    station_id: int | None = typer.Option(None, min=1),
    max_brave_calls: int = typer.Option(2, min=0, max=2),
    max_pages: int = typer.Option(8, min=1, max=20),
    apply: bool = typer.Option(False, help="Persist best route fields and route records"),
    min_confidence: float = typer.Option(0.6, min=0.0, max=1.0),
    min_quality_score: int = typer.Option(60, min=0, max=100),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_route_discovery_batch(
            session=session,
            limit=limit,
            station_id=station_id,
            max_brave_calls=max_brave_calls,
            max_pages=max_pages,
            apply=apply,
            min_confidence=min_confidence,
            min_quality_score=min_quality_score,
        )
    print("[green]Route discovery batch complete[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("seed-phase2-route-queue")
def seed_phase2_route_queue_cmd(
    core: bool = typer.Option(True, help="Seed current core route-discovery candidates"),
    enrichment: bool = typer.Option(True, help="Seed pending enrichment reassessment candidates"),
    core_limit: int = typer.Option(494, min=1, max=5000),
    enrichment_limit: int = typer.Option(474, min=1, max=5000),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = seed_phase2_route_queue(
            session,
            include_core=core,
            include_enrichment=enrichment,
            core_limit=core_limit,
            enrichment_limit=enrichment_limit,
        )
    print("[green]Phase-2 route queue seeded[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("phase2-route-queue-status")
def phase2_route_queue_status_cmd() -> None:
    init_db()
    with SessionLocal() as session:
        result = phase2_route_queue_status(session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("run-phase2-route-worker")
def run_phase2_route_worker_cmd(
    worker_id: str | None = typer.Option(None, help="Stable worker id for logs/claims"),
    limit: int = typer.Option(100, min=1, max=5000),
    max_brave_calls: int = typer.Option(2, min=0, max=2),
    max_pages: int = typer.Option(8, min=1, max=20),
    stale_after_minutes: int = typer.Option(90, min=5, max=1440),
) -> None:
    init_db()
    with SessionLocal() as session:
        result = run_phase2_route_worker(
            session,
            worker_id=worker_id,
            limit=limit,
            max_brave_calls=max_brave_calls,
            max_pages=max_pages,
            stale_after_minutes=stale_after_minutes,
        )
    print("[green]Phase-2 route worker complete[/green]")
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("enrich-station-editorial")
def enrich_station_editorial_cmd(
    limit: int = typer.Option(25, min=1, max=5000),
    station_id: int | None = typer.Option(None, min=1),
    provider: str = typer.Option("gemini", help="gemini|openai"),
    only_unassessed: bool = typer.Option(True, help="Skip stations already enriched with current assessment kind"),
    apply: bool = typer.Option(False, help="Persist editorial enrichment to DB"),
) -> None:
    with SessionLocal() as session:
        result = run_station_editorial_enrichment(
            session=session,
            limit=limit,
            station_id=station_id,
            provider=provider,
            only_unassessed=only_unassessed,
            apply=apply,
        )
    print("[green]Station editorial enrichment complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("cleanup-people-quality")
def cleanup_people_quality_cmd(
    limit: int = typer.Option(500000, min=1, max=5000000),
    apply: bool = typer.Option(False, help="If true, delete low-quality people records"),
) -> None:
    with SessionLocal() as session:
        result = cleanup_people_quality(session=session, limit=limit, dry_run=not apply)
    print("[green]People quality cleanup complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("cleanup-focus-keywords")
def cleanup_focus_keywords_cmd(
    keywords: str = typer.Option("jazz", help="Comma-separated keywords, z. B. jazz,classical"),
    limit: int = typer.Option(50000, min=1, max=500000),
    include_genres: bool = typer.Option(False, help="Also match station_genres (broader, can be noisy)"),
    apply: bool = typer.Option(False, help="If true, matching stations are set to REJECTED"),
) -> None:
    keyword_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
    with SessionLocal() as session:
        result = cleanup_stations_by_focus_keywords(
            session=session,
            keywords=keyword_list,
            limit=limit,
            dry_run=not apply,
            include_genres=include_genres,
        )
    print("[green]Focus keyword cleanup complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("cleanup-meta-stations")
def cleanup_meta_stations_cmd(
    limit: int = typer.Option(50000, min=1, max=500000),
    apply: bool = typer.Option(False, help="If true, meta/list/directory stations are set to REJECTED"),
) -> None:
    with SessionLocal() as session:
        result = cleanup_stations_by_meta_rules(session=session, limit=limit, dry_run=not apply)
    print("[green]Meta station cleanup complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("cleanup-entrypoint-stations")
def cleanup_entrypoint_stations_cmd(
    limit: int = typer.Option(50000, min=1, max=500000),
    apply: bool = typer.Option(False, help="If true, submission-entrypoint-only records are set to REJECTED"),
) -> None:
    with SessionLocal() as session:
        result = cleanup_stations_by_entrypoint_rules(session=session, limit=limit, dry_run=not apply)
    print("[green]Entrypoint station cleanup complete[/green]")
    print(json.dumps(result, indent=2))


@app.command("backfill-primary-submission-routes")
def backfill_primary_submission_routes_cmd(
    limit: int | None = typer.Option(None, min=1, help="Optional station limit"),
) -> None:
    with SessionLocal() as session:
        result = backfill_primary_submission_routes(session, limit=limit)
    print("[green]Primary submission routes backfilled[/green]")
    print(json.dumps(result, indent=2))


@app.command("dashboard")
def dashboard_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8080, min=1, max=65535, help="Bind port"),
) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()

from __future__ import annotations

import json

import typer
from rich import print

from radio_db.db import SessionLocal, init_db
from radio_db.dashboard import create_app
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
from radio_db.services.forms_agent import run_main_scan_cycle, run_rejected_scan_cycle, start_manual_scan_run

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


@app.command("dashboard")
def dashboard_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8080, min=1, max=65535, help="Bind port"),
) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()

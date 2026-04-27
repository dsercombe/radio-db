# Radio Database Agent

LLM-gesteuertes Discovery- und Enrichment-System für internationale Radiosender mit Fokus auf:
- kontinuierliche Sender-Discovery (statt immer gleicher Ergebnisse)
- strukturierte Metadaten zu Programmen und Genres
- explizite Song-Submission-Kanäle für Newcomer-Projekte

## Was umgesetzt ist

- Python-CLI (`radio-db`) für komplette Pipeline-Steuerung
- SQLAlchemy-Datenmodell für Stationen, Programme, Genres, Submission-Kanäle, Evidenz und Query-Frontier
- Seed-Ingestion aus:
  - Radio Browser API
  - Wikidata SPARQL
- Discovery via Brave Search API
- LLM-Extraktion in strukturiertes JSON (mit Heuristik-Fallback ohne LLM-Key)
- Dedupe/Fingerprinting (Name + Domain + Land + Fuzzy Matching)
- Novelty-Mechanismus:
  - Query-Frontier mit UCB-Priorisierung
  - Query-Performance-Metriken (`yield_new`, `yield_duplicate`)
  - dynamische Re-Crawl-Intervalle nach tatsächlicher Novelty

## Architektur

1. Seed Layer
- Füllt initialen Kandidatenbestand aus strukturierten Quellen.

2. Discovery Layer
- Führt Query-Templates pro Land/Genre aus, holt Web-Treffer und crawlt Zielseiten.

3. Extraction Layer
- LLM extrahiert Station, Genres, Programme, Submission-Infos als JSON.

4. Resolution Layer
- Dedupe + Upsert + Evidenzspeicherung + Statusbewertung (`candidate`/`verified`).

5. Frontier Intelligence
- Query-Ranking via UCB, um Suchbudget auf neue, ergiebige Queries zu lenken.

## Datenmodell (Kern)

- `stations`
- `station_aliases`
- `station_genres`
- `station_programs`
- `submission_channels`
- `station_contacts` (Hosts, DJs, Music Directors, Program Directors)
- `evidence`
- `crawl_frontier`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Optional:
- `BRAVE_API_KEY` setzen für Web-Discovery
- `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_CX` setzen für zusätzliche Google-Discovery/Detail-Seiten
- `TAVILY_API_KEY` setzen für zusätzliche Recherche-Resultate/Fallback
- `OPENAI_API_KEY` setzen für bessere Extraktion
- Für Browser-Form-Scanning zusätzlich: `pip install playwright && playwright install chromium`
- `DATABASE_URL` auf Postgres setzen, z. B. `postgresql+psycopg://user:pass@localhost/radio_db`
- Kosten-/Rate-Limits in `.env` anpassen:
  - `MAX_BRAVE_CALLS_PER_MONTH`
  - `MAX_GOOGLE_CSE_CALLS_PER_DAY`
  - `MAX_GOOGLE_CSE_CALLS_PER_MONTH`
  - `MAX_TAVILY_CALLS_PER_DAY`
  - `MAX_TAVILY_CALLS_PER_MONTH`
  - `MAX_DUCKDUCKGO_CALLS_PER_DAY`
  - `MAX_DUCKDUCKGO_CALLS_PER_MONTH`
  - `ENABLE_*_SEARCH`
  - `MAX_RESULTS_PER_SOURCE`
  - `GOOGLE_ENABLE_DETAIL_QUERY`
  - `GOOGLE_DETAIL_QUERY_SUFFIX`
  - `TAVILY_ENABLE_DETAIL_QUERY`
  - `TAVILY_DETAIL_QUERY_SUFFIX`
  - `MAX_DAILY_USD`
  - `MAX_LLM_CALLS_PER_RUN`
  - `MAX_LLM_CALLS_PER_DAY`
  - `MAX_LLM_CALLS_PER_DOMAIN_PER_RUN`
  - `MAX_PAGE_FETCHES_PER_RUN`
  - `MAX_CODEX_CALLS_PER_DAY`
  - `MAX_CODEX_DAILY_USD`
  - `PRIORITY_COUNTRIES`
  - `BROWSER_*`
  - `BROWSER_PROXY_URL` (z. B. `socks5://127.0.0.1:1055` für isolierten Tailscale-Proxy)

## CLI

```bash
radio-db init-db
radio-db seed-frontier
radio-db ingest-seeds --limit 500
radio-db discover --max-queries 25
radio-db run-country-discovery-cycle --max-queries 8
radio-db ingest-free --limit 2000
radio-db ingest-free-full --reset-checkpoint
radio-db enrich-priority --max-queries 10
radio-db station-enrich-search --station-limit 200 --queries-per-station 2
radio-db scan-submission-forms --station-limit 50 --mode read
radio-db run-country-form-cycle --station-limit 50 --mode fill_simulation
radio-db build-people --limit 1000
radio-db people-discovery --station-limit 200 --queries-per-station 2
radio-db people-search --q "dj" --limit 20
radio-db cleanup-people-quality
radio-db build-profiles --limit 1000
radio-db dashboard --host 0.0.0.0 --port 8080
radio-db stats
```

`ingest-free-full` holt freie Quellen paginiert mit persistentem Resume-Checkpoint:
- Checkpoint: `.radio_db_state/free_harvest_checkpoint.json`
- Fortsetzen: Befehl ohne `--reset-checkpoint`
- Neustart von vorne: mit `--reset-checkpoint`
- Begrenzung für kontrollierte Runs: `--max-rb-pages` / `--max-wikidata-pages`

Dashboard enthält:
- Datenbank-Ansicht mit Filtern (Suche, Land, Status, Genre, Submission, Confidence)
- Monitoring von Limits/Usage (`MAX_DAILY_USD`, LLM-Calls, Brave-Monatscalls)
- Überblick über `systemd`-Status (`radio-db.service` / `radio-db.timer`)
- laufende Prozesse und letzte Fehler aus `journalctl`

## Hetzner x86 Deployment

Für einen Debian/Ubuntu-basierten Hetzner x86_64 Server:

```bash
git clone <your-repo-url> /opt/radio-database
cd /opt/radio-database
./scripts/install_hetzner_x86.sh /opt/radio-database
```

Danach:

```bash
cd /opt/radio-database
nano .env
source .venv/bin/activate
radio-db ingest-free --limit 5000
radio-db enrich-priority --max-queries 10
```

Optionaler `systemd`-Betrieb (alle 6 Stunden):

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin radio || true
sudo chown -R radio:radio /opt/radio-database
sudo cp deploy/systemd/radio-db.service /etc/systemd/system/
sudo cp deploy/systemd/radio-db.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radio-db.timer
sudo systemctl list-timers | grep radio-db
```

## Cost Guard

Discovery ist absichtlich durch harte Schutzlimits abgesichert:
- Brave API Monatslimit über `MAX_BRAVE_CALLS_PER_MONTH`
- Tagesbudget in USD (Schätzung) über `MAX_DAILY_USD`
- LLM-Call-Limits pro Run/Tag/Domain
- Begrenzte Seitenabrufe pro Run
- LLM nur bei relevanten Treffern (Submission-/Radio-Marker)

Der Tageszähler wird in `.radio_db_state/cost_guard.json` persistiert und täglich (UTC) zurückgesetzt.
API-Nutzung (`brave_calls`, `google_cse_calls_*`, `tavily_calls_*`, `duckduckgo_calls_*`) liegt in `.radio_db_state/api_usage.json`.

## Vibe Profiles

`radio-db build-profiles` erstellt Snapshot-Profile pro Sender aus mehreren Facts/Evidence:
- `style_tags`
- `editorial_signals`
- `show_personality`
- `host_voice`
- `source_count` + `diversity_score`

## People Index

`radio-db build-people` konsolidiert gefundene Personen pro Sender:
- Rolle (`host`, `dj`, `music_director`, ...)
- Kontakt (`email`, `contact_url`)
- optionale Präferenz-Tags (`musical_preferences`, `genre_affinities`)
- `source_count` + `confidence`

`radio-db people-discovery` sucht zusätzlich explizit nach Personen über Suchquellen
(inkl. LinkedIn-URL-Hinweisen), ordnet Rollen ein und speichert Evidence.

`radio-db station-enrich-search` führt domain-scoped Queries pro existierendem Sender aus
(z. B. `site:station-domain contact/submit/shows/team`) und aktualisiert Facts gezielt.

## Discovery gegen Wiederholungen

Mechanismen im aktuellen Stand:
- persistente Query-Frontier statt statischer Suche
- Query-Scoring via UCB (`priority_score`)
- `yield_new / run_count` als Exploitation-Signal
- Exploration-Bonus für wenig gelaufene Queries
- adaptive `next_run_at` (mehr Novelty => schnellerer Re-Run)

Erweiterung (nächster Schritt):
- zusätzliche Query-Dimensionen (Sprache, Region, Submission-Synonyme)
- Result-URL-History mit TTL
- Bandit auf Query-Template-Ebene + Query-Expansion via LLM

## Submission-Form-Agent (neu)

Neue Tabellen:
- `forms`
- `form_fields`
- `form_recipes`

Neue Commands:
- `radio-db scan-submission-forms` (scannt Formulare und speichert Feldstruktur + Recipe)
- `radio-db run-country-form-cycle` (arbeitet `PRIORITY_COUNTRIES` zyklisch ab)
- `radio-db run-country-discovery-cycle` (arbeitet `PRIORITY_COUNTRIES` zyklisch ab und fokussiert neue Sender)

Sicherheitsmodus:
- Default ist Dry-Run (`mode=read` oder `mode=fill_simulation`)
- Keine echte finale Submission im Scan-Workflow

## Kontinuierlicher Länder-Discovery-Agent (neu)

Ziel:
- kontinuierlich Länderliste durchlaufen
- pro Lauf nur ein Land, danach Cursor auf nächstes Land
- Fokus auf neue Sender (bereits bekannte Domains werden übersprungen)

Steuerung über `.env`:
- `PRIORITY_COUNTRIES`
- `COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN`
- `COUNTRY_DISCOVERY_MIN_CONFIDENCE`
- `COUNTRY_DISCOVERY_INCLUDE_LINKUP`
- `COUNTRY_DISCOVERY_MAX_RESULTS_PER_QUERY`

## Hinweise zu APIs

- Google Places ist für Radiosender in diesem Setup nicht die beste Primärquelle.
- Brave + Radio Browser + Wikidata sind komplementär und decken Discovery + Struktur ab.
- Für höhere Qualität empfiehlt sich zusätzlich ein gezielter Scraper für station-spezifische Seiten wie:
  - `about`, `programs`, `shows`, `playlist`, `submit`, `contact`

## Qualität und Governance

Empfohlene Produktionsregeln:
- Aufnahme in `verified` nur bei ausreichender Confidence und Submission-Hinweis
- harte Limits je Domain/Tag (Rate-Limit)
- Auditierbarkeit über `evidence`-Tabelle
- Recrawl-Strategie abhängig von Senderwert und Änderungsfrequenz

## Nächste sinnvolle Ausbaustufen

1. Async Worker (Celery/RQ/Arq) für skalierte Crawls
2. Alembic-Migrationen + DB-Indizes für Postgres
3. HTML-zu-Text + Link-Priorisierung pro Station
4. Bewertungsmodell für Submission-Qualität (Form vs Mail vs Portal)
5. Export/API-Layer für CRM/Outreach-Systeme

# Radio DB Full-Scale Migration

## Current State

- GitHub migration branch: `codex/radio-db-full-scale-migration`
- Current production backend: `/opt/radio-database` on `meinserver`
- Current production database: local Postgres `radio_db_prod`
- Current public URL: `https://radio.public-air.net`

## Completed

- Captured the current production code in Git.
- Added Git ignores for runtime state, queue DBs, logs, backups, and migration artifacts.
- Prepared split frontend deployment:
  - `frontend/src/radioDbApi.ts` supports `VITE_API_ROOT`.
  - backend supports `RADIO_DB_CORS_ALLOWED_ORIGINS`.
- Verified tests and frontend build.
- Created migration exports on the server, excluded from Git:
  - `migration_artifacts/radio_db_schema.sql`
  - `migration_artifacts/radio_db_data.dump`

## Supabase Target

Target project created:

- Name: `Radio DB`
- Project ref: `alcaojhizqiifascmvmd`
- Region: `eu-central-1`
- API URL: `https://alcaojhizqiifascmvmd.supabase.co`
- Initial state: active, schema and data restored

Import completed from `migration_artifacts/radio_db_schema_supabase.sql` and `migration_artifacts/radio_db_data.dump` using direct Postgres connection. Do not commit database passwords or dump files.

## Vercel Target

Preferred target: a new Vercel project for the Radio DB frontend, with:

- Root directory: `frontend`
- Build command: `npm run build`
- Output directory: `dist`
- Env: `VITE_API_ROOT=https://radio.public-air.net/api/v1` initially

Longer term, this frontend can become part of `public-air.net` while the worker/backend remains separate.

## Cutover Plan

1. Supabase `Radio DB` project created.
2. Imported `radio_db_schema_supabase.sql`.
3. Restored `radio_db_data.dump` with `pg_restore`.
4. DB smoke checks passed:
   - `stations`: 10,795
   - `submission_channels`: 29,849
   - `evidence`: 167,216
   - `forms`: 3,222
   - `verified stations`: 298
   - `pitch_ready_stations`: 64
   - `radio-db stats` succeeds against Supabase.
5. Point `meinserver` worker/backend `DATABASE_URL` to Supabase.
6. Deploy Vercel frontend with `VITE_API_ROOT` pointing to the backend.
7. Add Auth gate for dashboard access.
8. Move worker state/logs to `/var/lib/radio-db` and `/var/log/radio-db`.

# High Priority Manual Curation

This folder is for manual country-by-country curation of top stations.

## Files
- `countries.json`: prioritized country backlog for manual work.
- `stations/XX.json`: one file per country (`XX` = ISO-2 code).

## Workflow
1. Pick next country from `countries.json` with `manual_status = "todo"`.
2. Fill `stations/XX.json` with high-value stations.
3. Set country status to `in_progress` / `done`.
4. Merge curated entries into `config/high_priority_stations.json`.

## Station template
Each station entry should follow the existing high-priority schema:
- `key`
- `name`
- `country_code`
- `website`
- `must_visit_paths`
- `enabled`
- optional: `priority_score`, `priority_tier`, `newcomer_signal`, `verification_sources`

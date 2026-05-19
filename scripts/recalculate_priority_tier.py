#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path

DEFAULT_DB='/opt/radio-database/radio.db'
DEFAULT_COUNTRIES='/opt/radio-database/config/high_priority_manual/countries.json'

def ensure_priority_column(conn):
    cols={r[1] for r in conn.execute('PRAGMA table_info(stations)').fetchall()}
    if 'priority_tier' not in cols:
        conn.execute('ALTER TABLE stations ADD COLUMN priority_tier INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE INDEX IF NOT EXISTS ix_stations_priority_tier ON stations(priority_tier)')
        conn.commit()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db-path', default=DEFAULT_DB)
    ap.add_argument('--countries-json', default=DEFAULT_COUNTRIES)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true')
    args=ap.parse_args()

    countries=json.loads(Path(args.countries_json).read_text(encoding='utf-8'))
    tier_by_code={str(c.get('code','')).upper(): int(c.get('priority_tier',0) or 0) for c in countries if c.get('code')}

    conn=sqlite3.connect(args.db_path)
    ensure_priority_column(conn)
    cur=conn.cursor()

    if args.force:
        cur.execute('UPDATE stations SET priority_tier = 0')

    for country_code, tier in tier_by_code.items():
        cur.execute('UPDATE stations SET priority_tier = ? WHERE UPPER(country_code)=?', (tier, country_code))

    # non-priority stations go back to cleanup queue
    cur.execute("UPDATE stations SET status='CANDIDATE', updated_at=CURRENT_TIMESTAMP WHERE COALESCE(priority_tier,0)=0 AND status!='REJECTED'")

    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()

    total=cur.execute('SELECT COUNT(*) FROM stations').fetchone()[0]
    high=cur.execute('SELECT COUNT(*) FROM stations WHERE COALESCE(priority_tier,0)>0').fetchone()[0]
    print(json.dumps({'total_stations':total,'high_priority_stations':high,'mapped_countries':len(tier_by_code),'dry_run':args.dry_run}, indent=2))

if __name__=='__main__':
    main()

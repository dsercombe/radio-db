#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "/opt/radio-database/radio.db"
DEFAULT_STATE = "/opt/radio-database/.radio_db_state/quality_cleanup_state.json"
DEFAULT_LOG = "/opt/radio-database/.radio_db_state/quality_cleanup_log.jsonl"

@dataclass
class StationRow:
    id:int; status:str; confidence_score:float; website_url:str|None; priority_tier:int; submission_count:int; evidence_count:int; people_count:int

def ensure_priority_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stations)").fetchall()}
    if "priority_tier" not in cols:
        conn.execute("ALTER TABLE stations ADD COLUMN priority_tier INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_stations_priority_tier ON stations(priority_tier)")
        conn.commit()

def decide_status(row: StationRow, min_conf: float, keep_priority: bool):
    st=(row.status or "").upper(); sub=row.submission_count>0; ev=row.evidence_count>0; ppl=row.people_count>0; web=bool((row.website_url or "").strip()); pri=row.priority_tier>0
    if keep_priority and pri and st=="VERIFIED" and row.confidence_score>=max(min_conf,0.55): return "VERIFIED","preserve_priority_verified"
    if st=="VERIFIED" and row.confidence_score>=max(min_conf,0.75) and (sub or row.evidence_count>=2): return "VERIFIED","preserve_strong_verified"
    noisy_candidate = st=="CANDIDATE" and row.confidence_score<min_conf and not sub and row.evidence_count<=1
    low_signal = row.confidence_score<min_conf*0.7 or (not sub and not ev and not ppl and row.confidence_score<0.5) or (not web and row.confidence_score<0.45)
    if noisy_candidate or low_signal: return "REJECTED","noisy_or_low_signal"
    if row.confidence_score>=min_conf and (sub or ev or ppl):
        if row.confidence_score>=0.8 and (sub or row.evidence_count>=2): return "VERIFIED","high_confidence_signal"
        return "CANDIDATE","needs_review"
    return "CANDIDATE","default_candidate"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--log-file", default=DEFAULT_LOG)
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--keep-priority", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args=ap.parse_args()

    state_path=Path(args.state_file); log_path=Path(args.log_file)
    state_path.parent.mkdir(parents=True, exist_ok=True); log_path.parent.mkdir(parents=True, exist_ok=True)
    state={}
    if state_path.exists() and not args.force:
        try: state=json.loads(state_path.read_text(encoding="utf-8"))
        except Exception: state={}

    last_id = int(state.get("last_station_id",0)) if not args.force else 0
    started_at=datetime.now(timezone.utc).isoformat()
    conn=sqlite3.connect(args.db_path); conn.row_factory=sqlite3.Row; conn.execute("PRAGMA journal_mode=WAL"); ensure_priority_column(conn)
    scanned=0; reasons=Counter(); changes=Counter()

    while True:
        rows=conn.execute("""
            SELECT s.id,s.status,COALESCE(s.confidence_score,0) confidence_score,s.website_url,COALESCE(s.priority_tier,0) priority_tier,
                   COALESCE(sc.cnt,0) submission_count,COALESCE(ev.cnt,0) evidence_count,COALESCE(sp.cnt,0) people_count
              FROM stations s
              LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM submission_channels GROUP BY station_id) sc ON sc.station_id=s.id
              LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM evidence GROUP BY station_id) ev ON ev.station_id=s.id
              LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM station_people GROUP BY station_id) sp ON sp.station_id=s.id
             WHERE s.id>? ORDER BY s.id ASC LIMIT ?
        """, (last_id,args.batch_size)).fetchall()
        if not rows: break
        updates=[]
        for r in rows:
            row=StationRow(int(r["id"]),str(r["status"] or "CANDIDATE"),float(r["confidence_score"] or 0),r["website_url"],int(r["priority_tier"] or 0),int(r["submission_count"] or 0),int(r["evidence_count"] or 0),int(r["people_count"] or 0))
            new,reason=decide_status(row,args.min_confidence,args.keep_priority)
            old=row.status.upper(); scanned+=1; reasons[reason]+=1
            if new!=old: updates.append((new,row.id)); changes[f"{old}->{new}"]+=1
            last_id=row.id
        if updates and not args.dry_run:
            conn.executemany("UPDATE stations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", updates); conn.commit()
        ckpt={"updated_at":datetime.now(timezone.utc).isoformat(),"started_at":started_at,"last_station_id":last_id,"scanned":scanned,"dry_run":args.dry_run,"min_confidence":args.min_confidence,"keep_priority":args.keep_priority,"reason_counts":dict(reasons),"status_changes":dict(changes),"completed":False}
        state_path.write_text(json.dumps(ckpt,indent=2),encoding="utf-8")

    final={"updated_at":datetime.now(timezone.utc).isoformat(),"started_at":started_at,"last_station_id":last_id,"scanned":scanned,"dry_run":args.dry_run,"min_confidence":args.min_confidence,"keep_priority":args.keep_priority,"reason_counts":dict(reasons),"status_changes":dict(changes),"completed":True}
    state_path.write_text(json.dumps(final,indent=2),encoding="utf-8")
    with log_path.open("a",encoding="utf-8") as f: f.write(json.dumps(final,ensure_ascii=False)+"\n")
    print(json.dumps(final,indent=2))

if __name__=="__main__":
    main()

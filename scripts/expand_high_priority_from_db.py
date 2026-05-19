#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, sqlite3
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime

DEFAULT_DB = "/opt/radio-database/radio.db"
DEFAULT_COUNTRIES = "/opt/radio-database/config/high_priority_manual/countries.json"
DEFAULT_ACTIVE = "/opt/radio-database/config/high_priority_stations.json"
DEFAULT_MANUAL_DIR = "/opt/radio-database/config/high_priority_manual/stations"

NON_STATION_DOMAIN_HINTS = (
    "one-submit.com",
    "musicgateway.com",
    "groover.co",
    "blog.groover.co",
    "onlineradiobox.com",
    "radio.net",
    "mytuner-radio.com",
    "internet-radio.com",
    "radio.fr",
    "radio-espana.es",
    "radio-en-ligne.fr",
    "apps.apple.com",
    "play.google.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "reddit.com",
    "spotify.com",
    "soundcloud.com",
    "mixcloud.com",
    "tunein.com",
)


def dom(url: str | None) -> str:
    if not url:
        return ""
    d = (urlparse(url).netloc or "").lower()
    return d[4:] if d.startswith("www.") else d


def key_from_domain(cc: str, domain: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", domain.strip().lower()).strip("_")
    return f"{cc.lower()}_{safe}" if safe else ""


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def station_candidates(conn: sqlite3.Connection, country: str, min_conf: float):
    q = """
        SELECT s.id, s.canonical_name, s.website_url, s.confidence_score, s.status,
               COALESCE(sc.cnt,0) submission_count,
               COALESCE(sp.cnt,0) people_count,
               COALESCE(pr.cnt,0) program_count,
               COALESCE(ev.cnt,0) evidence_count
          FROM stations s
          LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM submission_channels GROUP BY station_id) sc ON sc.station_id=s.id
          LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM station_people GROUP BY station_id) sp ON sp.station_id=s.id
          LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM station_programs GROUP BY station_id) pr ON pr.station_id=s.id
          LEFT JOIN (SELECT station_id,COUNT(*) cnt FROM evidence GROUP BY station_id) ev ON ev.station_id=s.id
         WHERE UPPER(s.country_code)=?
           AND UPPER(s.status)!='REJECTED'
           AND s.website_url IS NOT NULL
           AND length(trim(s.website_url))>0
           AND s.confidence_score>=?
         ORDER BY (CASE WHEN UPPER(s.status)='VERIFIED' THEN 1 ELSE 0 END) DESC,
                  submission_count DESC,
                  people_count DESC,
                  program_count DESC,
                  evidence_count DESC,
                  s.confidence_score DESC,
                  s.id ASC
    """
    return conn.execute(q, (country, min_conf)).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--countries", default=DEFAULT_COUNTRIES)
    ap.add_argument("--active", default=DEFAULT_ACTIVE)
    ap.add_argument("--manual-dir", default=DEFAULT_MANUAL_DIR)
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    countries_path = Path(args.countries)
    active_path = Path(args.active)
    manual_dir = Path(args.manual_dir)
    manual_dir.mkdir(parents=True, exist_ok=True)

    countries = load_json(countries_path, [])
    active = load_json(active_path, [])
    active_by_key = {r.get("key"): r for r in active if r.get("key")}
    active_domains = {dom(r.get("website")) for r in active if r.get("website")}

    conn = sqlite3.connect(args.db)
    now = datetime.utcnow().isoformat()
    summary = []

    for c in countries:
        code = str(c.get("code") or "").upper().strip()
        if not code:
            continue
        target = int(c.get("target_major_stations") or args.target)
        manual_path = manual_dir / f"{code}.json"
        manual = load_json(manual_path, {"country_code": code, "manual_status": "todo", "stations": []})
        manual_stations = manual.get("stations", [])
        manual_keys = {s.get("key") for s in manual_stations if s.get("key")}
        manual_domains = {dom(s.get("website")) for s in manual_stations if s.get("website")}

        total_now = len(manual_stations)
        needed = max(0, target - total_now)
        added = 0
        if needed > 0:
            rows = station_candidates(conn, code, args.min_confidence)
            for r in rows:
                _sid, name, website, conf, status, sub_cnt, people_cnt, program_cnt, ev_cnt = r
                d = dom(website)
                if not d:
                    continue
                if any(h in d for h in NON_STATION_DOMAIN_HINTS):
                    continue
                if d in manual_domains or d in active_domains:
                    continue
                key = key_from_domain(code, d)
                if not key or key in manual_keys:
                    continue
                score = max(60, min(95, int(round((conf or 0) * 100))))
                tier = "tier_1" if score >= 85 else ("tier_2" if score >= 75 else "tier_3")
                manual_stations.append({
                    "key": key,
                    "name": str(name).strip(),
                    "country_code": code,
                    "website": str(website).strip(),
                    "must_visit_paths": ["/shows", "/playlist", "/contact"],
                    "enabled": True,
                    "priority_tier": tier,
                    "priority_score": score,
                    "newcomer_signal": bool(sub_cnt or people_cnt or program_cnt),
                    "station_class": "auto_db_candidate",
                    "submission_hint_urls": [str(website).strip()],
                    "notes": f"Auto-filled from DB (status={status}, sub={sub_cnt}, people={people_cnt}, programs={program_cnt}, evidence={ev_cnt}).",
                })
                manual_keys.add(key)
                manual_domains.add(d)
                active_domains.add(d)
                added += 1
                if added >= needed:
                    break

        manual["stations"] = manual_stations
        manual["manual_status"] = "in_progress" if len(manual_stations) > 0 else manual.get("manual_status", "todo")

        if not args.dry_run:
            save_json(manual_path, manual)

        summary.append({"country": code, "target": target, "before": total_now, "added": added, "after": len(manual_stations)})

    merged_added = 0
    merged_updated = 0
    if not args.dry_run:
        # Merge all manual catalogs into the active catalog.
        for manual_file in sorted(manual_dir.glob("*.json")):
            catalog = load_json(manual_file, {})
            stations = catalog.get("stations", [])
            if not isinstance(stations, list):
                continue
            for s in stations:
                if not isinstance(s, dict) or not s.get("enabled", True):
                    continue
                key = s.get("key")
                website = s.get("website") or ""
                d = dom(website)
                if not key or not website:
                    continue
                if key in active_by_key:
                    row = active_by_key[key]
                    for f in [
                        "name",
                        "country_code",
                        "website",
                        "must_visit_paths",
                        "enabled",
                        "priority_score",
                        "priority_tier",
                        "newcomer_signal",
                    ]:
                        row[f] = s.get(f, row.get(f))
                    row["verification_sources"] = sorted(
                        set((row.get("verification_sources") or []) + ["manual_country_curation"])
                    )
                    row["last_verified_at"] = now
                    row.setdefault("added_at", now)
                    merged_updated += 1
                    continue
                if d and d in active_domains:
                    continue
                row = {
                    "key": key,
                    "name": s.get("name"),
                    "country_code": s.get("country_code"),
                    "website": website,
                    "must_visit_paths": s.get("must_visit_paths", ["/shows", "/contact"]),
                    "enabled": bool(s.get("enabled", True)),
                    "priority_score": int(s.get("priority_score", 75)),
                    "priority_tier": s.get("priority_tier", "tier_2"),
                    "newcomer_signal": bool(s.get("newcomer_signal", True)),
                    "verification_sources": ["manual_country_curation"],
                    "added_at": now,
                    "last_verified_at": now,
                }
                active.append(row)
                active_by_key[key] = row
                if d:
                    active_domains.add(d)
                merged_added += 1

        active.sort(
            key=lambda r: ((r.get("country_code") or ""), (r.get("name") or "").lower(), (r.get("key") or ""))
        )
        save_json(active_path, active)

        from collections import Counter
        counts = Counter((r.get("country_code") or "").upper() for r in active if r.get("country_code"))
        for row in countries:
            cc = str(row.get("code") or "").upper()
            if not cc:
                continue
            row["existing_high_priority_count"] = int(counts.get(cc, 0))
            if row["existing_high_priority_count"] > 0:
                row["manual_status"] = row.get("manual_status") or "in_progress"

        save_json(countries_path, countries)

    print(
        json.dumps(
            {
                "countries": summary,
                "dry_run": args.dry_run,
                "merged_added": merged_added,
                "merged_updated": merged_updated,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

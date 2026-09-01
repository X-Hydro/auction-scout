"""
Exports current property/auction state from SQLite into properties.json --
the single static file the frontend map loads. Run this as the last step
of the weekly automation, after load_csv.py has ingested the week's spider output.

Usage:
    python export_json.py auctionscout.db scout-properties.json

Design notes:
- One flat array, one object per property. No pagination -- client does all filtering.
- Statuses are treated as "live" (exported) by default. Only statuses explicitly
  listed in EXCLUDED_STATUSES are dropped from the map -- this is safer than a
  whitelist given real scraped data has varied status wording across six sources
  (e.g. "sold back to mortgagee" and "3rd party purchase" both mean the auction
  already concluded with a sale). Excluded properties remain in SQLite for history.
- All live auctions are exported with status="scheduled" regardless of the raw
  internal status (active/on_time/postponed/etc.). From a user perspective, if
  it's on the map it's happening. Postponement history is surfaced via two
  separate fields instead:
    - times_postponed: count of date_change events for that auction (0 if never moved)
    - original_date:   ISO8601 date from the first date_change event (null if never moved)
- auction_datetime stays ISO8601 so the frontend can do its own date-bucket logic
  (This Week / This Month / All) relative to *today*, rather than trusting a
  stale precomputed bucket from scrape time.
- The output JSON's top level includes accounting fields (total_in_db,
  excluded_total, excluded_by_status, excluded_missing_coords) so it's
  possible to see why the exported count differs from what load_csv.py
  reported, without running a separate diagnostic script.
- SEASONING_RULES (below) is a per-source gate applied on top of the
  exclusions above. NOT about waiting for data to get richer -- some
  sources (brockscott) never get richer no matter how long we wait, they
  have no photos/terms/detail page at any point, full stop. It's about
  (a) confidence: has a listing survived a second, independent scrape
  confirmation, filtering out one-off parsing glitches or listings pulled
  almost immediately; and (b) relevance: not cluttering the map with a
  thin, unphotographed, no-terms listing that's months out and not yet
  actionable for anyone. See SEASONING_RULES's own comment for the
  reconfirmation mechanism and a known trade-off.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from datetime import date
import shutil

from statuses import EXCLUDED_STATUSES


def _normalize_path(path: str) -> str:
    """Accepts Windows paths ("C:/..." or "C:\\...") as-is, and converts
    Git Bash / MSYS-style paths ("/c/dev/...") into native Windows paths
    ("C:/dev/...") on Windows so sqlite3/open() can resolve them correctly.
    No-op on non-Windows platforms."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])/(.*)$", path)
        if m:
            drive, rest = m.groups()
            path = f"{drive.upper()}:/{rest}"
    return path



# EXCLUDED_STATUSES now lives in statuses.py (shared with load_csv.py) --
# see that module's docstring. Everything NOT in that list is treated as
# still-live and gets exported.

# If a property hasn't been re-confirmed by a scrape within this many days,
# treat it as no longer live regardless of its last recorded status -- a
# source may have quietly removed a listing without our ever seeing an
# explicit status change (see load_csv.py's "disappeared" event detection).
# Set generously above the expected weekly run cadence to tolerate a missed
# run or two without prematurely hiding still-valid listings.
STALE_AFTER_DAYS = 14

# Per-source seasoning gates, applied ON TOP of the SQL-level exclusions
# above (status/coords/staleness/dedup already happened by the time a row
# reaches this check). Extend this dict to add the same treatment to any
# other low-information source later -- nothing else in this file needs
# to change.
#
#   max_days_out:        exclude if auction_datetime is further out than
#                         this many days from export time.
#   require_reconfirmed:  exclude unless this listing has been seen in
#                         2+ SEPARATE scrape runs.
#
# require_reconfirmed is derived from properties.first_seen_at /
# last_seen_at (already tracked by load_csv.py on every ingest) rather
# than a new counter column: both get set to the SAME run timestamp on
# first discovery, so last_seen_at can only be later than first_seen_at
# if a separate, later run re-confirmed the listing. last_seen_at >
# first_seen_at is therefore exactly "seen in 2+ distinct runs" already,
# no schema change needed.
#
# KNOWN TRADE-OFF: a listing first discovered already inside max_days_out
# (e.g. found 5 days before its own sale) may never get a second scrape
# to confirm it before the sale happens (twice-weekly cadence) -- under
# require_reconfirmed=True, that listing simply never surfaces. This is
# deliberate: favors confidence over completeness for these
# lower-information sources. A rare late-discovered straggler going
# unseen is an accepted cost, not an oversight.
SEASONING_RULES = {
    "brockscott": {"max_days_out": 14, "require_reconfirmed": True},
}


def _passes_seasoning(source, auction_date_str, first_seen_at, last_seen_at, seasoning_cutoffs):
    """True if this row has no seasoning rule configured for its source
    (the default -- always exportable), or satisfies whatever rule IS
    configured. seasoning_cutoffs is {source: iso_cutoff_string},
    precomputed once per export() call -- see there. Fails closed: a
    missing/unparseable auction_date on a source WITH a max_days_out rule
    does not pass, so a bad date can't accidentally bypass the gate."""
    rule = SEASONING_RULES.get(source)
    if not rule:
        return True

    if source in seasoning_cutoffs:
        if not auction_date_str or auction_date_str > seasoning_cutoffs[source]:
            return False

    if rule.get("require_reconfirmed"):
        if not first_seen_at or not last_seen_at or last_seen_at <= first_seen_at:
            return False

    return True


def export(db_path: str, json_path: str):
    db_path = _normalize_path(db_path)
    json_path = _normalize_path(json_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    has_dedup_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='property_duplicate_links'"
    ).fetchone() is not None
    dedup_exclusion = (
        "AND p.property_id NOT IN (SELECT property_id FROM property_duplicate_links)"
        if has_dedup_table else ""
    )
    if not has_dedup_table:
        print("Note: property_duplicate_links table not found -- skipping duplicate "
              "exclusion (run dedup_properties.py after updating schema.sql to enable it).")

    # ISO8601 timestamps compare correctly as plain strings, so this avoids
    # relying on SQLite's date-function parsing of the exact format we write.
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()

    # Same string-comparison approach as stale_cutoff above, one cutoff per
    # source that has a max_days_out seasoning rule.
    _now = datetime.now(timezone.utc)
    seasoning_cutoffs = {
        src: (_now + timedelta(days=rule["max_days_out"])).isoformat()
        for src, rule in SEASONING_RULES.items()
        if "max_days_out" in rule
    }

    placeholders = ",".join("?" for _ in EXCLUDED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT
            p.property_id,
            p.source,
            p.address_raw       AS address,
            p.latitude,
            p.longitude,
            p.state,
            p.county,
            p.municipality,
            p.first_seen_at,
            p.last_seen_at,
            a.auction_datetime  AS auction_date,
            'scheduled'         AS status,
            a.property_type,
            a.bedrooms,
            a.bathrooms,
            a.sqft,
            a.lot_size_raw      AS lot_size,
            a.year_built,
            a.source_url        AS url,
            (SELECT COUNT(*)
             FROM auction_events ae
             WHERE ae.auction_id = a.auction_id
               AND ae.event_type = 'date_change')  AS times_postponed,
            (SELECT ae.old_value
             FROM auction_events ae
             WHERE ae.auction_id = a.auction_id
               AND ae.event_type = 'date_change'
             ORDER BY ae.event_id ASC
             LIMIT 1)                               AS original_date
        FROM properties p
        JOIN auctions a ON a.property_id = p.property_id
        WHERE a.status NOT IN ({placeholders})
          AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
          AND p.last_seen_at >= ?
          AND NOT EXISTS (
              SELECT 1 FROM auction_events de
              WHERE de.auction_id = a.auction_id
                AND de.event_type = 'disappeared'
                AND de.event_id = (
                    SELECT MAX(event_id) FROM auction_events WHERE auction_id = a.auction_id
                )
          )
          {dedup_exclusion}
        ORDER BY a.auction_datetime ASC
        """,
        EXCLUDED_STATUSES + (stale_cutoff,),
        ).fetchall()

    properties = []
    excluded_seasoning = []
    for r in rows:
        row = dict(r)
        first_seen_at = row.pop("first_seen_at")
        last_seen_at = row.pop("last_seen_at")

        if not _passes_seasoning(row["source"], row["auction_date"], first_seen_at,
                                  last_seen_at, seasoning_cutoffs):
            excluded_seasoning.append({
                "address": row["address"], "source": row["source"],
                "auction_date": row["auction_date"],
                "first_seen_at": first_seen_at, "last_seen_at": last_seen_at,
            })
            continue

        # attach pdf links per property
        links = conn.execute(
            """SELECT l.url FROM auction_pdf_links l
                                     JOIN auctions a ON a.auction_id = l.auction_id
               WHERE a.property_id = ?""",
            (row["property_id"],),
        ).fetchall()
        row["pdf_links"] = [l["url"] for l in links]
        properties.append(row)

    # --- Exclusion accounting: why does exported count differ from total in DB? ---
    total_in_db = conn.execute("SELECT COUNT(*) FROM auctions").fetchone()[0]

    status_breakdown = {
        row["status"]: row["cnt"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM auctions GROUP BY status"
        )
    }
    excluded_by_status = {
        status: cnt for status, cnt in status_breakdown.items()
        if status in EXCLUDED_STATUSES
    }

    missing_coords_rows = conn.execute(
        f"""SELECT p.address_raw, p.source, a.status FROM auctions a
            JOIN properties p ON p.property_id = a.property_id
            WHERE a.status NOT IN ({placeholders})
              AND (p.latitude IS NULL OR p.longitude IS NULL)""",
        EXCLUDED_STATUSES,
    ).fetchall()
    excluded_missing_coords = [
        {"address": r["address_raw"], "source": r["source"], "status": r["status"]}
        for r in missing_coords_rows
    ]

    excluded_duplicates_count = conn.execute(
        "SELECT COUNT(*) FROM property_duplicate_links"
    ).fetchone()[0] if has_dedup_table else 0

    disappeared_rows = conn.execute(
        f"""SELECT p.address_raw, p.source, a.status FROM auctions a
            JOIN properties p ON p.property_id = a.property_id
            WHERE a.status NOT IN ({placeholders})
              AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM auction_events de
                  WHERE de.auction_id = a.auction_id
                    AND de.event_type = 'disappeared'
                    AND de.event_id = (
                        SELECT MAX(event_id) FROM auction_events WHERE auction_id = a.auction_id
                    )
              )""",
        EXCLUDED_STATUSES,
    ).fetchall()
    excluded_disappeared = [
        {"address": r["address_raw"], "source": r["source"], "status": r["status"]}
        for r in disappeared_rows
    ]

    stale_rows = conn.execute(
        f"""SELECT p.address_raw, p.source, a.status, p.last_seen_at FROM auctions a
            JOIN properties p ON p.property_id = a.property_id
            WHERE a.status NOT IN ({placeholders}) AND p.last_seen_at < ?""",
        EXCLUDED_STATUSES + (stale_cutoff,),
        ).fetchall()
    excluded_stale = [
        {"address": r["address_raw"], "source": r["source"], "status": r["status"],
         "last_seen_at": r["last_seen_at"]}
        for r in stale_rows
    ]

    meta = {
        "generated_at": conn.execute("SELECT datetime('now')").fetchone()[0],
        "count": len(properties),
        "total_in_db": total_in_db,
        "excluded_total": total_in_db - len(properties),
        "excluded_by_status": excluded_by_status,
        "excluded_missing_coords_count": len(excluded_missing_coords),
        "excluded_missing_coords": excluded_missing_coords[:25],  # capped sample
        "excluded_duplicates_count": excluded_duplicates_count,
        "excluded_disappeared_count": len(excluded_disappeared),
        "excluded_disappeared": excluded_disappeared[:25],  # capped sample
        "excluded_stale_count": len(excluded_stale),
        "excluded_stale": excluded_stale[:25],  # capped sample
        "excluded_seasoning_count": len(excluded_seasoning),
        "excluded_seasoning": excluded_seasoning[:25],  # capped sample
    }

    with open(json_path, "w") as f:
        json.dump(
            {**meta, "properties": properties},
            f,
            indent=2,  # keep file size down; not meant to be hand-read
        )

    print("Exported {} of {} auctions to {}".format(len(properties), total_in_db, json_path))
    if excluded_by_status:
        print("Excluded by status:")
        for status, cnt in sorted(excluded_by_status.items(), key=lambda x: -x[1]):
            print("  {!r}: {}".format(status, cnt))
    if excluded_missing_coords:
        print("Excluded for missing coordinates (live status, no lat/lon): {}".format(
            len(excluded_missing_coords)))
        for r in excluded_missing_coords[:10]:
            print("  [{}] {!r} (status={!r})".format(r["source"], r["address"], r["status"]))
        if len(excluded_missing_coords) > 10:
            print("  ... and {} more (see JSON meta for up to 25)".format(
                len(excluded_missing_coords) - 10))
    if excluded_duplicates_count:
        print("Excluded as cross-source duplicates: {} "
              "(run dedup_properties.py to recompute)".format(excluded_duplicates_count))
    if excluded_disappeared:
        print("Excluded as disappeared (confirmed gone from source, status not yet "
              "updated to reflect it): {}".format(len(excluded_disappeared)))
        for r in excluded_disappeared[:10]:
            print("  [{}] {!r} (status={!r})".format(r["source"], r["address"], r["status"]))
    if excluded_stale:
        print("Excluded as stale (not re-confirmed in {}+ days despite live status): {}".format(
            STALE_AFTER_DAYS, len(excluded_stale)))
        for r in excluded_stale[:10]:
            print("  [{}] {!r} (last seen {})".format(r["source"], r["address"], r["last_seen_at"]))
    if excluded_seasoning:
        print("Excluded by seasoning rule (too far out and/or not yet reconfirmed "
              "by a second scrape): {}".format(len(excluded_seasoning)))
        for r in excluded_seasoning[:10]:
            print("  [{}] {!r} (auction {}, first seen {}, last seen {})".format(
                r["source"], r["address"], r["auction_date"],
                r["first_seen_at"], r["last_seen_at"]))

    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Export current property/auction state from SQLite into a "
                     "properties.json file for the frontend map.",
        epilog="Examples:\n"
               "  python export_json.py\n"
               "  python export_json.py --db auctionscout.db --json scout-properties.json\n"
               "  python export_json.py --db /c/dev/auction-scout/auction-scout-data/auctionscout.db "
               "--json /c/dev/auction-scout/scout-properties.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default="auctionscout.db",
        help="SQLite database file to read from (default: auctionscout.db). "
             "Accepts Windows (C:/...) or Git Bash (/c/...) style paths."
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="scout-properties.json",
        help="Output JSON file for the frontend map (default: scout-properties.json)."
    )

    args = parser.parse_args()
    db_path = _normalize_path(args.db)
    json_path = _normalize_path(args.json_path)

    export(db_path, json_path)

    json_backup = Path(f"{json_path}.{date.today():%Y.%m.%d}")
    shutil.copy2(json_path, json_backup)
    db_backup = Path(f"{db_path}.{date.today():%Y.%m.%d}")
    shutil.copy2(db_path, db_backup)
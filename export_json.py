"""
Exports current property/auction state from SQLite into properties.json —
the single static file the frontend map loads. Run this as the last step
of the weekly automation, after load_csv.py has ingested the week's spider output.

Usage:
    python export_json.py auctionscout.db scout-properties.json

Design notes:
- One flat array, one object per property. No pagination — client does all filtering.
- Statuses are treated as "live" (exported) by default. Only statuses explicitly
  listed in EXCLUDED_STATUSES are dropped from the map — this is safer than a
  whitelist given real scraped data has varied status wording across six sources
  (e.g. "sold back to mortgagee" and "3rd party purchase" both mean the auction
  already concluded with a sale). Excluded properties remain in SQLite for history.
- auction_datetime stays ISO8601 so the frontend can do its own date-bucket logic
  (This Week / This Month / All) relative to *today*, rather than trusting a
  stale precomputed bucket from scrape time.
- The output JSON's top level includes accounting fields (total_in_db,
  excluded_total, excluded_by_status, excluded_missing_coords) so it's
  possible to see why the exported count differs from what load_csv.py
  reported, without running a separate diagnostic script.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# Statuses that mean the auction is over and should NOT appear on the live map.
# Add to this list as new terminal-status wording turns up across sources.
# Everything NOT in this list is treated as still-live and gets exported.
EXCLUDED_STATUSES = (
    "sold back to mortgagee",
    "3rd party purchase",
    "canceled",
    "cancelled",
    "withdrawn",
    'bank buy back'
)

# If a property hasn't been re-confirmed by a scrape within this many days,
# treat it as no longer live regardless of its last recorded status — a
# source may have quietly removed a listing without our ever seeing an
# explicit status change (see load_csv.py's "disappeared" event detection).
# Set generously above the expected weekly run cadence to tolerate a missed
# run or two without prematurely hiding still-valid listings.
STALE_AFTER_DAYS = 14


def export(db_path: str, json_path: str):
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
        print("Note: property_duplicate_links table not found — skipping duplicate "
              "exclusion (run dedup_properties.py after updating schema.sql to enable it).")

    # ISO8601 timestamps compare correctly as plain strings, so this avoids
    # relying on SQLite's date-function parsing of the exact format we write.
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()

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
            a.auction_datetime  AS auction_date,
            a.status,
            a.property_type,
            a.bedrooms,
            a.bathrooms,
            a.sqft,
            a.lot_size_raw      AS lot_size,
            a.year_built,
            a.source_url        AS url
        FROM properties p
        JOIN auctions a ON a.property_id = p.property_id
        WHERE a.status NOT IN ({placeholders})
          AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
          AND p.last_seen_at >= ?
          {dedup_exclusion}
        ORDER BY a.auction_datetime ASC
        """,
        EXCLUDED_STATUSES + (stale_cutoff,),
        ).fetchall()

    properties = []
    for r in rows:
        row = dict(r)
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
        "excluded_stale_count": len(excluded_stale),
        "excluded_stale": excluded_stale[:25],  # capped sample
    }

    with open(json_path, "w") as f:
        json.dump(
            {**meta, "properties": properties},
            f,
            indent=None,  # keep file size down; not meant to be hand-read
        )

    print(f"Exported {len(properties)} of {total_in_db} auctions to {json_path}")
    if excluded_by_status:
        print("Excluded by status:")
        for status, cnt in sorted(excluded_by_status.items(), key=lambda x: -x[1]):
            print(f"  {status!r}: {cnt}")
    if excluded_missing_coords:
        print(f"Excluded for missing coordinates (live status, no lat/lon): {len(excluded_missing_coords)}")
        for r in excluded_missing_coords[:10]:
            print(f"  [{r['source']}] {r['address']!r} (status={r['status']!r})")
        if len(excluded_missing_coords) > 10:
            print(f"  ... and {len(excluded_missing_coords) - 10} more (see JSON meta for up to 25)")
    if excluded_duplicates_count:
        print(f"Excluded as cross-source duplicates: {excluded_duplicates_count} "
              f"(run dedup_properties.py to recompute)")
    if excluded_stale:
        print(f"Excluded as stale (not re-confirmed in {STALE_AFTER_DAYS}+ days despite live status): "
              f"{len(excluded_stale)}")
        for r in excluded_stale[:10]:
            print(f"  [{r['source']}] {r['address']!r} (last seen {r['last_seen_at']})")

    conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "auctionscout.db"
    json_path = sys.argv[2] if len(sys.argv) > 2 else "scout-properties.json"
    export(db_path, json_path)
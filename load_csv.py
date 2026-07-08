"""
Loads a spider-output CSV (markers.csv format) into the AuctionScout SQLite DB.

Usage:
    python load_csv.py markers.csv auctionscout.db

Idempotent: re-running against an updated CSV will detect status/date changes
on existing properties and log them to auction_events, rather than duplicating rows.
"""

import csv
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from dateutil import parser as dateparser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def extract_listing_id(url: str) -> str:
    """Pull the ?id= param from the auction URL. Falls back to the full URL if absent."""
    qs = parse_qs(urlparse(url).query)
    if "id" in qs:
        return qs["id"][0]
    return url


def parse_auction_datetime(raw: str):
    """'Wed. Jul. 8, 2026 at 11 am' -> ISO8601. Returns None if unparseable."""
    if not raw:
        return None
    cleaned = raw.replace(" at ", " ").replace(".", "")
    try:
        dt = dateparser.parse(cleaned, fuzzy=True)
        return dt.isoformat()
    except (ValueError, OverflowError):
        return None


# Description field looks like:
# "Single Family Home | Property Type: Residential; Mortgage Ref: ...; Lot Size: 4.32 acres;
#  Square Feet: 4,268 sf; # Bedrooms: 4; # Baths: 2; Year Built: 1981; County: Norfolk"
FIELD_PATTERNS = {
    "property_type": r"Property Type:\s*([^;]+)",
    "mortgage_ref": r"Mortgage Ref:\s*([^;]+)",
    "lot_size_raw": r"Lot Size:\s*([^;]+)",
    "sqft": r"Square Feet:\s*([\d,]+)",
    "bedrooms": r"#\s*Bedrooms:\s*(\d+)",
    "year_built": r"Year Built:\s*(\d{4})",
    "county": r"County:\s*([^;]+)",
}
# Baths is messier ("2" or "3 full / 2 half") — capture raw, coerce to float where simple
BATH_PATTERN = r"#\s*Baths:\s*([^;]+)"


def parse_description(desc: str) -> dict:
    out = {
        "property_type": None, "mortgage_ref": None, "lot_size_raw": None,
        "sqft": None, "bedrooms": None, "bathrooms": None,
        "year_built": None, "county": None,
    }
    if not desc:
        return out
    for field, pattern in FIELD_PATTERNS.items():
        m = re.search(pattern, desc)
        if m:
            val = m.group(1).strip()
            if field in ("sqft",):
                val = val.replace(",", "")
                out[field] = int(val) if val.isdigit() else None
            elif field == "bedrooms":
                out[field] = int(val)
            elif field == "year_built":
                out[field] = int(val)
            else:
                out[field] = val

    m = re.search(BATH_PATTERN, desc)
    if m:
        bath_raw = m.group(1).strip()
        simple = re.match(r"^(\d+(\.\d+)?)$", bath_raw)
        if simple:
            out["bathrooms"] = float(simple.group(1))
        else:
            # e.g. "3 full / 2 half" -> approximate as 3.5
            full = re.search(r"(\d+)\s*full", bath_raw)
            half = re.search(r"(\d+)\s*half", bath_raw)
            total = (int(full.group(1)) if full else 0) + 0.5 * (int(half.group(1)) if half else 0)
            out["bathrooms"] = total if total else None

    return out


def load(csv_path: str, db_path: str):
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)

    schema_path = os.path.join(SCRIPT_DIR, "schema.sql")
    if os.path.exists(schema_path):
        conn.executescript(open(schema_path, encoding="utf-8").read())
    # If schema.sql isn't present, assume db_path already has the tables
    # (e.g. this is an existing database) and proceed without it.

    cur = conn.cursor()

    ts = now_iso()
    records_found = 0
    records_new = 0
    records_changed = 0
    records_failed = 0
    failures = []
    source_name = None
    sources_in_this_run = set()

    try:
        cur.execute(
            "INSERT INTO spider_runs (source, started_at, status) VALUES (?, ?, 'running')",
            ("pending", ts),
        )
    except sqlite3.OperationalError as e:
        print(f"ERROR: database at {db_path} doesn't have the expected tables ({e}).")
        print("Either place schema.sql alongside load_csv.py to initialize a fresh "
              "database, or point db_path at an existing AuctionScout database.")
        sys.exit(1)
    run_id = cur.lastrowid

    # utf-8-sig transparently strips a UTF-8 BOM if present (common when a CSV
    # is saved from Excel or some Windows tools) and behaves identically to
    # plain utf-8 when no BOM exists — so this is a strict improvement either way.
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        expected_cols = {"Name", "Latitude", "Longitude", "Source", "State",
                         "Description", "Auction Date/Time", "Status", "PDF Links", "URL"}
        missing_cols = expected_cols - set(reader.fieldnames or [])
        if missing_cols:
            print(f"ERROR: CSV is missing expected column(s): {sorted(missing_cols)}")
            print(f"Columns found: {reader.fieldnames}")
            sys.exit(1)

        for line_num, row in enumerate(reader, start=2):  # header is line 1
            records_found += 1
            try:
                source_name = row["Source"]
                sources_in_this_run.add(source_name)
                listing_id = extract_listing_id(row["URL"])
                parsed_desc = parse_description(row["Description"])
                auction_dt = parse_auction_datetime(row["Auction Date/Time"])
                status = row["Status"].strip().lower()

                # --- upsert property ---
                cur.execute(
                    "SELECT property_id FROM properties WHERE source=? AND source_listing_id=?",
                    (row["Source"], listing_id),
                )
                existing = cur.fetchone()
                if existing:
                    property_id = existing[0]
                    cur.execute(
                        "UPDATE properties SET last_seen_at=? WHERE property_id=?",
                        (ts, property_id),
                    )
                else:
                    cur.execute(
                        """INSERT INTO properties
                           (source, source_listing_id, address_raw, latitude, longitude,
                            state, county, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (row["Source"], listing_id, row["Name"], float(row["Latitude"]),
                         float(row["Longitude"]), row["State"], parsed_desc["county"], ts, ts),
                    )
                    property_id = cur.lastrowid
                    records_new += 1

                # --- upsert auction (current state) + diff against prior for events ---
                cur.execute(
                    "SELECT auction_id, status, auction_datetime FROM auctions WHERE property_id=?",
                    (property_id,),
                )
                prior = cur.fetchone()

                if prior is None:
                    cur.execute(
                        """INSERT INTO auctions
                           (property_id, auction_datetime_raw, auction_datetime, status,
                            description_raw, property_type, bedrooms, bathrooms, sqft,
                            lot_size_raw, year_built, mortgage_ref, source_url, last_updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (property_id, row["Auction Date/Time"], auction_dt, status,
                         row["Description"], parsed_desc["property_type"], parsed_desc["bedrooms"],
                         parsed_desc["bathrooms"], parsed_desc["sqft"], parsed_desc["lot_size_raw"],
                         parsed_desc["year_built"], parsed_desc["mortgage_ref"], row["URL"], ts),
                    )
                    auction_id = cur.lastrowid
                    cur.execute(
                        """INSERT INTO auction_events
                           (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
                           VALUES (?, 'first_seen', NULL, ?, ?, ?)""",
                        (auction_id, status, ts, run_id),
                    )
                    # PDF links (semicolon-separated)
                    for link in [u.strip() for u in row["PDF Links"].split(";") if u.strip()]:
                        cur.execute(
                            "INSERT INTO auction_pdf_links (auction_id, url) VALUES (?, ?)",
                            (auction_id, link),
                        )
                else:
                    auction_id, prior_status, prior_dt = prior
                    changed = False
                    if prior_status != status:
                        cur.execute(
                            """INSERT INTO auction_events
                               (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
                               VALUES (?, 'status_change', ?, ?, ?, ?)""",
                            (auction_id, prior_status, status, ts, run_id),
                        )
                        changed = True
                    if prior_dt != auction_dt:
                        cur.execute(
                            """INSERT INTO auction_events
                               (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
                               VALUES (?, 'date_change', ?, ?, ?, ?)""",
                            (auction_id, prior_dt, auction_dt, ts, run_id),
                        )
                        changed = True
                    if changed:
                        records_changed += 1

                    cur.execute(
                        """UPDATE auctions SET
                                               auction_datetime_raw=?, auction_datetime=?, status=?,
                                               description_raw=?, property_type=?, bedrooms=?, bathrooms=?, sqft=?,
                                               lot_size_raw=?, year_built=?, mortgage_ref=?, source_url=?, last_updated_at=?
                           WHERE auction_id=?""",
                        (row["Auction Date/Time"], auction_dt, status, row["Description"],
                         parsed_desc["property_type"], parsed_desc["bedrooms"], parsed_desc["bathrooms"],
                         parsed_desc["sqft"], parsed_desc["lot_size_raw"], parsed_desc["year_built"],
                         parsed_desc["mortgage_ref"], row["URL"], ts, auction_id),
                    )
            except Exception as e:
                records_failed += 1
                addr = row.get("Name", "<unknown address>")
                failures.append(f"  line {line_num} ({addr}): {type(e).__name__}: {e}")
                continue

    # --- Detect disappeared listings: present in a prior run, absent from this
    # one, for a source we actually scraped this time. We don't guess WHY it
    # vanished (sold, canceled, or a scraper miss) — just record the fact,
    # once, so it's visible in the audit trail. export_json.py's staleness
    # cutoff is what actually keeps it off the live map.
    #
    # NOTE: if sources_in_this_run is empty (a completely empty CSV — zero
    # rows for every source), this intentionally does NOT run at all. A
    # zero-row file can't distinguish "this source genuinely has no live
    # listings right now" from "the scraper crashed and produced nothing" —
    # treating those the same would risk mass-marking everything from a
    # source as disappeared just because of a bad scraper run.
    disappeared_count = 0
    if sources_in_this_run:
        src_placeholders = ",".join("?" for _ in sources_in_this_run)
        stale_candidates = cur.execute(
            f"""SELECT p.property_id, p.address_raw, p.source, a.auction_id, a.status
                FROM properties p
                JOIN auctions a ON a.property_id = p.property_id
                WHERE p.source IN ({src_placeholders}) AND p.last_seen_at != ?""",
            (*sources_in_this_run, ts),
        ).fetchall()
        for property_id, address, src, auction_id, status in stale_candidates:
            last_event = cur.execute(
                """SELECT event_type FROM auction_events WHERE auction_id=?
                   ORDER BY event_id DESC LIMIT 1""",
                (auction_id,),
            ).fetchone()
            if last_event and last_event[0] == "disappeared":
                continue  # already flagged on a prior run — don't spam the log
            cur.execute(
                """INSERT INTO auction_events
                   (auction_id, event_type, old_value, new_value, detected_at, spider_run_id)
                   VALUES (?, 'disappeared', ?, NULL, ?, ?)""",
                (auction_id, status, ts, run_id),
            )
            disappeared_count += 1

    cur.execute(
        """UPDATE spider_runs SET source=?, finished_at=?, records_found=?,
                                  records_new=?, records_changed=?, status='success' WHERE run_id=?""",
        (source_name or "unknown", now_iso(), records_found, records_new, records_changed, run_id),
    )

    conn.commit()
    print(f"Run {run_id}: {records_found} found, {records_new} new, {records_changed} changed, "
          f"{records_failed} failed, {disappeared_count} newly disappeared")
    if failures:
        print(f"\n{records_failed} row(s) failed to load:")
        for f in failures:
            print(f)
    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Load an AuctionScout markers.csv file into the SQLite database."
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to the markers.csv file to import."
    )
    parser.add_argument(
        "--db",
        default="auctionscout.db",
        help="SQLite database file (default: auctionscout.db)"
    )

    args = parser.parse_args()
    load(args.csv, args.db)
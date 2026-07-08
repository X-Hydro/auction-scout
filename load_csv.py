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
    schema_path = os.path.join(SCRIPT_DIR, "createAuctionScoutDB.sql")
    if not os.path.exists(schema_path):
        print(f"ERROR: schema.sql not found at {schema_path}")
        print("Make sure schema.sql sits in the same folder as load_csv.py.")
        sys.exit(1)
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.executescript(open(schema_path, encoding="utf-8").read())
    cur = conn.cursor()

    ts = now_iso()
    records_found = 0
    records_new = 0
    records_changed = 0
    records_failed = 0
    failures = []
    source_name = None

    cur.execute(
        "INSERT INTO spider_runs (source, started_at, status) VALUES (?, ?, 'running')",
        ("pending", ts),
    )
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

    cur.execute(
        """UPDATE spider_runs SET source=?, finished_at=?, records_found=?,
           records_new=?, records_changed=?, status='success' WHERE run_id=?""",
        (source_name, now_iso(), records_found, records_new, records_changed, run_id),
    )

    conn.commit()
    print(f"Run {run_id}: {records_found} found, {records_new} new, {records_changed} changed, {records_failed} failed")
    if failures:
        print(f"\n{records_failed} row(s) failed to load:")
        for f in failures:
            print(f)
    conn.close()


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "markers.csv"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "auctionscout.db"
    load(csv_path, db_path)
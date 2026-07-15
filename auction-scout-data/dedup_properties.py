"""
dedup_properties.py — Cross-source deduplication for AuctionScout.

Different auction sources sometimes list the SAME physical property (e.g. the
same foreclosure appearing on both Sullivan and Harmon Law). load_csv.py's own
dedup only catches the same (source, listing_id) being re-scraped — it can't
catch two DIFFERENT sources describing the same address.

This script finds those cross-source matches by pairing properties that are
geographically close, have the SAME house number (extracted via a simple
leading-number regex — no external address-parsing service needed), and a
similar street name. Requiring an exact house-number match is what prevents
adjacent-but-different properties (e.g. "128 North Street" vs "126 North
Street") from being merged just because they're nearby with similar-looking
street names — distance and street-name fuzziness alone aren't enough to
tell those apart. Matches are recorded as a soft link — WITHOUT deleting or
modifying either property's own data. Both keep their full independent
history; only the export step (export_json.py) uses this table to hide the
non-canonical one from the map.

Safe to re-run repeatedly: fully recomputes all links each time, so canonical
choices stay correct as statuses change week to week (e.g. if the property
that was previously canonical gets marked sold, the other source's still-live
listing becomes canonical on the next run).

Usage:
    python dedup_properties.py auctionscout.db
"""

import difflib
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DIST_THRESHOLD_M = 100         # candidate pairs must be within this many meters
STREET_SIM_THRESHOLD = 0.6     # difflib similarity ratio on street name only, 0..1

# Must match export_json.py's EXCLUDED_STATUSES — kept as a separate copy
# since these are standalone, independently-runnable scripts by design.
EXCLUDED_STATUSES = (
    "sold back to mortgagee",
    "3rd party purchase",
    "canceled",
    "cancelled",
    "withdrawn",
)

ABBREV = {
    r"\bstreet\b": "st", r"\bavenue\b": "ave", r"\bdrive\b": "dr",
    r"\broad\b": "rd", r"\bcourt\b": "ct", r"\blane\b": "ln",
    r"\bplace\b": "pl", r"\bboulevard\b": "blvd", r"\bnorth\b": "n",
    r"\bsouth\b": "s", r"\beast\b": "e", r"\bwest\b": "w",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_house_street(addr: str) -> tuple:
    """
    Splits a raw scraped address into (house_number, normalized_street_name).
    Addresses reliably start with a house number, so a simple regex handles
    this without needing an external parsing service (e.g. libpostal) — that
    would add a network dependency for a problem this small.
    Returns (None, "") if no leading house number is found.
    """
    a = addr.lower().strip()
    m = re.match(r"^(\d+[a-z]?)\s+(.*)", a)
    if not m:
        return (None, "")
    house_number = m.group(1)
    street = m.group(2)
    street = street.split(",")[0]  # drop ", City, ST" trailing portion
    street = re.sub(r"[^a-z0-9\s]", " ", street)
    for pattern, repl in ABBREV.items():
        street = re.sub(pattern, repl, street)
    street = re.sub(r"\s+", " ", street).strip()
    return (house_number, street)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def is_live(status: str) -> bool:
    return status not in EXCLUDED_STATUSES


def choose_canonical(a: dict, b: dict) -> tuple:
    """Returns (canonical, duplicate) — which property should be shown vs hidden."""
    a_live, b_live = is_live(a["status"]), is_live(b["status"])
    if a_live != b_live:
        return (a, b) if a_live else (b, a)
    if a["last_updated_at"] != b["last_updated_at"]:
        return (a, b) if a["last_updated_at"] > b["last_updated_at"] else (b, a)
    return (a, b) if a["property_id"] < b["property_id"] else (b, a)


def dedup(db_path: str):
    if not os.path.exists(db_path):
        print(f"ERROR: database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT p.property_id, p.source, p.address_raw, p.latitude, p.longitude,
                  a.status, a.last_updated_at
           FROM properties p
                    JOIN auctions a ON a.property_id = p.property_id
           WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL"""
    ).fetchall()
    properties = [dict(r) for r in rows]
    for p in properties:
        p["house_number"], p["street"] = parse_house_street(p["address_raw"])

    print(f"Comparing {len(properties)} properties across sources...")

    links = []  # (duplicate_id, canonical_id, distance_m, score)
    n = len(properties)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = properties[i], properties[j]
            if a["source"] == b["source"]:
                continue  # same-source dupes already handled by load_csv.py

            # Cheap bounding-box pre-filter before the more expensive haversine calc.
            if abs(a["latitude"] - b["latitude"]) > 0.0015 or abs(a["longitude"] - b["longitude"]) > 0.0015:
                continue

            dist = haversine_m(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
            if dist > DIST_THRESHOLD_M:
                continue

            # House number must match exactly — this is the key discriminator that
            # prevents adjacent-but-different properties (e.g. "128 North Street"
            # vs "126 North Street") from being merged just because they're close
            # together and their street names are nearly identical strings.
            if not a["house_number"] or not b["house_number"]:
                continue
            if a["house_number"] != b["house_number"]:
                continue

            score = difflib.SequenceMatcher(None, a["street"], b["street"]).ratio()
            if score < STREET_SIM_THRESHOLD:
                continue

            canonical, duplicate = choose_canonical(a, b)
            links.append((duplicate["property_id"], canonical["property_id"], dist, score))

    ts = now_iso()
    conn.execute("DELETE FROM property_duplicate_links")
    conn.executemany(
        """INSERT INTO property_duplicate_links
           (property_id, canonical_property_id, match_distance_m, match_score, detected_at)
           VALUES (?, ?, ?, ?, ?)""",
        [(dup_id, can_id, dist, score, ts) for dup_id, can_id, dist, score in links],
    )
    conn.commit()

    print(f"Found {len(links)} duplicate link(s).")
    if links:
        print("\nDuplicate pairs (property_id hidden -> canonical shown):")
        addr_by_id = {p["property_id"]: (p["source"], p["address_raw"]) for p in properties}
        for dup_id, can_id, dist, score in links:
            dup_src, dup_addr = addr_by_id[dup_id]
            can_src, can_addr = addr_by_id[can_id]
            print(f"  [{dup_src}] {dup_addr!r} -> [{can_src}] {can_addr!r} "
                  f"(dist={dist:.0f}m, score={score:.2f})")

    conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "auctionscout.db"
    dedup(db_path)
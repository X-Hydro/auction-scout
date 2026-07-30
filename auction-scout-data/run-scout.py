"""
Run registered spiders and write a unified markers.csv for Google My Maps.

Usage:
    python run-scout.py                       # run all available spiders
    python run-scout.py --spiders sullivan    # run just one
    python run-scout.py --spiders sullivan harmon
    python run-scout.py --list                # show what's registered/available
"""

import argparse
import csv
import re
import sys
import shutil
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "spiders"))

from spiders.sullivan import SullivanSpider
from spiders.harmon import HarmonSpider
from spiders.brockscott import BrockScottSpider
from spiders.jjmanning import JJManningSpider
from spiders.towne import TowneAuctionSpider
from spiders.patriot import PatriotSpider
from spiders.skypoint import SkypointSpider
from spiders.landmark import LandmarkSpider

from base import DEFAULT_OVERRIDES_PATH
from geocode import reverse_geocode_geography, geocode_with_fallbacks


# Spiders that are implemented and ready to run.
REGISTRY = {
    "sullivan": SullivanSpider,
    "harmon": HarmonSpider,
    "brockscott": BrockScottSpider,
    "jjmanning": JJManningSpider,
    "towne": TowneAuctionSpider,
    "patriot": PatriotSpider,
    "skypoint": SkypointSpider,
    "landmark": LandmarkSpider,
}

# Spiders that exist as a stub but are intentionally not runnable yet
# (e.g. blocked by robots.txt) -- listed here so --spiders gives a clear
# explanation instead of an argparse "invalid choice" error.
KNOWN_UNAVAILABLE = {}

DEFAULT_OUT_PATH = "markers.csv"  # used when multiple spiders ran in one pass
FIELDNAMES = [
    "ID", "Name", "Latitude", "Longitude", "Source", "State", "County", "Municipality",
    "Timing", "Property Type", "Bedrooms", "Bathrooms", "Sqft", "Lot Size", "Year Built",
    "Description", "Auction Date/Time", "Status", "PDF Links", "URL",
]


def parse_args():
    all_choices = sorted(REGISTRY) + sorted(KNOWN_UNAVAILABLE) + ["all"]
    p = argparse.ArgumentParser(description="Scrape auction listings into markers.csv")
    p.add_argument(
        "--spiders", nargs="+", default=["all"], choices=all_choices,
        metavar="SPIDER",
        help=f"Which spider(s) to run. Choices: {', '.join(all_choices)}. Default: all",
    )
    p.add_argument("--list", action="store_true", help="List registered spiders and exit")
    p.add_argument(
        "--split-output", action="store_true",
        help="Also write a separate {source}_markers.csv per spider, "
             "in addition to the merged markers.csv",
    )
    return p.parse_args()


def resolve_spiders(names):
    if "all" in names:
        names = list(REGISTRY)

    spider_classes = []
    for name in names:
        if name in KNOWN_UNAVAILABLE:
            print(f"[{name}] SKIPPED -- {KNOWN_UNAVAILABLE[name]}")
            continue
        spider_classes.append(REGISTRY[name])

    return spider_classes


# Matches a 2-letter state abbreviation, optionally followed by a zip
# (5-digit or ZIP+4). Anchored to the end of the string since city_state's
# last comma segment is "STATE" or "STATE ZIP" (e.g. "MA" or "MA 01880") --
# never anchoring to start avoids false-matching 2-letter fragments earlier
# in a messier city name.
STATE_RE = re.compile(r"\b([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$")


# ---- cross-source dedup ------------------------------------------------
#
# Confirmed 2026-07-30: Landmark Auction Co. (the auction house) and
# Brock & Scott (the law firm) list many of the same MA/NH/RI/VT
# foreclosures -- same property, two different spiders, two different
# ids, no relationship between the rows anywhere in the pipeline. Without
# this step they'd show up as two separate markers on the map. Landmark's
# listing has photos and clearer status/date detail, so it wins whenever
# both sources have a row for the same address.
#
# Deliberately scoped to ONLY this confirmed pair (see DEDUP_SOURCE_PRIORITY)
# rather than deduping any two sources that happen to produce the same
# normalized address -- an unconfirmed collision between two OTHER sources
# is more likely a coincidence (or a real bug worth seeing) than an actual
# duplicate, and silently dropping a real listing is worse than showing an
# extra marker. Same "explicit, visible exception" philosophy as
# harmon.py's respect_robots override -- this isn't a blanket assumption
# that any two sources overlap.
DEDUP_SOURCE_PRIORITY = {
    frozenset({"landmark", "brockscott"}): ["landmark", "brockscott"],
}

# Common street-type words that show up spelled out on one site and
# abbreviated on the other (e.g. Landmark's "128 North Street" vs Brock &
# Scott's own formatting choices) -- normalized to a single form so
# matching doesn't depend on which spelling a given source happened to use.
_STREET_ABBR = {
    "STREET": "ST", "DRIVE": "DR", "ROAD": "RD", "AVENUE": "AVE",
    "LANE": "LN", "TERRACE": "TER", "CIRCLE": "CIR", "COURT": "CT",
    "PLACE": "PL", "BOULEVARD": "BLVD", "PARKWAY": "PKWY", "SQUARE": "SQ",
    "TRAIL": "TRL", "EXTENSION": "EXT", "HIGHWAY": "HWY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_UNIT_RE = re.compile(r"\b(?:UNIT|APT|#)\s*([A-Z0-9-]+)\b", re.IGNORECASE)
_ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*$")


def _normalize_street(street):
    """Uppercase, strip punctuation, and normalize common street-type/
    directional words so '128 North Street' and '128 North St' (or
    'N St') compare equal. Only compares against the FIRST name when a
    Landmark title has an 'a/k/a' alias -- Brock & Scott's addresses never
    carry one, so that's the only name it could ever match against."""
    if not street:
        return ""
    s = street.split(" a/k/a ")[0]
    s = re.sub(r"[.,]", "", s).upper().strip()
    s = re.sub(r"\s+", " ", s)
    for full, abbr in _STREET_ABBR.items():
        s = re.sub(rf"\b{full}\b", abbr, s)
    # Unit/apt info is compared separately (see _dedup_key) -- strip it
    # out here so "128 North St" and "128 North St Unit 4" still match on
    # the base street.
    s = _UNIT_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _dedup_key(row):
    """(zip, normalized street, unit) -- unit is kept as a SEPARATE part
    of the key (not folded into the street) specifically so two different
    units in the same building (e.g. '111 Foster St Unit 510' and
    'Unit 302') are never treated as duplicates of each other; only an
    exact street+unit+zip match collapses two rows."""
    zip_match = _ZIP_RE.search(row.get("city_state", "") or "")
    zip_code = zip_match.group(1) if zip_match else ""
    unit_match = _UNIT_RE.search(row.get("street", "") or "")
    unit = unit_match.group(1).upper() if unit_match else ""
    return zip_code, _normalize_street(row.get("street", "")), unit


def dedup_cross_source(rows):
    """
    Collapse rows that represent the same physical auction listed on more
    than one confirmed-overlapping source (see DEDUP_SOURCE_PRIORITY),
    keeping only the highest-priority source's row. Rows that can't be
    matched confidently (no zip found) or whose sources aren't a known
    overlapping pair are left alone -- better an extra marker than a
    silently dropped real listing.
    """
    keyed = {}
    unkeyable = []
    for row in rows:
        zip_code, street_norm, unit = _dedup_key(row)
        if not zip_code or not street_norm:
            unkeyable.append(row)
            continue
        keyed.setdefault((zip_code, street_norm, unit), []).append(row)

    kept = list(unkeyable)
    dropped = []
    for group in keyed.values():
        if len(group) == 1:
            kept.append(group[0])
            continue

        sources_present = {r["source"] for r in group}
        priority = next(
            (order for pair, order in DEDUP_SOURCE_PRIORITY.items()
             if sources_present <= pair),
            None,
        )
        if priority is None:
            # Same normalized address, but not a pair we've confirmed
            # overlaps -- don't guess, keep every row.
            kept.extend(group)
            continue

        group.sort(key=lambda r: priority.index(r["source"]))
        kept.append(group[0])
        dropped.extend((group[0], loser) for loser in group[1:])

    if dropped:
        print(f"Cross-source dedup: dropped {len(dropped)} duplicate row(s):")
        for winner, loser in dropped:
            print(f"  kept [{winner['source']}:{winner['id']}] over "
                  f"[{loser['source']}:{loser['id']}] -- "
                  f"{winner.get('street', '')}, {winner.get('city_state', '')}")

    return kept

def format_row(row):

    return {
        # "{source}:{id}" mirrors the geocode_batch() key -- this is the
        # stable identifier a customer can quote back to us to debug a
        # specific listing, and (once a diff/report step exists) the join
        # key for detecting new/changed/removed auctions run over run.
        "ID": f"{row.get('source', '')}:{row.get('id', '')}",
        "Name": f"{row['street']}, {row['city_state']}",
        "Latitude": row["latitude"],
        "Longitude": row["longitude"],
        "Source": row["source"],
        "State": row.get("state", ""),
        "County": row.get("county", ""),
        "Municipality": row.get("municipality", ""),
        "Timing": row.get("timing", "Unknown"),
        "Property Type": row.get("property_type", "") or "",
        "Bedrooms": row.get("bedrooms", "") or "",
        "Bathrooms": row.get("bathrooms", "") or "",
        "Sqft": row.get("sqft", "") or "",
        "Lot Size": row.get("lot_size", "") or "",
        "Year Built": row.get("year_built", "") or "",
        # Free-text only now -- case #, court SP #, opening bid, book page,
        # and anything else that doesn't have (or doesn't yet have) its own
        # structured column.
        "Description": row.get("extra_fields", ""),
        "Auction Date/Time": row["date_time"],
        "Status": row["status"],
        "PDF Links": row.get("pdf_links", ""),
        "URL": row["url"],
    }


def write_csv(path, rows):
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            if row["latitude"] is None:
                continue
            writer.writerow(format_row(row))
            written += 1
    return written


def main():
    args = parse_args()

    if args.list:
        print("Available:")
        for name in sorted(REGISTRY):
            print(f"  {name}")
        print("Registered but unavailable:")
        for name, reason in sorted(KNOWN_UNAVAILABLE.items()):
            print(f"  {name} -- {reason}")
        return

    spider_classes = resolve_spiders(args.spiders)
    if not spider_classes:
        print("No runnable spiders selected. Nothing to do.")
        return

    all_rows = []
    for spider_cls in spider_classes:
        spider = spider_cls()
        print(f"--- {spider.name} ---")
        rows = spider.scrape()
        print(f"[{spider.name}] {len(rows)} auctions after dedupe/status filter")
        all_rows.extend(rows)

    all_rows = dedup_cross_source(all_rows)

    print("Geocoding addresses...")
    address_pairs = [
        (f"{r['source']}:{r['id']}", f"{r['street']}, {r['city_state']}")
        for r in all_rows if r.get("id")
    ]
    coords, still_unmatched = geocode_with_fallbacks(address_pairs)

    for row in all_rows:
        key = f"{row['source']}:{row['id']}"
        lat, lon = coords.get(key, (None, None))
        row["latitude"] = lat
        row["longitude"] = lon

        if lat is not None and lon is not None:
            geo = reverse_geocode_geography(lat, lon)
            row["state"] = geo["state"]
            row["county"] = geo["county"]
            row["municipality"] = geo["municipality"]

    if still_unmatched:
        print(f"\n{len(still_unmatched)} address(es) could not be geocoded "
              f"automatically (Census + Nominatim both failed):")
        for aid, addr in still_unmatched:
            print(f"  - [{aid}] {addr}")
        print(f"\nTo fix: look up coordinates manually (e.g. Google Maps -- "
              f"right-click the pin, click the lat/lon to copy it), then add "
              f"a row to {DEFAULT_OVERRIDES_PATH} (create it with this header "
              f"if it doesn't exist yet: id,latitude,longitude,address,note). "
              f"Ready-to-fill lines for the addresses above:")
        print("  id,latitude,longitude,address,note")
        for aid, addr in still_unmatched:
            print(f'  {aid},,,"{addr}",')
        print(f"Next run will pick these up automatically once filled in.")

    if len(spider_classes) == 1:
        out_path = f"{spider_classes[0].name}.csv"
    else:
        out_path = DEFAULT_OUT_PATH

    written = write_csv(out_path, all_rows)
    print(f"Wrote {out_path} ({written} markers from {len(spider_classes)} spider(s))")

    if args.split_output:
        sources = sorted({row["source"] for row in all_rows})
        if len(sources) <= 1:
            print("--split-output skipped: only one source in this run, "
                  f"same as {out_path}")
        else:
            for source in sources:
                source_rows = [r for r in all_rows if r["source"] == source]
                path = f"{source}_markers.csv"
                n = write_csv(path, source_rows)
                print(f"Wrote {path} ({n} markers)")
                backup = Path(f"{path}.{date.today():%Y.%m.%d}")
                shutil.copy2(path, backup)


if __name__ == "__main__":
    main()
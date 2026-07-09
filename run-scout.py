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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "spiders"))

from spiders.sullivan import SullivanSpider
from spiders.harmon import HarmonSpider
from spiders.brockscott import BrockScottSpider
from spiders.jjmanning import JJManningSpider
from spiders.towne import TowneAuctionSpider
from spiders.patriot import PatriotSpider
from base import geocode_batch

# Spiders that are implemented and ready to run.
REGISTRY = {
    "sullivan": SullivanSpider,
    "harmon": HarmonSpider,
    "brockscott": BrockScottSpider,
    "jjmanning": JJManningSpider,
    "towne": TowneAuctionSpider,
    "patriot": PatriotSpider,
}

# Spiders that exist as a stub but are intentionally not runnable yet
# (e.g. blocked by robots.txt) -- listed here so --spiders gives a clear
# explanation instead of an argparse "invalid choice" error.
KNOWN_UNAVAILABLE = {}

DEFAULT_OUT_PATH = "markers.csv"  # used when multiple spiders ran in one pass
FIELDNAMES = [
    "ID", "Name", "Latitude", "Longitude", "Source", "State", "County", "Timing",
    "Property Type", "Bedrooms", "Bathrooms", "Sqft", "Lot Size", "Year Built",
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


def extract_state(city_state):
    """Pull just the 2-letter state code out of a city_state string, even
    when a zip is glued on after it with no comma (e.g. 'Wakefield, MA
    01880' -> 'MA', not 'MA 01880'). Falls back to the raw last-comma
    segment if the pattern doesn't match, rather than returning nothing."""
    if "," not in city_state:
        return ""
    last_segment = city_state.split(",")[-1].strip()
    m = STATE_RE.search(last_segment)
    return m.group(1) if m else last_segment


def format_row(row):
    state = extract_state(row["city_state"])
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
        "State": state,
        # County now comes through as its own field (see spiders/brockscott.py)
        # rather than being parsed back out of a merged Description string --
        # keep that pattern for any future spider: if a site's page has a
        # field structured, carry it through structured, don't flatten it into
        # free text and make some downstream step re-parse it.
        "County": row.get("county", ""),
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

    print("Geocoding addresses...")
    address_pairs = [
        (f"{r['source']}:{r['id']}", f"{r['street']}, {r['city_state']}")
        for r in all_rows if r.get("id")
    ]
    coords = geocode_batch(address_pairs)

    for row in all_rows:
        key = f"{row['source']}:{row['id']}"
        lat, lon = coords.get(key, (None, None))
        row["latitude"] = lat
        row["longitude"] = lon

    unmatched = [r for r in all_rows if r["latitude"] is None]
    if unmatched:
        print(f"Warning: {len(unmatched)} address(es) failed to geocode:")
        for r in unmatched:
            print(f"  - [{r['source']}] {r['street']}, {r['city_state']}")

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


if __name__ == "__main__":
    main()
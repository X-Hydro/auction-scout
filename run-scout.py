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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "spiders"))

from spiders.sullivan import SullivanSpider
from spiders.harmon import HarmonSpider
from base import geocode_batch

# Spiders that are implemented and ready to run.
REGISTRY = {
    "sullivan": SullivanSpider,
    "harmon": HarmonSpider,
}

# Spiders that exist as a stub but are intentionally not runnable yet
# (e.g. blocked by robots.txt) -- listed here so --spiders gives a clear
# explanation instead of an argparse "invalid choice" error.
KNOWN_UNAVAILABLE = {}

OUT_PATH = "markers.csv"
FIELDNAMES = [
    "Name", "Latitude", "Longitude", "Source", "State", "Timing",
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


def format_row(row):
    state = row["city_state"].split(",")[-1].strip() if "," in row["city_state"] else ""
    return {
        "Name": f"{row['street']}, {row['city_state']}",
        "Latitude": row["latitude"],
        "Longitude": row["longitude"],
        "Source": row["source"],
        "State": state,
        "Timing": row.get("timing", "Unknown"),
        "Description": f"{row.get('description', '')} | {row.get('extra_fields', '')}",
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

    written = write_csv(OUT_PATH, all_rows)
    print(f"Wrote {OUT_PATH} ({written} markers from {len(spider_classes)} spider(s))")

    if args.split_output:
        sources = sorted({row["source"] for row in all_rows})
        for source in sources:
            source_rows = [r for r in all_rows if r["source"] == source]
            path = f"{source}_markers.csv"
            n = write_csv(path, source_rows)
            print(f"Wrote {path} ({n} markers)")


if __name__ == "__main__":
    main()
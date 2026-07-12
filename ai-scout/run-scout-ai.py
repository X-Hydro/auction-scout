"""
run-scout-ai.py -- AI-enhanced version of run-scout.py.

Same usage as the original:
    python run-scout-ai.py                       # run all available spiders
    python run-scout-ai.py --spiders sullivan    # run just one
    python run-scout-ai.py --spiders sullivan harmon
    python run-scout-ai.py --list                # show what's registered/available

Only 3 of the 6 spiders (sullivan, jjmanning, patriot) actually use AI --
harmon/brockscott/towne are reused UNCHANGED from ../spiders/, since
their sites don't expose property specs at all (see each spider's module
docstring in the original repo). --list flags which is which.

Requires ANTHROPIC_API_KEY set in your shell before running:
    export ANTHROPIC_API_KEY="sk-ant-..."
Without it, sullivan/jjmanning/patriot will still run (id/url/date/status/
street/city_state all come from unchanged parse_listing() logic), but
every parse_detail() AI call will fail and log a warning, falling back to
blank property-spec fields for that listing.

At the end of the run, this prints REAL measured API usage/cost for this
specific run (not an estimate) -- see ai_property_extractor.get_stats().
"""
import argparse
import csv
import re
import sys
from pathlib import Path

THIS_DIR = Path(__file__).parent
REPO_ROOT = THIS_DIR.parent

# Parent repo root -- base.py, geocode.py, and spiders/ live here. Same
# sys.path pattern run-scout.py itself uses for spiders/.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR / "spiders_ai"))

from spiders_ai.sullivan_ai import SullivanAISpider
from spiders_ai.jjmanning_ai import JJManningAISpider
from spiders_ai.patriot_ai import PatriotAISpider
# No AI benefit on these three (property specs genuinely absent from
# their sites) -- reuse the original spiders UNCHANGED rather than
# duplicating them here.
from spiders.harmon import HarmonSpider
from spiders.brockscott import BrockScottSpider
from spiders.towne import TowneAuctionSpider

from base import DEFAULT_OVERRIDES_PATH
from geocode import reverse_geocode_geography, geocode_with_fallbacks
import ai_property_extractor


REGISTRY = {
    "sullivan": SullivanAISpider,
    "harmon": HarmonSpider,
    "brockscott": BrockScottSpider,
    "jjmanning": JJManningAISpider,
    "towne": TowneAuctionSpider,
    "patriot": PatriotAISpider,
}

# For --list, so it's obvious which spiders this script actually changes.
AI_ENHANCED = {"sullivan", "jjmanning", "patriot"}

KNOWN_UNAVAILABLE = {}

DEFAULT_OUT_PATH = "markers.csv"
FIELDNAMES = [
    "ID", "Name", "Latitude", "Longitude", "Source", "State", "County", "Municipality",
    "Timing", "Property Type", "Bedrooms", "Bathrooms", "Sqft", "Lot Size", "Year Built",
    "Description", "Auction Date/Time", "Status", "PDF Links", "URL",
]


def parse_args():
    all_choices = sorted(REGISTRY) + sorted(KNOWN_UNAVAILABLE) + ["all"]
    p = argparse.ArgumentParser(description="Scrape auction listings (AI-enhanced) into markers.csv")
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


STATE_RE = re.compile(r"\b([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$")


# NOTE: format_row/write_csv are intentionally duplicated from
# run-scout.py rather than imported -- run-scout.py isn't importable as
# a module (hyphenated filename), same reason the original never
# factored these into base.py either. Keep both in sync if the CSV
# schema (FIELDNAMES) ever changes.
def format_row(row):
    return {
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


def _require_overrides_file(overrides_path):
    """
    geocode_overrides.csv is REQUIRED, not optional -- some real addresses
    on these sites (confirmed: sullivan:21317, sullivan:21318, and likely
    others as more sites are added) fail BOTH Census and Nominatim
    automatic geocoding and can only be resolved via this manual file.
    Silently proceeding with zero overrides means those listings get
    dropped from the output CSV with no error at all (write_csv() skips
    any row with latitude=None) -- exactly the bug that motivated this
    check. Fail loudly and immediately instead, before wasting an AI
    extraction budget on a run that will lose data anyway.
    """
    path = Path(overrides_path)
    if not path.exists():
        print(f"\nFATAL: {overrides_path} does not exist.")
        print(f"This file is required -- it's the only way some real "
              f"addresses (confirmed: sullivan:21317, sullivan:21318) can "
              f"be geocoded at all; Census and Nominatim both fail on them.")
        print(f"Create it at the repo root with header:")
        print(f"  id,latitude,longitude,address,note")
        sys.exit(1)
    if path.stat().st_size == 0:
        print(f"\nFATAL: {overrides_path} exists but is empty (0 bytes).")
        print(f"Add at least the header row: id,latitude,longitude,address,note")
        sys.exit(1)
    # Has a header but zero actual override rows -- not fatal (a genuinely
    # empty-but-initialized file is plausible for a brand new site with no
    # known geocoding failures yet), but worth a visible warning rather
    # than silence, since it's easy to miss.
    with open(path, newline="", encoding="utf-8") as f:
        row_count = sum(1 for _ in csv.DictReader(f))
    if row_count == 0:
        print(f"WARNING: {overrides_path} has a header but zero override "
              f"rows. If any addresses are known to fail automatic "
              f"geocoding, they'll be silently dropped from the output.")
    else:
        print(f"[overrides] Loaded {overrides_path} ({row_count} manual override(s))")


def main():
    args = parse_args()

    if args.list:
        print("Available:")
        for name in sorted(REGISTRY):
            tag = " (AI-enhanced)" if name in AI_ENHANCED else " (no AI -- unchanged from ../spiders/)"
            print(f"  {name}{tag}")
        print("Registered but unavailable:")
        for name, reason in sorted(KNOWN_UNAVAILABLE.items()):
            print(f"  {name} -- {reason}")
        return

    spider_classes = resolve_spiders(args.spiders)
    if not spider_classes:
        print("No runnable spiders selected. Nothing to do.")
        return

    ai_property_extractor.reset_stats()

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
    # DEFAULT_OVERRIDES_PATH is a bare relative filename -- it resolves
    # against the CURRENT WORKING DIRECTORY, not this script's location.
    # Since run-scout-ai.py normally runs from ai-scout/ (one level below
    # where geocode_overrides.csv actually lives, alongside run-scout.py),
    # passing the bare default here would silently look in the wrong
    # place and find nothing -- exactly what happened before this fix
    # (2 known-hard-to-geocode listings were silently dropped from the
    # AI run's output because their manual overrides were never found).
    overrides_path = str(REPO_ROOT / DEFAULT_OVERRIDES_PATH)
    _require_overrides_file(overrides_path)
    coords, still_unmatched = geocode_with_fallbacks(address_pairs, overrides_path=overrides_path)

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
        print(f"\nTo fix: look up coordinates manually, then add a row to "
              f"{DEFAULT_OVERRIDES_PATH} (header: id,latitude,longitude,address,note).")
        print("  id,latitude,longitude,address,note")
        for aid, addr in still_unmatched:
            print(f'  {aid},,,"{addr}",')

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

    stats = ai_property_extractor.get_stats()
    print(f"\n--- AI usage this run ---")
    print(f"  New extractions (real API calls): {stats['api_calls']}")
    print(f"  Cache hits (no charge):            {stats['cache_hits']}")
    print(f"  Estimated cost this run:           ${stats['estimated_cost']:.4f}")


if __name__ == "__main__":
    main()
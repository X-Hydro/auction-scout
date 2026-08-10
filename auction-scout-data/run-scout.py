"""
Run registered spiders and write a unified markers.csv for Google My Maps.

Replaces the old run-scout.py / run-scout-ai.py split. One registry, one
CSV pipeline, no --ai flag -- there's no spider left that runs two
different ways depending on a flag; each spider either uses AI internally
or it doesn't, full stop (see USES_AI below). Rationale for merging: the
two scripts had drifted apart in ways that were only found while merging
them -- run-scout-ai.py's REGISTRY was missing skypoint/landmark entirely
(never backported after being added here), and it never called
dedup_cross_source() at all, so the Landmark/Brock & Scott overlap was
deduped in one script's output but not the other's. One registry means
there's nothing left to drift.

Layout this assumes (repo root = wherever this file lives):
    run-scout.py
    spiders/
        sullivan.py, harmon.py, brockscott.py, jjmanning.py, towne.py,
        patriot.py, skypoint.py, landmark.py
        keenan_ai.py   <- the one spider that uses AI; see below and its
                          own module docstring

Usage:
    python run-scout.py                       # run all spiders
    python run-scout.py --spiders keenan       # run just one
    python run-scout.py --spiders sullivan harmon
    python run-scout.py --list                 # show what's registered,
                                                 # and which use AI

keenan uses AI internally, unconditionally, every run -- requires
ANTHROPIC_API_KEY in your shell. Without it, keenan still runs and still
produces id/url/date/status/street/city_state/description/terms (all from
regex, unaffected), but property_type/bedrooms/bathrooms/sqft/lot_size/
year_built come back blank and a warning gets logged per listing -- see
spiders/keenan_ai.py's module docstring for why this site doesn't get a
no-AI mode the way the others do. Every other spider (sullivan, harmon,
brockscott, jjmanning, towne, patriot, skypoint, landmark) never calls
the API at all, including sullivan/jjmanning/patriot -- those had AI
variants in an earlier version of this pipeline that are now retired, not
carried forward, so their Property Type/Bedrooms/Bathrooms/Sqft/Lot Size/
Year Built columns stay blank same as harmon/brockscott/towne/skypoint/
landmark's always have.

geocode_overrides.csv is REQUIRED, not optional -- some real addresses
(confirmed: sullivan:21317, sullivan:21318) fail BOTH Census and
Nominatim automatic geocoding and can only be resolved via this manual
file.

Prints REAL measured API usage/cost for the run (not an estimate) --
see ai_property_extractor.get_stats() -- but only when keenan (or any
future AI-using spider) was actually part of the run, so a run without
it doesn't print a noisy "0 calls, $0.00" line.
"""

import argparse
import csv
import os
import re
import sys
import shutil
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "spiders"))

from spiders.sullivan import SullivanSpider
from spiders.harmon import HarmonSpider
from spiders.brockscott import BrockScottSpider
from spiders.jjmanning import JJManningSpider
from spiders.towne import TowneAuctionSpider
from spiders.patriot import PatriotSpider
from spiders.skypoint import SkypointSpider
from spiders.landmark import LandmarkSpider
from spiders.keenan_ai import KeenanAISpider

from base import DEFAULT_OVERRIDES_PATH
from geocode import reverse_geocode_geography, geocode_with_fallbacks
import ai_property_extractor
import run_qc


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
    "keenan_ai": KeenanAISpider,
}

# Informational only (--list) -- the _ai suffix on a filename
# (spiders/keenan_ai.py) is the primary at-a-glance signal for which
# spiders call the AI extractor internally; this set just surfaces the
# same fact in --list output without needing to check the filesystem.
# Not a toggle -- there's no flag that changes a spider's AI usage, each
# one just always does or never does.
USES_AI = {"keenan_ai"}

# Spiders that exist as a stub but are intentionally not runnable yet
# (e.g. blocked by robots.txt) -- listed here so --spiders gives a clear
# explanation instead of an argparse "invalid choice" error.
KNOWN_UNAVAILABLE = {}

DEFAULT_OUT_PATH = "markers.csv"  # used when multiple spiders ran in one pass
FIELDNAMES = [
    "ID", "Name", "Latitude", "Longitude", "Source", "State", "County", "Municipality",
    "Timing", "Property Type", "Bedrooms", "Bathrooms", "Sqft", "Lot Size", "Year Built",
    "Summary", "Description", "Metadata", "Auction Date/Time", "Status", "PDF Links", "URL",
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
    p.add_argument(
        "--strict", action="store_true",
        help="Exit with a nonzero status if any spider's row count looks "
             "anomalous (zero rows, or a sharp drop below recent normal, "
             "per run_qc.py). The run still completes and writes its "
             "output either way -- this only affects the exit code, so a "
             "scheduler/cron job can detect and alert on a bad run instead "
             "of it looking like a normal success.",
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


def _require_ai_key_if_needed(spider_classes):
    needs_ai = sorted({cls.name for cls in spider_classes if cls.name in USES_AI})
    if not needs_ai:
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        return

    print(f"\nFATAL: ANTHROPIC_API_KEY is not set in this environment.")
    print(f"{', '.join(needs_ai)} always call the AI extractor for every "
          f"listing and cannot fall back to a no-AI mode -- running "
          f"without a key would just produce blank property-spec columns "
          f"for every listing, with no clear error, which is worse than "
          f"not running at all.")
    print(f"Set it in the SAME terminal you'll run this from, then try "
          f"again. On Windows:")
    print(f'  PowerShell (this session only): $env:ANTHROPIC_API_KEY = "sk-ant-..."')
    print(f'  Persistent (open a NEW terminal after): setx ANTHROPIC_API_KEY "sk-ant-..."')
    print(f"On macOS/Linux:")
    print(f'  export ANTHROPIC_API_KEY="sk-ant-..."')
    print(f"To confirm it's actually visible before re-running the full scrape:")
    print(f'  python -c "import os; print(os.environ.get(\'ANTHROPIC_API_KEY\'))"')
    sys.exit(1)


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
# that any two sources overlap. (No Keenan entry yet -- it's the first
# Maine source, nothing confirmed to overlap with it so far.)
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
        "Summary": row.get("description", "") or "",
        "Description": row.get("extra_fields", ""),
        "Metadata": row.get("metadata", "") or "",
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
            tag = " (uses AI internally, every run)" if name in USES_AI else ""
            print(f"  {name}{tag}")
        print("Registered but unavailable:")
        for name, reason in sorted(KNOWN_UNAVAILABLE.items()):
            print(f"  {name} -- {reason}")
        return

    spider_classes = resolve_spiders(args.spiders)
    if not spider_classes:
        print("No runnable spiders selected. Nothing to do.")
        return

    _require_ai_key_if_needed(spider_classes)

    # Reset unconditionally -- cheap, and get_stats() at the end decides
    # whether to print based on whether anything actually happened, not
    # on a flag. Covers keenan (and any future AI-using spider) with no
    # special-casing needed here.
    ai_property_extractor.reset_stats()

    all_rows = []
    any_yield_warnings = False
    run_date = date.today().isoformat()
    stats_this_run = []
    for spider_cls in spider_classes:
        spider = spider_cls()
        print(f"--- {spider.name} ---")
        rows = spider.scrape()
        print(f"[{spider.name}] {len(rows)} auctions after dedupe/status filter")
        if run_qc.check_yield(spider.name, len(rows), run_qc.load_recent_counts(spider.name)):
            any_yield_warnings = True
        stats_this_run.append((spider.name, len(rows)))
        all_rows.extend(rows)
    run_qc.append_stats(run_date, stats_this_run)

    all_rows = dedup_cross_source(all_rows)

    print("Geocoding addresses...")
    address_pairs = [
        (f"{r['source']}:{r['id']}", f"{r['street']}, {r['city_state']}")
        for r in all_rows if r.get("id")
    ]
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

    dated_backup = Path(f"{out_path}.{date.today():%Y.%m.%d}")
    shutil.copy2(out_path, dated_backup)
    print(f"Wrote dated snapshot {dated_backup}")

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

    stats = ai_property_extractor.get_stats()
    if stats["api_calls"] or stats["cache_hits"]:
        print(f"\n--- AI usage this run ---")
        print(f"  New extractions (real API calls): {stats['api_calls']}")
        print(f"  Cache hits (no charge):            {stats['cache_hits']}")
        print(f"  Estimated cost this run:           ${stats['estimated_cost']:.4f}")

    if any_yield_warnings and args.strict:
        print(f"\n--strict: exiting nonzero due to spider yield warning(s) above.")
        sys.exit(2)


if __name__ == "__main__":
    main()
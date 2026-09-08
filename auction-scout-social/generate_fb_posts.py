#!/usr/bin/env python3
"""
generate_fb_posts.py

Selects upcoming real estate auctions from scout-properties.json (or, later,
the auctionscout.db) and generates a SINGLE Facebook post listing all of them.

By default this is a DRY RUN: it prints the post to stdout / writes it to a
file, and makes no network calls. Pass --post to actually publish it to the
AuctionScout Facebook Page via the Graph API.

Because a Facebook post has no built-in expiration, this script keeps a small
local log (fb_post_log.json by default) of every post it publishes, along
with the latest auction date referenced in that post. Run with --cleanup
(as a separate, later invocation -- e.g. a daily scheduled task) to delete
any logged post whose auctions have all already happened.

Selection rule (per user spec):
    - up to 2 properties from CT
    - up to 2 properties from MA
    - up to 1 property from ME
    - up to 1 property from VT
    - up to 1 property from NH
    - only auctions in the future (auction_date >= now)
    - only auctions happening within the next 2 days (auction_date <= now + 2 days)
    - only auctions not in a terminal status (statuses.EXCLUDED_STATUSES --
      e.g. sold, cancelled, withdrawn, or already-past-due)
    - earliest auction_date first within each state

Requires (only when --post or --cleanup is used):
    - FACEBOOK_PAGE_ACCESS_TOKEN env var set to a long-lived Page access token
    - requests (pip install requests)

Usage:
    python3 generate_fb_posts.py
    python3 generate_fb_posts.py --input /path/to/scout-properties.json
    python3 generate_fb_posts.py --out posts.txt
    python3 generate_fb_posts.py --post
    python3 generate_fb_posts.py --cleanup
"""

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # only required when --post or --cleanup is used

# statuses.py lives in the sibling auction-scout-data repo, not here --
# add it to the path rather than keeping a second copy that could drift
# out of sync with the real one.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "auction-scout-data"))

from statuses import EXCLUDED_STATUSES

# State -> max number of properties to pull for this run
STATE_QUOTAS = {
    "RI": 2,
    "CT": 2,
    "MA": 2,
    "ME": 1,
    "VT": 1,
    "NH": 1,
}

# Order states appear in the output
STATE_ORDER = ["MA", "CT", "NH", "VT", "ME", "RI"]

# Only include auctions happening within this many days from "now"
MAX_DAYS_OUT = 5

BASE_MAP_URL = "https://www.oncoord.com/auction-scout/"
STATUS_PAGE_URL = "https://www.oncoord.com/auction-scout/status.html"

# Same Page ID confirmed working in generate_fp_test_post.py
DEFAULT_PAGE_ID = "1207529282449839"
GRAPH_API_VERSION = "v26.0"

# Local record of published posts, used to know what's safe to delete later
DEFAULT_LOG_PATH = Path("fb_post_log.json")


def post_to_facebook(message: str, page_id: str, access_token: str) -> dict:
    """Publish a single text post to the given Facebook Page. Returns the
    parsed JSON response. Raises RuntimeError on a non-2xx response."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required for --post. Install with: pip install requests")

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/feed"
    response = requests.post(
        url,
        params={"access_token": access_token},
        data={"message": message},
    )
    result = response.json()
    if response.status_code != 200:
        raise RuntimeError(f"Facebook API error (HTTP {response.status_code}): {result}")
    return result


def delete_facebook_post(post_id: str, access_token: str) -> dict:
    """Delete a previously published post by its Graph API post id."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required for --cleanup. Install with: pip install requests")

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{post_id}"
    response = requests.delete(url, params={"access_token": access_token})
    result = response.json()
    if response.status_code != 200:
        raise RuntimeError(f"Facebook API error (HTTP {response.status_code}): {result}")
    return result


def load_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_log(log_path: Path, entries: list[dict]) -> None:
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def load_properties(path: Path) -> list[dict]:
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("properties", [])


def parse_auction_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def select_properties(properties: list[dict], now: datetime) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Group eligible properties by state, sorted by soonest auction first,
    then trim each state's list to its quota.

    Returns (by_state, extra_counts) -- extra_counts[state] is how many
    eligible properties in that state didn't make the cut, for the "+N
    more" teaser in the post."""
    cutoff = now + timedelta(days=MAX_DAYS_OUT)
    by_state: dict[str, list[dict]] = {state: [] for state in STATE_QUOTAS}

    for prop in properties:
        state = prop.get("state")
        if state not in STATE_QUOTAS:
            continue
        # Blocklist, not allowlist -- "scheduled" never actually appears in
        # your data (sources use "active", "postponed", etc.). This checks
        # against the real terminal statuses in statuses.py instead of one
        # hardcoded "good" word.
        if prop.get("status") in EXCLUDED_STATUSES:
            continue
        dt = parse_auction_date(prop.get("auction_date"))
        if dt is None or dt < now or dt > cutoff:
            continue
        if dt.date() == now.date():
            continue
        if prop.get("latitude") is None or prop.get("longitude") is None:
            continue
        by_state[state].append(prop)

    extra_counts: dict[str, int] = {}
    for state, props in by_state.items():
        props.sort(key=lambda p: parse_auction_date(p["auction_date"]))
        quota = STATE_QUOTAS[state]
        extra_counts[state] = max(0, len(props) - quota)
        by_state[state] = props[:quota]

    return by_state, extra_counts


def build_map_url(prop: dict) -> str:
    lat = prop["latitude"]
    lng = prop["longitude"]
    return f"{BASE_MAP_URL}?lat={lat}&lng={lng}&zoom=16"


def format_teaser(state: str, extra: int) -> str:
    """One-line call-to-action appended after a state's listings when more
    eligible auctions exist than the quota showed. Links to that state's
    filtered view on the status page, not the generic homepage."""
    noun = "auction" if extra == 1 else "auctions"
    teaser_url = f"{STATUS_PAGE_URL}?states={state}"
    return (
        f"📍 +{extra} more upcoming {state} {noun} on AuctionScout\n"
        f"{teaser_url}"
    )


def format_post(prop: dict) -> str:
    dt = parse_auction_date(prop["auction_date"])
    date_str = dt.strftime("%m/%d")
    # %-I (no leading zero) is Linux/Mac-only and raises ValueError on Windows,
    # so format with a leading zero (%I) and strip it manually instead.
    time_str = dt.strftime("%I:%M %p").lstrip("0")

    address = prop["address"]
    map_url = build_map_url(prop)

    return (
        "🏠 Upcoming Real Estate Auction 🏠\n"
        f"{address}\n"
        f"{date_str} at {time_str}\n"
        f"{map_url}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate (dry-run) Facebook auction posts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Use the default input path, print posts to stdout
  python generate_fb_posts.py

  # Point at a specific properties file
  python generate_fb_posts.py --input ../auction-scout-data/scout-properties.json

  # Also write the posts to a text file
  python generate_fb_posts.py --input ../auction-scout-data/scout-properties.json --out posts.txt

  # Simulate a specific "today" for testing (doesn't affect real auctions)
  python generate_fb_posts.py --as-of 2026-09-02T15:50:44

  # Actually publish the (single, combined) post to Facebook
  python generate_fb_posts.py --post

  # Delete any previously published post whose auctions have all passed
  python generate_fb_posts.py --cleanup
""",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("c:/dev/oncoord-platform/oncoord-frontend/auction-scout/scout-properties.json"),
        help="Path to scout-properties.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to write the generated post to (in addition to stdout)",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Override 'now' for testing, format YYYY-MM-DDTHH:MM:SS",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Actually publish the combined post to Facebook. Without this flag, it's a dry run.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete previously published posts whose auctions have all already passed, then exit.",
    )
    parser.add_argument(
        "--page-id",
        type=str,
        default=DEFAULT_PAGE_ID,
        help=f"Facebook Page ID to post to (default: {DEFAULT_PAGE_ID})",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"Path to the local post-tracking log (default: {DEFAULT_LOG_PATH})",
    )
    args = parser.parse_args()

    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now()

    # --cleanup is a standalone action: delete expired posts and exit.
    if args.cleanup:
        access_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")
        if not access_token:
            raise SystemExit(
                "FACEBOOK_PAGE_ACCESS_TOKEN environment variable is not set. "
                "Set it to your long-lived Page access token before using --cleanup."
            )

        entries = load_log(args.log)
        if not entries:
            print(f"# No posts logged in {args.log} -- nothing to clean up.")
            return

        remaining = []
        deleted = 0
        for entry in entries:
            expires_at = datetime.fromisoformat(entry["expires_at"])
            if expires_at < now:
                try:
                    delete_facebook_post(entry["post_id"], access_token)
                    print(f"# Deleted post {entry['post_id']} (expired {entry['expires_at']})")
                    deleted += 1
                except Exception as e:
                    print(f"# FAILED to delete post {entry['post_id']}: {e} -- keeping it logged for retry")
                    remaining.append(entry)
            else:
                remaining.append(entry)

        save_log(args.log, remaining)
        print(f"# Cleanup done: {deleted} deleted, {len(remaining)} still active.")
        return

    properties = load_properties(args.input)
    by_state, extra_counts = select_properties(properties, now)

    selected_props = [prop for state in STATE_ORDER for prop in by_state.get(state, [])]
    posts = [format_post(prop) for prop in selected_props]

    # Combined post text: each state's listings followed by a "+N more"
    # teaser if there were more eligible auctions than the quota showed.
    # Built separately from `posts` above -- teasers aren't real listings,
    # so they stay out of the log/expiration math further down.
    blocks = []
    for state in STATE_ORDER:
        for prop in by_state.get(state, []):
            blocks.append(format_post(prop))
        extra = extra_counts.get(state, 0)
        if extra:
            blocks.append(format_teaser(state, extra))
    output_text = "\n\n".join(blocks)

    print(f"# Generated {len(posts)} listing(s) as of {now.isoformat()}")
    print(f"# Per-state counts: " + ", ".join(
        f"{s}={len(by_state.get(s, []))}" + (f" (+{extra_counts.get(s, 0)} more)" if extra_counts.get(s, 0) else "")
        for s in STATE_ORDER
    ))
    print()
    print(output_text)

    if args.out:
        args.out.write_text(output_text + "\n", encoding="utf-8")
        print(f"\n# Wrote post to {args.out}", flush=True)

    if not args.post:
        print("\n# Dry run only -- no post was published. Pass --post to publish this to Facebook.")
        return

    if not posts:
        print("\n# Nothing to post.")
        return

    access_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit(
            "FACEBOOK_PAGE_ACCESS_TOKEN environment variable is not set. "
            "Set it to your long-lived Page access token before using --post."
        )

    print(f"\n# Publishing combined post ({len(posts)} listings) to Facebook Page {args.page_id}...")
    try:
        result = post_to_facebook(output_text, page_id=args.page_id, access_token=access_token)
        post_id = result.get("id", "?")
        print(f"# Posted OK -- id={post_id}")

        # The post's "expiration" is the latest auction date among its listings.
        # Add a 1-day grace period so it doesn't disappear mid-auction-day.
        latest_auction = max(parse_auction_date(p["auction_date"]) for p in selected_props)
        expires_at = latest_auction + timedelta(days=1)

        entries = load_log(args.log)
        entries.append({
            "post_id": post_id,
            "posted_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "listing_count": len(posts),
        })
        save_log(args.log, entries)
        print(f"# Logged to {args.log} -- will be eligible for cleanup after {expires_at.isoformat()}")
    except Exception as e:
        print(f"# FAILED: {e}")


if __name__ == "__main__":
    main()
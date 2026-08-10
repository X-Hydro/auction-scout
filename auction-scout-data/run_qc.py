import csv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

STATS_PATH = REPO_ROOT / "run_scout_stats.csv"
STATS_FIELDNAMES = ["date", "spider", "count"]
BASELINE_DROP_THRESHOLD = 0.5  # warn if a run falls below 50% of recent min
BASELINE_LOOKBACK = 10  # how many recent non-zero runs to compare against


def load_recent_counts(name):
    """Row counts from the last BASELINE_LOOKBACK non-zero runs of this
    spider, oldest first. Zero-row runs are excluded on purpose -- if a
    broken run counted toward the baseline, the NEXT broken run would
    look "normal" by comparison and stop warning."""
    if not STATS_PATH.exists():
        return []
    with open(STATS_PATH, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["spider"] == name]
    counts = [int(r["count"]) for r in rows if int(r["count"]) > 0]
    return counts[-BASELINE_LOOKBACK:]


def append_stats(run_date, spider_counts):
    """spider_counts: list of (name, count) tuples for this run. Every
    spider that ran gets a row, including zero-count ones -- the log
    itself should be a complete record even though load_recent_counts()
    ignores the zero rows when computing a baseline."""
    is_new = not STATS_PATH.exists()
    with open(STATS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATS_FIELDNAMES)
        if is_new:
            w.writeheader()
        for name, count in spider_counts:
            w.writerow({"date": run_date, "spider": name, "count": count})


def check_yield(name, count, recent_counts):
    """
    Compare this run's row count against recent history for this spider.
    Returns True if this run looks anomalous and worth a human's attention.

    Zero rows always warns, with or without history -- a spider producing
    NOTHING is never a fine outcome to stay silent about. A sharp drop
    below recent normal is a softer second signal that only fires once
    there's history to compare against (a brand-new spider's first run
    has nothing to be "below", so it's never flagged on that basis alone).
    """
    if count == 0:
        print(f"[{name}] WARNING: 0 rows this run.")
        if recent_counts:
            print(f"[{name}]   recent counts (last {len(recent_counts)} good runs): {recent_counts}")
        else:
            print(f"[{name}]   (no run history yet to compare against)")
        return True
    if recent_counts:
        recent_min = min(recent_counts)
        if recent_min > 0 and count < recent_min * BASELINE_DROP_THRESHOLD:
            print(f"[{name}] WARNING: {count} row(s) this run -- well below "
                  f"recent normal (min of last {len(recent_counts)} runs: {recent_min}).")
            return True
    return False


def _load_all_stats():
    if not STATS_PATH.exists():
        return []
    with open(STATS_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def audit_last_run():
    """
    Standalone entry point (`python run_qc.py`): re-checks the MOST
    RECENT logged run for every spider against that spider's history
    BEFORE it, using the exact same check_yield() logic run-scout.py
    itself uses live. Doesn't scrape anything -- purely reads
    run_scout_stats.csv, so it's a way to ask "did anything look wrong
    last time?" without waiting for (or triggering) another scrape.
    Returns True if any spider's last run looked anomalous.
    """
    rows = _load_all_stats()
    if not rows:
        print(f"No history yet at {STATS_PATH} -- run run-scout.py at "
              f"least once first.")
        return False

    by_spider = {}
    for r in rows:
        by_spider.setdefault(r["spider"], []).append(r)

    any_warned = False
    for name in sorted(by_spider):
        entries = by_spider[name]  # file is append-only -> file order == run order
        last = entries[-1]
        last_count = int(last["count"])
        history_before_last = [int(e["count"]) for e in entries[:-1] if int(e["count"]) > 0]
        history_before_last = history_before_last[-BASELINE_LOOKBACK:]

        print(f"[{name}] last run: {last['date']} -- {last_count} row(s)")
        if check_yield(name, last_count, history_before_last):
            any_warned = True

    if not any_warned:
        print("\nNo anomalies in the most recent run for any spider.")
    return any_warned


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Audit run_scout_stats.csv for the most recent run "
                    "per spider, without re-scraping anything.",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Exit with a nonzero status if any spider's most recent "
             "logged run looked anomalous.",
    )
    args = p.parse_args()

    warned = audit_last_run()
    if warned and args.strict:
        sys.exit(2)
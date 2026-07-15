"""
Simple JSON-file cache, keyed by "{source}:{id}", storing a content hash
+ the last extracted fields. run-scout.py is typically re-run daily while
an auction is still upcoming -- this avoids paying for a fresh AI
extraction on every run for a listing whose detail page hasn't changed.

Not a database, just a flat file -- consistent with how geocode_overrides.csv
is handled elsewhere in this codebase.
"""
import hashlib
import json
from pathlib import Path

DEFAULT_CACHE_PATH = "ai_extract_cache.json"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cache(path=DEFAULT_CACHE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: couldn't read {path} ({e}); starting with an empty cache")
        return {}


def save_cache(cache: dict, path=DEFAULT_CACHE_PATH) -> None:
    Path(path).write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_cached_fields(cache: dict, key: str, text: str) -> dict | None:
    """Returns the cached field dict if this key's stored hash matches the
    current page text, else None (meaning: re-extract)."""
    entry = cache.get(key)
    if entry and entry.get("hash") == _hash(text):
        return entry.get("fields")
    return None


def store_fields(cache: dict, key: str, text: str, fields: dict) -> None:
    cache[key] = {"hash": _hash(text), "fields": fields}
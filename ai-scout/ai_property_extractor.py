"""
Shared AI-based property-spec extractor.

Scope is deliberately narrow: property specs only (property_type,
bedrooms, bathrooms, sqft, lot_size, year_built, extra_fields). Identity
(id/url/street/city_state), dates, and status are NOT touched here --
those are handled well by each site's existing structural parsing
(table cells, ACF field names, banner CSS classes, etc.) and shouldn't be
handed to a model; that code is reliable specifically because it's
deterministic and auditable.

Two call shapes, both feeding the same schema:
  - sullivan.py: text is already a clean "Label: value; Label: value"
    blob assembled from <li> elements -- no HTML-to-text step needed.
  - jjmanning.py / patriot.py: text is free-text prose (a paragraph
    description) where bed/bath/sqft is embedded in a sentence, not a
    labeled field at all -- these sites currently extract NONE of that.

Caching: results are cached by "{source}:{id}" + a hash of the input
text, in a flat JSON file (ai_cache.py) -- same pattern as
geocode_overrides.csv elsewhere in this codebase. Re-running run-scout.py
daily on a listing that hasn't changed doesn't re-bill the API.
"""
from __future__ import annotations

import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

import ai_cache

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
_MODEL = "claude-sonnet-4-6"

_CACHE_PATH = "ai_property_cache.json"
_cache = ai_cache.load_cache(_CACHE_PATH)

# Sonnet 4.6 standard pricing -- kept here (not just in estimate_ai_cost.py)
# so run-scout-ai.py can report real spend for a run without needing that
# separate script. Update both places if pricing changes.
_INPUT_PRICE_PER_MTOK = 3.00
_OUTPUT_PRICE_PER_MTOK = 15.00
_TOOL_USE_OVERHEAD_TOKENS = 497
_CHARS_PER_TOKEN = 4

_stats = {"api_calls": 0, "cache_hits": 0, "estimated_cost": 0.0}


def get_stats() -> dict:
    """Running totals since this process started (or since reset_stats())
    -- call this from run-scout-ai.py after a run to report real spend."""
    return dict(_stats)


def reset_stats() -> None:
    _stats["api_calls"] = 0
    _stats["cache_hits"] = 0
    _stats["estimated_cost"] = 0.0


def _estimate_call_cost(input_text: str, output_tokens: int = 150) -> float:
    fixed_tokens = (
            len(_SYSTEM_PROMPT) // _CHARS_PER_TOKEN
            + len(str(PropertySpecs.model_json_schema())) // _CHARS_PER_TOKEN
            + _TOOL_USE_OVERHEAD_TOKENS
    )
    input_tokens = fixed_tokens + len(input_text) // _CHARS_PER_TOKEN
    return (
            (input_tokens / 1_000_000) * _INPUT_PRICE_PER_MTOK
            + (output_tokens / 1_000_000) * _OUTPUT_PRICE_PER_MTOK
    )


class PropertySpecs(BaseModel):
    property_type: Optional[str] = Field(
        None, description="e.g. Residential, Commercial, Land"
    )
    bedrooms: Optional[int] = Field(
        None, description="Bedroom count for the PRIMARY/main structure only"
    )
    bathrooms: Optional[str] = Field(
        None, description="Bathroom count EXACTLY as described, e.g. '2', "
                          "'1.5', '3 full / 2 half' -- kept as text, not coerced to a single "
                          "number, since some listings report a full/half split that a plain "
                          "int would lose."
    )
    sqft: Optional[int] = Field(
        None, description="Living space square footage for the primary "
                          "structure only, as a plain integer -- no commas, units, or "
                          "+/- markers"
    )
    lot_size: Optional[str] = Field(
        None, description="Lot size exactly as written, including unit and "
                          "any +/- marker, e.g. '6,970± sf' or '2.13+/- Acres'. Do not "
                          "convert units."
    )
    year_built: Optional[int] = None
    extra_fields: Optional[str] = Field(
        None, description="Leftover facts not covered by the fields above, "
                          "as 'Label: value' pairs separated by '; ' -- e.g. mortgage/case "
                          "reference, room count, parcel ID, or a SECOND structure's own "
                          "bed/bath/sqft/year if the lot has more than one building (e.g. "
                          "'In-law home: 2BR/2BA, 1,802sf, built 2016'). Do not restate "
                          "property_type/bedrooms/bathrooms/sqft/lot_size/year_built here."
    )


_SYSTEM_PROMPT = """You extract property specifications from a real \
estate foreclosure/auction listing for a New England auction aggregator. \
You'll be given either a block of labeled fields or a free-text \
description -- both should be read the same way.

Rules:
- Only extract what's actually stated. Leave a field null rather than \
guessing or inferring a plausible value.
- bedrooms/bathrooms/sqft/year_built describe the PRIMARY structure. \
Numbers embedded in prose count the same as labeled fields -- e.g. \
"3,267+/- sf, 4-Bedroom, 3.5-Bath Colonial" should be read as \
sqft=3267, bedrooms=4, bathrooms="3.5" just as reliably as a labeled \
"Bedrooms: 4" field would be.
- If a second structure exists on the lot (in-law apartment, guest \
house, converted barn, etc.) with its own bed/bath/sqft, do NOT merge \
those numbers into the primary fields -- summarize it in extra_fields \
instead.
- lot_size should be copied close to verbatim, preserving unit and any \
+/- notation.
- extra_fields is a compact catch-all, not a transcript: only genuinely \
distinct facts as short 'Label: value' pairs joined with '; '.
"""


def extract_property_specs(
        text: str, source_site: str, cache_key: str | None = None
) -> PropertySpecs:
    if not text or not text.strip():
        return PropertySpecs()

    if cache_key:
        cached = ai_cache.get_cached_fields(_cache, cache_key, text)
        if cached is not None:
            _stats["cache_hits"] += 1
            return PropertySpecs(**cached)

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[
            {
                "name": "record_property_specs",
                "description": "Record extracted property specifications",
                "input_schema": PropertySpecs.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": "record_property_specs"},
        messages=[
            {"role": "user", "content": f"Source: {source_site}\n\n{text}"}
        ],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    specs = PropertySpecs(**tool_use.input)

    _stats["api_calls"] += 1
    _stats["estimated_cost"] += _estimate_call_cost(text)

    if cache_key:
        ai_cache.store_fields(_cache, cache_key, text, specs.model_dump())
        ai_cache.save_cache(_cache, _CACHE_PATH)

    return specs
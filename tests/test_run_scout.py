"""
Tests for run-scout.py's extract_state() and format_row() helpers.

run-scout.py isn't a normal importable module (hyphenated filename), so
it's loaded via importlib rather than a plain `import`. Loading it pulls in
every registered spider, since run-scout.py imports them all at module
level -- that's expected and matches what happens when the script is
actually run; it also means this test file needs the same sys.path setup
as the other spider tests (see conftest.py).
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
RUN_SCOUT_PATH = ROOT / "run-scout.py"


def _load_run_scout():
    spec = importlib.util.spec_from_file_location("run_scout", RUN_SCOUT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_scout():
    return _load_run_scout()


# ---- extract_state -------------------------------------------------

class TestExtractState:
    @pytest.mark.parametrize("city_state,expected", [
        # The exact production bug: zip glued onto state with a space,
        # no comma -- must NOT come back as "MA 01880".
        ("Wakefield, MA 01880", "MA"),
        ("Medford, MA 02155", "MA"),
        ("Rochester, MA 02770", "MA"),
        # Parenthetical city names shouldn't confuse the state match.
        ("Hyde Park (Boston), MA 02136", "MA"),
        # No zip at all -- the one case that already worked before the fix.
        ("Springfield, MA", "MA"),
        # Other states, and a ZIP+4, for good measure.
        ("Providence, RI 02903", "RI"),
        ("Burlington, VT 05401-1234", "VT"),
    ])
    def test_extracts_state_ignoring_glued_zip(self, run_scout, city_state, expected):
        assert run_scout.extract_state(city_state) == expected

    def test_no_comma_returns_empty_string(self, run_scout):
        assert run_scout.extract_state("no comma here") == ""

    def test_empty_string_returns_empty_string(self, run_scout):
        assert run_scout.extract_state("") == ""

    def test_falls_back_to_raw_segment_when_pattern_does_not_match(self, run_scout):
        # If a future site's last segment doesn't look like "ST" or "ST
        # 01234", we should get the raw text back, not None/empty -- silent
        # data loss is worse than an obviously-wrong value someone will
        # notice in the CSV.
        assert run_scout.extract_state("Somewhere, Unexpected Format") == "Unexpected Format"


# ---- format_row (end-to-end for the State field) -----------------------

class TestFormatRowState:
    """The actual production bug was in format_row()'s own comma-split,
    not in extract_state() -- so cover format_row() directly too, not just
    the helper in isolation."""

    @staticmethod
    def _row(city_state, **overrides):
        row = {
            "id": "1", "source": "brockscott", "street": "1 Test St",
            "city_state": city_state, "latitude": 42.0, "longitude": -71.0,
            "date_time": "07/27/2026 - 02:00:00 PM", "status": "active",
            "url": "https://example.com",
        }
        row.update(overrides)
        return row

    def test_state_column_excludes_zip(self, run_scout):
        row = self._row("Wakefield, MA 01880")
        assert run_scout.format_row(row)["State"] == "MA"

    def test_state_column_correct_when_no_zip_present(self, run_scout):
        row = self._row("Springfield, MA")
        assert run_scout.format_row(row)["State"] == "MA"

    def test_county_still_passes_through_untouched(self, run_scout):
        # Not what broke this time, but format_row is the single place
        # both bugs lived -- cheap to guard the sibling field too.
        row = self._row("Wakefield, MA 01880", county="Middlesex")
        assert run_scout.format_row(row)["County"] == "Middlesex"
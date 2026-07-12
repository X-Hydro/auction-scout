import load_csv


class TestParseDescription:
    def test_extracts_all_fields(self, sample_row):
        result = load_csv.parse_description(sample_row["Description"])
        assert result["property_type"] == "Residential"
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 2.0
        assert result["sqft"] == 4268
        assert result["year_built"] == 1981
        # county is NOT produced here by design -- see the FIELD_PATTERNS
        # comment in load_csv.py. It's sourced from the CSV's own County
        # column (reverse-geocoded), never re-parsed out of free text.
        assert "county" not in result
        assert "Norfolk Cty." in result["mortgage_ref"]


class TestResolvePropertyDetails:
    def test_prefers_explicit_columns(self, sample_row_with_columns):
        """When dedicated columns are populated, they should be used --
        this is the current, real shape spiders like sullivan.py emit."""
        parsed_desc = load_csv.parse_description(sample_row_with_columns["Description"])
        result = load_csv.resolve_property_details(sample_row_with_columns, parsed_desc)

        assert result["property_type"] == "Residential"
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 2.0
        assert result["sqft"] == 4268
        assert result["lot_size_raw"] == "4.32 acres"
        assert result["year_built"] == 1981
        # Description here only has genuine leftovers -- mortgage_ref
        # still correctly comes through the Description-parsing fallback,
        # since there's no dedicated "Mortgage Ref" column at all.
        assert "Norfolk Cty." in result["mortgage_ref"]

    def test_column_wins_over_conflicting_description(self, row_with_conflicting_description):
        """The strong version of the above: columns and Description
        actively DISAGREE here (Bedrooms column=4, Description text
        says 5; Property Type column=Residential, Description says
        Land). If resolve_property_details() ever regressed to
        preferring Description, this is what would catch it --
        test_prefers_explicit_columns alone couldn't, since its
        Description has nothing to conflict with."""
        parsed_desc = load_csv.parse_description(row_with_conflicting_description["Description"])
        # sanity check the fixture itself is set up as intended --
        # confirms Description really would parse to different values
        assert parsed_desc["bedrooms"] == 5
        assert parsed_desc["property_type"] == "Land"

        result = load_csv.resolve_property_details(row_with_conflicting_description, parsed_desc)

        assert result["bedrooms"] == 4  # column, not Description's 5
        assert result["property_type"] == "Residential"  # column, not Description's Land
        assert result["sqft"] == 4268  # column, not Description's 9999
        assert result["year_built"] == 1981  # column, not Description's 1900

    def test_falls_back_to_description_when_column_blank(self, sample_row):
        """A source with no dedicated columns at all (sample_row's legacy
        shape) should still get correct values via the Description
        fallback -- this is resolve_property_details() applied to the
        OTHER real scenario it needs to handle."""
        parsed_desc = load_csv.parse_description(sample_row["Description"])
        result = load_csv.resolve_property_details(sample_row, parsed_desc)

        assert result["property_type"] == "Residential"
        assert result["bedrooms"] == 4
        assert result["sqft"] == 4268
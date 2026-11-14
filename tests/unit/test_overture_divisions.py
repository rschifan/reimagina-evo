"""Selection rules for Overture administrative divisions.

The rule under test exists because of a specific silent failure. The
Mexicali bbox returns 16 divisions spanning two countries, and
``US · Imperial County`` and ``MX · Municipio de Mexicali`` are *both*
``subtype='county'``. Code that picked the first matching county would
load the wrong side of the international border, and the failure would
leave no trace: a valid polygon, a successful AOI, a successful load, and
a map of the wrong country.

So the matcher is a pure function, separated from the S3 read, and the
border case has a test rather than a comment.
"""

from __future__ import annotations

import pytest

from reimagina.pipeline.overture import (
    DIVISION_SUBTYPE_COUNTY,
    MEXICALI_BBOX,
    MEXICALI_DIVISION_NAME,
    MEXICO_CITY_BBOX,
    OVERTURE_DEFAULT_RELEASE,
)
from reimagina.pipeline.overture_divisions import (
    DIVISIONS_AREA_TYPE,
    DIVISIONS_THEME,
    DivisionCandidate,
    _primary_name,
    matches,
)


def _candidate(
    overture_id: str,
    *,
    subtype: str | None = "county",
    country: str | None = "MX",
    name: str | None = None,
    region: str | None = None,
) -> DivisionCandidate:
    return DivisionCandidate(
        overture_id=overture_id,
        subtype=subtype,
        division_class="land",
        country=country,
        region=region,
        name=name,
    )


# The real contents of the Mexicali bbox in release 2026-10-21.0, as
# `overture divisions` printed them. Kept as a fixture rather than fetched,
# so the selection rule is tested without a network call and without a test
# that breaks when Overture rotates a release.
MEXICALI_BBOX_DIVISIONS = [
    _candidate("mx-country-1", subtype="country", name="México"),
    _candidate("mx-country-2", subtype="country", name="México"),
    _candidate("mx-mexicali", subtype="county", name="Municipio de Mexicali"),
    _candidate(
        "mx-parajes", subtype="neighborhood", name="Parajes de Puebla Segunda Sección"
    ),
    _candidate("mx-bcn", subtype="region", name="Baja California", region="MX-BCN"),
    _candidate("us-imperial", subtype="county", country="US", name="Imperial County"),
    _candidate("us-elcentro", subtype="locality", country="US", name="El Centro"),
    _candidate(
        "us-losalamos",
        subtype="neighborhood",
        country="US",
        name="Los Alamos International Center",
    ),
    _candidate("mx-plaza", subtype="microhood", name="Plaza"),
    _candidate("us-calexico", subtype="locality", country="US", name="Calexico"),
    _candidate("mx-uabc", subtype="microhood", name="Explanada UABC"),
    _candidate("mx-islas", subtype="locality", name="Islas Agrarias - Grupo A"),
    _candidate("us-ca-land", subtype="region", country="US", name="California"),
    _candidate("us-ca-sea", subtype="region", country="US", name="California"),
    _candidate("us-country-1", subtype="country", country="US", name="United States"),
    _candidate("us-country-2", subtype="country", country="US", name="United States"),
]


class TestBorderSafety:
    """⚠ The tests that fail if the wrong country can ever be selected."""

    def test_subtype_alone_is_ambiguous_across_the_border(self):
        # This is the precondition for every other test here: if subtype were
        # sufficient, none of the rest would be needed.
        counties = matches(
            MEXICALI_BBOX_DIVISIONS, subtype="county", country=None, name=None
        )
        assert len(counties) == 2
        assert {c.country for c in counties} == {"MX", "US"}

    def test_country_narrows_to_one_side(self):
        got = matches(
            MEXICALI_BBOX_DIVISIONS, subtype="county", country="MX", name=None
        )
        assert [c.overture_id for c in got] == ["mx-mexicali"]

    def test_mexicali_criteria_never_select_imperial_county(self):
        got = matches(
            MEXICALI_BBOX_DIVISIONS,
            subtype=DIVISION_SUBTYPE_COUNTY,
            country="MX",
            name=MEXICALI_DIVISION_NAME,
        )
        assert [c.overture_id for c in got] == ["mx-mexicali"]
        assert all(c.country == "MX" for c in got)

    def test_us_criteria_never_select_mexicali(self):
        # The mirror image. A matcher that ignored country would pass the
        # test above by accident and fail this one.
        got = matches(
            MEXICALI_BBOX_DIVISIONS,
            subtype="county",
            country="US",
            name="Imperial County",
        )
        assert [c.overture_id for c in got] == ["us-imperial"]


class TestMatching:
    def test_none_criteria_mean_dont_care(self):
        got = matches(MEXICALI_BBOX_DIVISIONS, subtype=None, country=None, name=None)
        assert len(got) == len(MEXICALI_BBOX_DIVISIONS)

    def test_name_match_is_case_and_whitespace_insensitive(self):
        # Overture's names.primary is upstream text. Failing an AOI build over
        # a trailing space would be a bad trade.
        for variant in (
            "Municipio de Mexicali",
            "municipio de mexicali",
            "  Municipio de Mexicali  ",
            "MUNICIPIO DE MEXICALI",
        ):
            got = matches(
                MEXICALI_BBOX_DIVISIONS, subtype=None, country=None, name=variant
            )
            assert [c.overture_id for c in got] == ["mx-mexicali"], variant

    def test_country_match_is_case_insensitive(self):
        got = matches(
            MEXICALI_BBOX_DIVISIONS, subtype="county", country="mx", name=None
        )
        assert [c.overture_id for c in got] == ["mx-mexicali"]

    def test_a_name_that_does_not_exist_matches_nothing(self):
        # Not an exception: find_division returns the candidates so the caller
        # can print what the release actually holds.
        assert (
            matches(MEXICALI_BBOX_DIVISIONS, subtype=None, country=None, name="Tijuana")
            == []
        )

    def test_missing_fields_do_not_match_a_supplied_criterion(self):
        blank = [_candidate("blank", subtype=None, country=None, name=None)]
        assert matches(blank, subtype="county", country=None, name=None) == []
        assert matches(blank, subtype=None, country="MX", name=None) == []
        assert matches(blank, subtype=None, country=None, name="x") == []
        # …but a blank candidate still survives "don't care" on all three.
        assert len(matches(blank, subtype=None, country=None, name=None)) == 1

    def test_duplicate_names_in_different_classes_both_match(self):
        # `California` appears twice — land and maritime. The matcher returns
        # both; find_division raises rather than picking one, because two
        # matches means the criteria do not identify a place.
        got = matches(
            MEXICALI_BBOX_DIVISIONS, subtype="region", country="US", name="California"
        )
        assert len(got) == 2


class TestPrimaryName:
    def test_reads_the_primary_field_of_the_names_struct(self):
        assert _primary_name({"primary": "Municipio de Mexicali"}) == (
            "Municipio de Mexicali"
        )

    def test_tolerates_a_flattened_string(self):
        # The struct's shape is upstream and has changed before. A release
        # that flattens it should degrade to a working match, not to a
        # TypeError inside a filter.
        assert _primary_name("Mexicali") == "Mexicali"

    def test_missing_and_null_names_are_none_not_an_error(self):
        assert _primary_name(None) is None
        assert _primary_name({}) is None
        assert _primary_name({"common": {"es": "Mexicali"}}) is None
        assert _primary_name(42) is None


class TestConstants:
    def test_pinned_release_looks_like_an_overture_release(self):
        # Not a check that it exists on S3 — that is `data verify-sources`'
        # job, and a unit test that fails when Overture rotates a release
        # fails for a reason unrelated to the code under it.
        assert OVERTURE_DEFAULT_RELEASE.count("-") == 2
        year, month, rest = OVERTURE_DEFAULT_RELEASE.split("-")
        day, revision = rest.split(".")
        assert 2024 <= int(year) <= 2100
        assert 1 <= int(month) <= 12
        assert 1 <= int(day) <= 31
        assert int(revision) >= 0

    def test_pin_is_not_one_of_the_releases_known_to_be_rotated_away(self):
        # The three the module docstring records as having stopped resolving.
        # A revert to any of them breaks every extract, silently until fetch.
        assert OVERTURE_DEFAULT_RELEASE not in {
            "2026-02-18.0",
            "2026-04-15.0",
            "2026-07-22.0",
        }

    def test_mexicali_bbox_is_ordered_and_contains_the_city(self):
        xmin, ymin, xmax, ymax = MEXICALI_BBOX
        assert xmin < xmax and ymin < ymax
        # Mexicali's city centre, ~Centro Cívico.
        assert xmin < -115.48 < xmax
        assert ymin < 32.63 < ymax

    def test_mexicali_bbox_crosses_the_border_on_purpose(self):
        # Documented in the constant's own comment: the extract is a superset
        # and `_clip_buildings_to_aoi` removes what falls outside the Mexican
        # polygon. A box that stopped at the border would risk clipping
        # Mexicali itself. The border runs at roughly 32.66 here.
        _, _, _, ymax = MEXICALI_BBOX
        assert ymax > 32.67

    def test_mexicali_and_mexico_city_boxes_are_disjoint(self):
        # Pasting one into the other's command is the obvious mistake; they
        # are 1,600 km apart and must not silently overlap.
        a_xmin, _, a_xmax, _ = MEXICALI_BBOX
        b_xmin, _, b_xmax, _ = MEXICO_CITY_BBOX
        assert a_xmax < b_xmin or b_xmax < a_xmin

    def test_division_theme_names_match_overtures_schema(self):
        # `division` is points and `division_boundary` is the shared border
        # lines; neither is a usable AOI geometry.
        assert DIVISIONS_THEME == "divisions"
        assert DIVISIONS_AREA_TYPE == "division_area"

    def test_county_is_the_subtype_for_a_mexican_municipio(self):
        assert DIVISION_SUBTYPE_COUNTY == "county"


@pytest.mark.parametrize(
    "criteria",
    [
        {"subtype": "county", "country": "MX", "name": MEXICALI_DIVISION_NAME},
        {"subtype": None, "country": "MX", "name": MEXICALI_DIVISION_NAME},
        {"subtype": "county", "country": None, "name": MEXICALI_DIVISION_NAME},
    ],
)
def test_any_criteria_set_including_the_name_is_unambiguous(criteria):
    got = matches(MEXICALI_BBOX_DIVISIONS, **criteria)
    assert [c.overture_id for c in got] == ["mx-mexicali"]


class TestSilverNaming:
    """The Bronze/Silver names must not disagree about which city they hold.

    Found by running a real Mexicali load: Bronze landed
    ``overture_buildings_mexicali.parquet`` (it takes its name from the raw
    file) and Silver wrote ``overture_buildings_mexico_city_silver.parquet``
    beside it, because the default was the Mexico City module constant. Both
    paths are returned in the load result and the Bronze one reaches
    ``data_source_catalog.storage_uri``, so the wrong name propagates into
    the provenance of the layer whose purpose is traceability.
    """

    def test_silver_name_follows_the_bronze_artifact(self):
        from reimagina.pipeline.overture import silver_filename_for

        assert (
            silver_filename_for("/b/src/overture_buildings_mexicali.parquet")
            == "overture_buildings_mexicali_silver.parquet"
        )

    def test_the_cdmx_name_is_unchanged(self):
        # Backward compatibility: the pre-existing CDMX artifact must keep
        # the exact name the fixtures and the inventory doc already record.
        from reimagina.pipeline.overture import (
            OVERTURE_BUILDINGS_FILENAME,
            OVERTURE_BUILDINGS_SILVER_FILENAME,
            silver_filename_for,
        )

        assert (
            silver_filename_for(OVERTURE_BUILDINGS_FILENAME)
            == OVERTURE_BUILDINGS_SILVER_FILENAME
        )

    def test_two_cities_never_collide(self):
        from reimagina.pipeline.overture import silver_filename_for

        assert silver_filename_for("overture_buildings_mexicali.parquet") != (
            silver_filename_for("overture_buildings_mexico_city.parquet")
        )

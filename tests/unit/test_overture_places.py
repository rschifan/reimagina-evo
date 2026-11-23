"""Pure-logic tests for the Overture places ingest.

The schema guard is the important one. Overture renamed this theme's category
field — ``categories.primary`` became ``basic_category`` — and reading the old
path does not raise: it yields ``None`` for every row, so the category filter
matches nothing and the ingest loads zero places and reports success. That is
not a hypothetical. It happened while this module was being written, and
produced a confident "0 park destinations in Mexicali" that was wrong.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
import shapely

from reimagina.pipeline import overture_places as op


class TestSchemaGuard:
    def test_accepts_the_current_schema(self):
        op._require_expected_columns(
            ["id", "basic_category", "names", "geometry", "bbox", "confidence"]
        )

    def test_rejects_the_old_categories_schema(self):
        """A release carrying the pre-rename struct must fail loudly."""
        with pytest.raises(op.OverturePlacesSchemaChanged) as exc:
            op._require_expected_columns(["id", "categories", "names", "geometry"])
        assert "basic_category" in str(exc.value)

    def test_names_what_is_missing_and_what_is_there(self):
        """The remedy is to look at the real schema, so the error carries it."""
        with pytest.raises(op.OverturePlacesSchemaChanged) as exc:
            op._require_expected_columns(["id", "names"])
        message = str(exc.value)
        assert "basic_category" in message
        assert "geometry" in message
        assert "names" in message  # the 'Present:' list

    def test_error_explains_the_silent_failure_mode(self):
        """Without this sentence the reader fixes the column name and never
        learns that the old path fails by loading nothing."""
        with pytest.raises(op.OverturePlacesSchemaChanged) as exc:
            op._require_expected_columns([])
        assert "zero places" in str(exc.value)


class TestFirstCountry:
    def test_reads_the_first_address(self):
        assert op._first_country({"addresses": [{"country": "MX"}]}) == "MX"

    def test_missing_addresses_is_none(self):
        assert op._first_country({}) is None
        assert op._first_country({"addresses": None}) is None
        assert op._first_country({"addresses": []}) is None

    def test_address_without_country_is_none(self):
        assert op._first_country({"addresses": [{"locality": "Mexicali"}]}) is None


def _gdf(rows: list[tuple[str, str]]) -> gpd.GeoDataFrame:
    """A minimal places frame: (name, category) at distinct points."""
    return gpd.GeoDataFrame(
        {
            "id": [f"id{i}" for i in range(len(rows))],
            "name": [name for name, _ in rows],
            "category": [cat for _, cat in rows],
            "confidence": [0.9] * len(rows),
            "country": ["MX"] * len(rows),
        },
        geometry=[shapely.Point(-115.4 + i * 0.001, 32.6) for i in range(len(rows))],
        crs="EPSG:4326",
    )


class TestDropExcludedByName:
    def test_drops_industrial_estates_and_keeps_parks(self):
        gdf = _gdf(
            [
                ("Parque Industrial el Dorado", "park"),
                ("Ejido Hidalgo Park", "park"),
                ("Parque Acuatico Chorilandia", "park"),
            ]
        )
        kept = op._drop_excluded_by_name(gdf)
        assert sorted(kept["name"]) == [
            "Ejido Hidalgo Park",
            "Parque Acuatico Chorilandia",
        ]

    def test_drops_event_venues_whatever_category_they_carry(self):
        """Upstream tags these as park, amusement_park and swimming_pool —
        so the rule has to be on the name, not the category."""
        gdf = _gdf(
            [
                ("Jardin de Eventos Ximena", "swimming_pool"),
                ("Jardín de eventos Verdugo", "amusement_park"),
                ("Jardín de Eventos GR", "sport_or_fitness_facility"),
                ("Alberca Municipal", "swimming_pool"),
            ]
        )
        kept = op._drop_excluded_by_name(gdf)
        assert list(kept["name"]) == ["Alberca Municipal"]

    def test_keeps_everything_when_nothing_matches(self):
        gdf = _gdf([("Parque Vicente Guerrero", "park"), ("Gimnasio UABC", "gym")])
        assert len(op._drop_excluded_by_name(gdf)) == 2

    def test_returns_a_geodataframe_with_geometry_intact(self):
        gdf = _gdf([("Parque Industrial X", "park"), ("Parque Real", "park")])
        kept = op._drop_excluded_by_name(gdf)
        assert isinstance(kept, gpd.GeoDataFrame)
        assert kept.crs == gdf.crs
        assert kept.geometry.notna().all()

    def test_logs_the_names_it_drops(self, caplog):
        """A count alone is unauditable; the names are what make a rule that
        starts eating real parks visible."""
        gdf = _gdf([("Parque Industrial Nicoya", "park"), ("Parque Real", "park")])
        with caplog.at_level("INFO", logger=op.logger.name):
            op._drop_excluded_by_name(gdf)
        assert "Parque Industrial Nicoya" in caplog.text
        assert "industrial" in caplog.text.lower()


class TestToGeodataframe:
    class _FakeTable:
        def __init__(self, rows):
            self._rows = rows

        def to_pylist(self):
            return self._rows

    def test_flattens_names_and_decodes_wkb(self):
        point = shapely.Point(-115.45, 32.65)
        table = self._FakeTable(
            [
                {
                    "id": "08f...",
                    "basic_category": "park",
                    "names": {"primary": "Parque Vicente Guerrero"},
                    "confidence": 0.87,
                    "addresses": [{"country": "MX"}],
                    "geometry": shapely.to_wkb(point),
                }
            ]
        )
        gdf = op._to_geodataframe(table)
        assert gdf.loc[0, "name"] == "Parque Vicente Guerrero"
        assert gdf.loc[0, "category"] == "park"
        assert gdf.loc[0, "country"] == "MX"
        assert gdf.geometry.iloc[0].equals(point)
        assert gdf.crs == "EPSG:4326"

    def test_unnamed_place_falls_back_to_its_category(self):
        """An empty label renders as a blank popup, which reads as missing
        data; an unnamed park is still a park."""
        table = self._FakeTable(
            [
                {
                    "id": "x",
                    "basic_category": "playground",
                    "names": None,
                    "confidence": 0.5,
                    "addresses": [],
                    "geometry": shapely.to_wkb(shapely.Point(0, 0)),
                }
            ]
        )
        gdf = op._to_geodataframe(table)
        assert gdf.loc[0, "name"] == "playground"
        assert gdf.loc[0, "country"] is None


# ── lineage: `storage_uri` names the release, not our copy ──────────────────


def test_places_record_the_release_they_were_cut_from(tmp_path, monkeypatch):
    """`data_source_catalog.storage_uri` is lineage, so it names the release.

    The same rule #95 settled for buildings, cdmx and raster, applied to the
    one Overture theme it did not reach: places registered a `pending://`
    placeholder and then overwrote it with the local bronze path once the
    subset landed, so the catalog recorded where our copy sat and nothing
    about where the places came from. The bronze copy needs no column — it
    lives at `data/bronze/{source_id}/` by construction.
    """
    seen: dict[str, object] = {}
    overwrites: list[str] = []

    class FakeConn:
        def commit(self):
            pass

    def _register(conn, **kwargs):
        seen.update(kwargs)
        return "source-1"

    # Stubbed at the catalog, not at `ensure_places_source`: the URI is derived
    # inside it from the release, so the value to check is the one written.
    monkeypatch.setattr(op.feat, "seed_feature_types", lambda conn: None)
    monkeypatch.setattr(op.cat, "get_source", lambda conn, **kwargs: None)
    monkeypatch.setattr(op.cat, "register_source", _register)
    monkeypatch.setattr(
        op, "extract_places_subset", lambda **kwargs: tmp_path / "raw.parquet"
    )
    monkeypatch.setattr(
        op,
        "stage_places_subset",
        lambda raw_path, **kwargs: (
            tmp_path / "bronze.parquet",
            tmp_path / "silver.parquet",
        ),
    )
    monkeypatch.setattr(
        op.cat,
        "update_source_storage_uri",
        lambda conn, source_id, storage_uri: overwrites.append(storage_uri),
    )
    monkeypatch.setattr(op, "load_places_subset", lambda conn, **kwargs: {})
    monkeypatch.setattr(op, "summarize_places_layer", lambda conn, **kwargs: {})

    op.bootstrap_places(
        FakeConn(),
        bbox=(-115.6, 32.5, -115.3, 32.7),
        orchestrator_run_id="orch-1",
        release="2026-10-22.0",
    )

    recorded = str(seen.get("storage_uri"))
    assert recorded.startswith("s3://overturemaps-us-west-2/release/2026-10-22.0/")
    assert "theme=places/type=place" in recorded
    assert not overwrites, "the lineage must not be replaced with a local path"


def test_a_places_row_registered_under_the_old_convention_moves_on_reingest(
    monkeypatch,
):
    """An existing row that still holds a bronze path or a placeholder is
    corrected on the next ingest, as `ensure_overture_buildings_source` does."""
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        op.cat,
        "get_source",
        lambda conn, **kwargs: {
            "id": "src-9",
            "storage_uri": "pending://overture/places",
        },
    )
    monkeypatch.setattr(
        op.cat,
        "update_source_storage_uri",
        lambda conn, source_id, storage_uri: updates.append((source_id, storage_uri)),
    )

    source_id = op.ensure_places_source(object(), version="2026-10-22.0")

    assert source_id == "src-9"
    assert updates == [
        (
            "src-9",
            "s3://overturemaps-us-west-2/release/2026-10-22.0/"
            "theme=places/type=place/*.parquet",
        )
    ]


def test_an_existing_extract_loads_without_touching_s3(tmp_path, monkeypatch):
    """`raw_path` is the offline route: the source bundle ships the extract.

    Given one, nothing is extracted; the file itself is what gets staged.
    """
    staged: list[object] = []

    class FakeConn:
        def commit(self):
            pass

    def _no_extract(**kwargs):
        raise AssertionError("extract_places_subset must not run")

    monkeypatch.setattr(op.feat, "seed_feature_types", lambda conn: None)
    monkeypatch.setattr(op, "ensure_places_source", lambda conn, **kwargs: "s-1")
    monkeypatch.setattr(op, "extract_places_subset", _no_extract)
    monkeypatch.setattr(
        op,
        "stage_places_subset",
        lambda raw_path, **kwargs: (staged.append(raw_path), (raw_path, raw_path))[1],
    )
    monkeypatch.setattr(op, "load_places_subset", lambda conn, **kwargs: {})
    monkeypatch.setattr(op, "summarize_places_layer", lambda conn, **kwargs: {})

    shipped = tmp_path / "places_2026-10-21.0.parquet"
    op.bootstrap_places(
        FakeConn(),
        bbox=(-115.6, 32.5, -115.3, 32.7),
        orchestrator_run_id="orch-1",
        raw_path=shipped,
    )
    assert staged == [shipped]

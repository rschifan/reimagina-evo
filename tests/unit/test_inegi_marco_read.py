"""Unit tests for reimagina.pipeline.inegi_marco WKB decode + partition filtering.

DB-free: builds a tiny hive-partitioned parquet on disk that mirrors the
INEGI marco_geostadistico shape (CVE_ENT partition + WKB BLOB geometry
column) and asserts the reader decodes geometries and respects the filter.
"""
from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely.wkb
from shapely.geometry import Polygon, box

from reimagina.pipeline.inegi_marco import (
    INEGI_MARCO_SOURCE_CRS,
    LAYER_SPECS,
    DEFAULT_LOAD_ORDER,
    read_layer_to_geodataframe,
)


def _write_partition(parquet_dir: Path, *, cve_ent: str, rows: list[dict]) -> None:
    """Mirror the on-disk shape we get from the data lake.

    Each row has: CVEGEO (str), NOMGEO (str), CVE_MUN (int32), geometry (WKB blob).
    Files live under <parquet_dir>/CVE_ENT=<value>/part-0.parquet.
    """
    part_dir = parquet_dir / f"CVE_ENT={cve_ent}"
    part_dir.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "CVEGEO": pa.array([r["CVEGEO"] for r in rows], type=pa.string()),
        "NOMGEO": pa.array([r["NOMGEO"] for r in rows], type=pa.string()),
        "CVE_MUN": pa.array([r["CVE_MUN"] for r in rows], type=pa.int32()),
        "geometry": pa.array([shapely.wkb.dumps(r["geom"]) for r in rows], type=pa.binary()),
    })
    pq.write_table(table, part_dir / "part-0.parquet")


@pytest.fixture
def fake_municipalities(tmp_path: Path) -> Path:
    parquet_dir = tmp_path / "areas_municipales.parquet"
    _write_partition(parquet_dir, cve_ent="02", rows=[
        {"CVEGEO": "02002", "NOMGEO": "Mexicali", "CVE_MUN": 2,
         "geom": box(-115.5, 32.0, -115.0, 32.5)},
    ])
    _write_partition(parquet_dir, cve_ent="09", rows=[
        {"CVEGEO": "09015", "NOMGEO": "Cuauhtémoc", "CVE_MUN": 15,
         "geom": box(-99.2, 19.4, -99.1, 19.5)},
        {"CVEGEO": "09012", "NOMGEO": "Tlalpan", "CVE_MUN": 12,
         "geom": box(-99.3, 19.2, -99.0, 19.4)},
    ])
    _write_partition(parquet_dir, cve_ent="19", rows=[
        {"CVEGEO": "19039", "NOMGEO": "Monterrey", "CVE_MUN": 39,
         "geom": box(-100.4, 25.6, -100.2, 25.8)},
    ])
    return parquet_dir


def test_read_filtered_to_pilot_states(fake_municipalities: Path) -> None:
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=["02", "09"])

    assert len(gdf) == 3
    assert sorted(gdf["NOMGEO"].tolist()) == ["Cuauhtémoc", "Mexicali", "Tlalpan"]
    # Monterrey lives in CVE_ENT=19 and must be excluded.
    assert "Monterrey" not in gdf["NOMGEO"].tolist()


def test_read_decodes_wkb_into_shapely(fake_municipalities: Path) -> None:
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=["02"])

    assert len(gdf) == 1
    geom = gdf.geometry.iloc[0]
    # If decode worked, this is a real shapely geometry, not a bytes blob.
    assert isinstance(geom, Polygon)
    assert pytest.approx(geom.bounds, rel=1e-9) == (-115.5, 32.0, -115.0, 32.5)


def test_read_sets_source_crs(fake_municipalities: Path) -> None:
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=["09"])

    # Source CRS is EPSG:4326 (verified from real data sampling).
    # silver.clean_vector reprojects later if it differs.
    assert str(gdf.crs).upper().endswith("4326")
    assert INEGI_MARCO_SOURCE_CRS == "EPSG:4326"


def test_read_with_no_filter_returns_everything(fake_municipalities: Path) -> None:
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=None)

    # 1 + 2 + 1 across the three partitions.
    assert len(gdf) == 4


def test_read_with_int_filter_coerces_to_string(fake_municipalities: Path) -> None:
    # Hive partition values are strings on disk; the reader must coerce
    # incoming ints so callers can pass either ['02','09'] or [2, 9].
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=[2, 9])

    assert len(gdf) == 3


def test_read_returns_empty_geodataframe_when_filter_misses(fake_municipalities: Path) -> None:
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=["99"])

    assert gdf.empty
    # Empty result still has the source CRS set so downstream cleaners don't error.
    assert gdf.crs is not None


def test_layer_specs_match_default_load_order() -> None:
    # If anyone adds a new layer to LAYER_SPECS without listing it in the
    # load order, that's almost always a bug — bootstrap() would silently
    # skip the new layer.
    assert set(LAYER_SPECS.keys()) == set(DEFAULT_LOAD_ORDER)


def test_layer_specs_cover_the_four_grain_levels() -> None:
    slugs = {spec.feature_type_slug for spec in LAYER_SPECS.values()}
    assert slugs == {"state", "municipality", "ageb", "block"}


# ─── Two-level partitioning regression ────────────────────────────────


def _write_two_level_partition(
    parquet_dir: Path, *, cve_ent: str, cve_mun: str, rows: list[dict],
) -> None:
    """Mirror the real data lake's nested partition shape:
    ``<root>/CVE_ENT=09/CVE_MUN=015/part-0.parquet`` with no CVE_MUN
    column inside the parquet files (it's a partition value)."""
    part_dir = parquet_dir / f"CVE_ENT={cve_ent}" / f"CVE_MUN={cve_mun}"
    part_dir.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "CVEGEO": pa.array([r["CVEGEO"] for r in rows], type=pa.string()),
        "NOMGEO": pa.array([r["NOMGEO"] for r in rows], type=pa.string()),
        "geometry": pa.array(
            [shapely.wkb.dumps(r["geom"]) for r in rows], type=pa.binary(),
        ),
    })
    pq.write_table(table, part_dir / "part-0.parquet")


@pytest.fixture
def fake_two_level_municipalities(tmp_path: Path) -> Path:
    """Real-shape mirror of the data lake's areas_municipales.parquet.

    Three munis in two states, each in its own
    ``CVE_ENT=NN/CVE_MUN=NNN/`` subdirectory — exactly how the upstream
    Azure container ships the data.
    """
    parquet_dir = tmp_path / "areas_municipales.parquet"
    _write_two_level_partition(
        parquet_dir, cve_ent="02", cve_mun="002",
        rows=[{"CVEGEO": "02002", "NOMGEO": "Mexicali",
               "geom": box(-115.5, 32.0, -115.0, 32.5)}],
    )
    _write_two_level_partition(
        parquet_dir, cve_ent="09", cve_mun="012",
        rows=[{"CVEGEO": "09012", "NOMGEO": "Tlalpan",
               "geom": box(-99.3, 19.2, -99.0, 19.4)}],
    )
    _write_two_level_partition(
        parquet_dir, cve_ent="09", cve_mun="015",
        rows=[{"CVEGEO": "09015", "NOMGEO": "Cuauhtémoc",
               "geom": box(-99.2, 19.4, -99.1, 19.5)}],
    )
    return parquet_dir


def test_read_recovers_cve_mun_from_nested_partition(
    fake_two_level_municipalities: Path,
) -> None:
    """The first ingest run silently produced municipalities with no
    CVE_MUN attribute because the reader only declared CVE_ENT in the
    HivePartitioning schema — pyarrow then walked past CVE_MUN
    directories without surfacing them as a column. Asserting CVE_MUN
    is present (and zero-padded as a string) locks that fix in place.
    """
    gdf = read_layer_to_geodataframe(
        fake_two_level_municipalities,
        cve_ent_filter=["09"],
        partition_cols=("CVE_ENT", "CVE_MUN"),
    )

    assert len(gdf) == 2
    # CVE_MUN must round-trip as the canonical 3-digit zero-padded string,
    # not int — JSONB attribute lookups (e.g. aoi_from_alcaldia) compare
    # against text, so "012" matters and 12 doesn't match.
    assert sorted(gdf["CVE_MUN"].tolist()) == ["012", "015"]
    assert sorted(gdf["NOMGEO"].tolist()) == ["Cuauhtémoc", "Tlalpan"]


def test_read_falls_back_to_single_level_partitioning_by_default(
    fake_municipalities: Path,
) -> None:
    """``partition_cols`` defaults to ``('CVE_ENT',)`` so existing
    callers (e.g. ``areas_estatales``) continue to work. This guards
    against accidentally requiring the new param everywhere."""
    gdf = read_layer_to_geodataframe(fake_municipalities, cve_ent_filter=["02"])
    assert len(gdf) == 1
    assert gdf.iloc[0]["NOMGEO"] == "Mexicali"


def test_layer_specs_declare_partition_cols_consistent_with_grain() -> None:
    """States are partitioned by CVE_ENT only; the other three grains
    nest a CVE_MUN level. If a future spec gets the partition wrong,
    the silent column-drop bug we hit on first run would recur."""
    spec_by_slug = {spec.feature_type_slug: spec for spec in LAYER_SPECS.values()}
    assert spec_by_slug["state"].partition_cols == ("CVE_ENT",)
    for slug in ("municipality", "ageb", "block"):
        assert spec_by_slug[slug].partition_cols == ("CVE_ENT", "CVE_MUN"), (
            f"expected nested partition for {slug!r}"
        )

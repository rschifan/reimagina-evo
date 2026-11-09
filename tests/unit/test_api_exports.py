"""Unit tests for apps.api.exports serialisers.

Pure-function coverage over hand-built row dicts — no DB involved.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import geopandas as gpd
import pytest
import shapely.geometry
import shapely.wkb

from apps.api.exports import (
    CSV_COLUMNS,
    indicator_rows_to_csv,
    indicator_rows_to_geojson,
    indicator_rows_to_geoparquet,
)


def _sample_row(
    *,
    feature_id: str = "blk-1",
    type_slug: str = "block",
    value: float | None = 0.75,
    quality_flag: str = "measured",
    geometry: shapely.geometry.base.BaseGeometry | None = None,
) -> dict:
    if geometry is None:
        geometry = shapely.geometry.box(0, 0, 1, 1)
    return {
        "feature_id": feature_id,
        "type_slug": type_slug,
        "geometry_wkb": geometry.wkb,
        "value_numeric": value,
        "quality_flag": quality_flag,
        "computed_at": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
    }


# ─── CSV ────────────────────────────────────────────────────────────────────


def test_csv_header_columns_are_stable():
    csv_bytes = b"".join(indicator_rows_to_csv([]))
    first_line = csv_bytes.decode().splitlines()[0]
    assert first_line == ",".join(CSV_COLUMNS)


def test_csv_emits_one_row_per_input_feature():
    rows = [_sample_row(feature_id="a"), _sample_row(feature_id="b")]
    csv_bytes = b"".join(indicator_rows_to_csv(rows))
    lines = csv_bytes.decode().splitlines()
    assert len(lines) == 3  # header + 2
    assert lines[1].startswith("a,")
    assert lines[2].startswith("b,")


def test_csv_quotes_values_with_commas_correctly():
    # Quality-flag is a closed enum so we simulate nastiness through
    # feature_id instead — standard library csv does the right thing.
    rows = [_sample_row(feature_id='weird,"id')]
    csv_bytes = b"".join(indicator_rows_to_csv(rows))
    lines = csv_bytes.decode().splitlines()
    # feature_id becomes the first column, RFC 4180 quoting.
    assert lines[1].startswith('"weird,""id"')


def test_csv_emits_empty_value_cell_for_missing_numeric():
    rows = [_sample_row(value=None, quality_flag="missing")]
    csv_bytes = b"".join(indicator_rows_to_csv(rows))
    line = csv_bytes.decode().splitlines()[1]
    # CSV value field is empty when value_numeric is None.
    parts = line.split(",")
    assert parts[2] == ""


# ─── GeoJSON ───────────────────────────────────────────────────────────────


def test_geojson_featurecollection_shape():
    rows = [_sample_row()]
    payload = indicator_rows_to_geojson(rows)
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["type"] == "Feature"
    assert feature["id"] == "blk-1"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["value"] == pytest.approx(0.75)
    assert feature["properties"]["type_slug"] == "block"
    assert feature["properties"]["quality_flag"] == "measured"


def test_geojson_skips_rows_without_geometry():
    bad = _sample_row()
    bad["geometry_wkb"] = b""
    payload = indicator_rows_to_geojson([bad, _sample_row(feature_id="keep")])
    assert len(payload["features"]) == 1
    assert payload["features"][0]["id"] == "keep"


def test_geojson_survives_json_dumps_and_reload():
    payload = indicator_rows_to_geojson([_sample_row()])
    reloaded = json.loads(json.dumps(payload))
    assert reloaded["features"][0]["properties"]["value"] == 0.75


# ─── GeoParquet ────────────────────────────────────────────────────────────


def test_geoparquet_round_trips_through_geopandas():
    rows = [_sample_row(), _sample_row(feature_id="blk-2", value=0.12)]
    payload = indicator_rows_to_geoparquet(rows)
    gdf = gpd.read_parquet(io.BytesIO(payload))
    assert len(gdf) == 2
    assert set(gdf.columns) >= {
        "feature_id",
        "type_slug",
        "value",
        "quality_flag",
        "computed_at",
        "geometry",
    }
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326
    values = sorted(gdf["value"].tolist())
    assert values == pytest.approx([0.12, 0.75])


def test_geoparquet_empty_input_produces_valid_file():
    payload = indicator_rows_to_geoparquet([])
    gdf = gpd.read_parquet(io.BytesIO(payload))
    assert len(gdf) == 0
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326


def test_geoparquet_preserves_computed_at_as_string_or_datetime():
    payload = indicator_rows_to_geoparquet([_sample_row()])
    gdf = gpd.read_parquet(io.BytesIO(payload))
    # Serialised form is ISO-8601 string — downstream tools (QGIS) parse this.
    assert isinstance(gdf.iloc[0]["computed_at"], str)
    assert "2026-04-01" in gdf.iloc[0]["computed_at"]


def test_geoparquet_declares_a_covering_bbox():
    """The export must be GeoParquet 1.1 with a covering bbox, not plain 1.0.

    The covering is what lets a reader skip row groups that cannot intersect
    its query instead of decoding every geometry — the same mechanism
    `pipeline/overture.py` relies on when reading the Overture cloud release.
    Writing without it made this project a consumer of cloud-native geospatial
    and not a producer of it.

    Asserted rather than trusted because the geometry of the failure is
    invisible: a file written at 1.0, or at 1.1 without the `covering` key,
    opens perfectly in QGIS and geopandas. It is simply slower to query at
    scale, and nothing says so.
    """
    import pyarrow.parquet as pq

    blob = indicator_rows_to_geoparquet(
        [
            _sample_row(
                feature_id=f"blk-{i}",
                geometry=shapely.geometry.box(i, 0, i + 1, 1),
            )
            for i in range(3)
        ]
    )
    schema = pq.read_schema(io.BytesIO(blob))
    geo = json.loads(schema.metadata[b"geo"])

    assert geo["version"] == "1.1.0", geo["version"]
    covering = geo["columns"][geo["primary_column"]].get("covering")
    assert covering, "no covering declared; a reader cannot find the bbox struct"
    assert set(covering["bbox"]) == {"xmin", "ymin", "xmax", "ymax"}
    # And the struct it names is actually present, not merely promised.
    assert "bbox" in schema.names


def test_geoparquet_still_declares_its_crs():
    """A covering bbox must not cost the CRS.

    Both live in the same `geo` metadata blob, and a file without the CRS
    opens in QGIS as unprojected coordinates near null island rather than
    failing — the kind of wrong that is noticed last.
    """
    import pyarrow.parquet as pq

    blob = indicator_rows_to_geoparquet([_sample_row(), _sample_row()])
    geo = json.loads(pq.read_schema(io.BytesIO(blob)).metadata[b"geo"])
    column = geo["columns"][geo["primary_column"]]
    assert column["crs"]["id"] == {"authority": "EPSG", "code": 4326}
    assert column["encoding"] == "WKB"

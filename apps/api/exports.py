"""Export serialisers for indicator_result rows.

Three pure functions — no FastAPI types, no HTTP knowledge — that turn
the dicts produced by `IndicatorResultRepository.list_current_with_geometry`
into the three shipped download formats:

- CSV: streaming, one row per feature, stable column order.
- GeoJSON: a single in-memory FeatureCollection dict ready for json.dump.
- GeoParquet: a single compressed GeoParquet bytes payload.

Kept side-effect free so they're trivial to unit-test over sample rows
without a DB.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import datetime

import geopandas as gpd
import shapely.wkb


CSV_COLUMNS: tuple[str, ...] = (
    "feature_id",
    "type_slug",
    "value",
    "quality_flag",
    "computed_at",
)


def indicator_rows_to_csv(rows: Iterable[dict]) -> Iterator[bytes]:
    """Yield CSV bytes chunked row by row.

    Columns are pinned (CSV_COLUMNS) so consumers can script against a
    stable order. Only `value_numeric` is emitted in the `value` column —
    GeoJSON and GeoParquet carry the full value shape for richer downloads;
    CSV stays intentionally narrow.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow({
            "feature_id": row["feature_id"],
            "type_slug": row["type_slug"],
            "value": _coerce_numeric(row.get("value_numeric")),
            "quality_flag": row["quality_flag"],
            "computed_at": _coerce_datetime(row["computed_at"]),
        })
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


class MalformedGeometryError(ValueError):
    """Raised when an indicator row carries a non-null WKB that shapely can't decode.

    The download formats are the boundary where a silent skip would turn a
    storage-layer bug into a subtly-truncated file for the user. Raising
    here lets the API return a 500 that surfaces the problem, rather than
    shipping a 200 with missing features.
    """


def _decode_geometry(row: dict):
    """Shapely-decode row['geometry_wkb']. Returns None iff WKB is null/empty.

    Non-empty-but-broken WKB raises MalformedGeometryError with the row's
    feature_id for triage.
    """
    wkb = row.get("geometry_wkb")
    if not wkb:
        return None
    try:
        return shapely.wkb.loads(bytes(wkb))
    except Exception as exc:  # shapely raises GEOSException / TypeError / etc.
        raise MalformedGeometryError(
            f"Undecodable WKB for feature_id={row.get('feature_id')!r}: {exc}"
        ) from exc


def indicator_rows_to_geojson(rows: Iterable[dict]) -> dict:
    """Return a GeoJSON FeatureCollection (EPSG:4326) for every row.

    Each feature carries value + quality_flag + computed_at on `properties`
    and a Shapely-decoded GeoJSON geometry. Rows with a null / empty WKB
    are skipped; rows with a non-null but undecodable WKB raise
    MalformedGeometryError so the caller sees the failure instead of a
    silently-truncated file.
    """
    features: list[dict] = []
    for row in rows:
        geom = _decode_geometry(row)
        if geom is None:
            continue
        features.append({
            "type": "Feature",
            "id": row["feature_id"],
            "geometry": geom.__geo_interface__,
            "properties": {
                "feature_id": row["feature_id"],
                "type_slug": row["type_slug"],
                "value": _coerce_numeric(row.get("value_numeric")),
                "quality_flag": row["quality_flag"],
                "computed_at": _coerce_datetime(row["computed_at"]),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def indicator_rows_to_geoparquet(rows: Iterable[dict]) -> bytes:
    """Serialise rows to a single GeoParquet byte payload.

    Preserves CRS=EPSG:4326 so the file opens directly in QGIS / geopandas
    without further reprojection. Returns an empty (valid) GeoParquet when
    `rows` is empty. Malformed WKB raises MalformedGeometryError (see
    indicator_rows_to_geojson for rationale).
    """
    records = []
    geometries = []
    for row in rows:
        geom = _decode_geometry(row)
        if geom is None:
            continue
        records.append({
            "feature_id": row["feature_id"],
            "type_slug": row["type_slug"],
            "value": _coerce_numeric(row.get("value_numeric")),
            "quality_flag": row["quality_flag"],
            "computed_at": _coerce_datetime(row["computed_at"]),
        })
        geometries.append(geom)

    gdf = gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")
    buffer = io.BytesIO()
    gdf.to_parquet(buffer, compression="snappy")
    return buffer.getvalue()


# ─── helpers ────────────────────────────────────────────────────────────────


def _coerce_numeric(value) -> float | None:
    """Convert Decimal / str-encoded numeric values into a plain float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value) -> str:
    """Render timestamps as ISO-8601 strings. Non-datetimes pass through as str."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)

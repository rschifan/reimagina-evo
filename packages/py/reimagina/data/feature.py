"""Feature type catalog seeding and feature loading."""
from __future__ import annotations

import geopandas as gpd

from reimagina.storage import backend_for_connection


FEATURE_TYPE_ROWS = [
    ("MEX",    "state",         "Estado",        "polygon", "context",        1),
    ("MEX",    "municipality",  "Municipio",     "polygon", "context",        2),
    ("MEX",    "neighborhood",  "Colonia",       "polygon", "context",        3),
    ("MEX",    "ageb",          "AGEB",          "polygon", "analysis_unit",  4),
    ("MEX",    "block",         "Manzana",       "polygon", "analysis_unit",  5),
    ("MEX",    "lot",           "Predio",        "polygon", "analysis_unit",  6),
    ("global", "building",      "Building",      "polygon", "context",        None),
    ("global", "tree",          "Tree",          "point",   "detection",      None),
    ("global", "bus_stop",      "Bus Stop",      "point",   "poi",            None),
    ("global", "station",       "Station",       "point",   "poi",            None),
    ("global", "establishment", "Establishment", "point",   "poi",            None),
    ("global", "road",          "Road",          "line",    "infrastructure", None),
    ("global", "sidewalk",      "Sidewalk",      "line",    "infrastructure", None),
    ("global", "bike_lane",     "Bike Lane",     "line",    "infrastructure", None),
    ("global", "crossing",      "Crossing",      "point",   "infrastructure", None),
]


def seed_feature_types(conn) -> None:
    """Insert the canonical Mexico + global feature types into feature_type_catalog.

    Safe to call multiple times — uses ON CONFLICT DO NOTHING.
    """
    backend_for_connection(conn).feature_repo().seed_feature_types(FEATURE_TYPE_ROWS)


def get_type_id(conn, context: str, type_slug: str) -> str:
    """Return the UUID of a feature type by context and slug."""
    return backend_for_connection(conn).feature_repo().get_type_id(context, type_slug)


def load_features(
    conn,
    gdf: gpd.GeoDataFrame,
    *,
    source_id: str,
    ingestion_run_id: str,
    type_id: str,
    dataset_version: str,
    source_record_ref_col: str,
    name_col: str | None = None,
    attributes_cols: list[str] | None = None,
    parent_id_col: str | None = None,
) -> int:
    """Load a GeoDataFrame of features into curated_geo.feature.

    The trigger on the table will set type_slug and role from type_id
    and validate geometry family and source alignment.

    Returns the number of rows inserted (0 on conflict with existing source_record_ref).
    """
    return backend_for_connection(conn).feature_repo().bulk_insert_features(
        gdf,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        type_id=type_id,
        dataset_version=dataset_version,
        source_record_ref_col=source_record_ref_col,
        name_col=name_col,
        attributes_cols=attributes_cols,
        parent_id_col=parent_id_col,
    )

"""Shared seeding helpers for integration tests.

Historically these lived inside ``test_data_layer_contracts`` and other
tests imported them via ``from tests.integration.test_data_layer_contracts
import _register_source, ...``. That cross-test import made one test file
an implicit fixture library. Keeping the helpers here makes the dependency
explicit and keeps test files symmetric.
"""

from __future__ import annotations

import warnings
from uuid import uuid4

from reimagina.data import catalog as cat


def register_source(
    conn,
    *,
    name: str,
    storage_type: str,
    integration_role: str,
) -> str:
    return cat.register_source(
        conn,
        name=name,
        provider="test",
        storage_type=storage_type,
        integration_role=integration_role,
        storage_uri=f"file:///tmp/{name}.dat",
        version="v1",
    )


def start_run(conn, source_id: str, step: str = "test_load") -> str:
    return cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step=step,
        code_version="test",
    )


def insert_feature(
    conn,
    *,
    type_id: str,
    source_id: str,
    ingestion_run_id: str,
    dataset_version: str,
    source_record_ref: str,
    geometry_wkt: str,
    parent_id: str | None = None,
    feature_id: str | None = None,
    attributes: dict | None = None,
) -> str:
    import json

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (id, type_id, parent_id, name, geometry, attributes,
               source_id, source_record_ref, ingestion_run_id, dataset_version)
            VALUES (
              COALESCE(%s::uuid, gen_random_uuid()),
              %s, %s, %s,
              ST_SetSRID(ST_GeomFromText(%s), 4326),
              %s::jsonb,
              %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                feature_id,
                type_id,
                parent_id,
                source_record_ref,
                geometry_wkt,
                json.dumps(attributes or {}),
                source_id,
                source_record_ref,
                ingestion_run_id,
                dataset_version,
            ),
        )
        return str(cur.fetchone()[0])


def insert_project(conn, name: str = "Test Project") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_core.project (name, description)
            VALUES (%s, %s)
            RETURNING id
            """,
            (name, "integration test project"),
        )
        return str(cur.fetchone()[0])


def seed_catalog_committed(conn, specs=None) -> None:
    """``seed_catalog`` on an autocommit connection, in one transaction.

    Tests that need committed writes visible to a worker's own connection set
    ``autocommit = True``, which breaks catalog seeding: a composite's
    dependency rows are validated by a deferred constraint trigger asserting the
    weights sum to 1.0, and under autocommit each row commits — and is checked —
    alone. Suspend autocommit for the seed so the trigger fires once, at the end,
    with every row present.

    ``specs`` defaults to ``SHIPPED_INDICATORS``. Pass a registry to seed a
    custom DAG — it must be topologically ordered, because ``seed_catalog``
    resolves each dependency slug to an id by SELECT and does not sort for you.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS, seed_catalog

    conn.autocommit = False
    try:
        seed_catalog(conn, SHIPPED_INDICATORS if specs is None else specs)
        conn.commit()
    finally:
        conn.autocommit = True


def discard(conn, sql: str, *params) -> None:
    """One cleanup statement, guarded and committed on its own.

    Cleanup as a single transaction is cleanup that stops at its first
    failure: everything after it stays unrun and the rows this test
    *committed* live on in the session's shared database. Each statement
    therefore stands alone, and a failure is warned about rather than
    swallowed — silence here looks exactly like success.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - warned about, never fatal
        conn.rollback()
        warnings.warn(f"cleanup failed ({sql.split()[2]}): {exc}", stacklevel=2)


# ── Synthetic OSM network ───────────────────────────────────────────────────

#: ~0.0009° of longitude at 19.3°N is ~95 m — one lattice step reads as a city
#: block. Kept as constants so tests can position amenities relative to nodes.
LATTICE_STEP = 0.0009
LATTICE_LON0 = -99.1800
LATTICE_LAT0 = 19.2900
LATTICE_EDGE_M = 95.0


def lattice_node_id(col: int, row: int) -> int:
    """Stable OSM-style node id for the lattice at ``(col, row)``."""
    return 1_000_000 + row * 10 + col


def lattice_xy(col: int, row: int) -> tuple[float, float]:
    """Lon/lat of the lattice node at ``(col, row)``."""
    return LATTICE_LON0 + col * LATTICE_STEP, LATTICE_LAT0 + row * LATTICE_STEP


def osm_lattice(side: int = 3):
    """An ``(nodes, edges)`` pair in the shape ``osmnx.graph_to_gdfs`` returns.

    A ``side``×``side`` grid with no diagonals, so the network distance between
    opposite corners is knowably longer than the straight line — the property
    that distinguishes a real routing result from a KNN in disguise.

    Lives here rather than in a test module because two suites need it, and
    this file exists precisely so a test file never becomes another's fixture
    library.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString, Point

    nodes = []
    for row in range(side):
        for col in range(side):
            lon, lat = lattice_xy(col, row)
            nodes.append({"osmid": lattice_node_id(col, row), "x": lon, "y": lat})
    nodes_df = pd.DataFrame(nodes).set_index("osmid")
    nodes_gdf = gpd.GeoDataFrame(
        nodes_df,
        geometry=[Point(r.x, r.y) for r in nodes_df.itertuples()],
        crs="EPSG:4326",
    )

    edges = []
    for row in range(side):
        for col in range(side):
            here = lattice_xy(col, row)
            if col + 1 < side:
                edges.append(
                    (
                        lattice_node_id(col, row),
                        lattice_node_id(col + 1, row),
                        here,
                        lattice_xy(col + 1, row),
                    )
                )
            if row + 1 < side:
                edges.append(
                    (
                        lattice_node_id(col, row),
                        lattice_node_id(col, row + 1),
                        here,
                        lattice_xy(col, row + 1),
                    )
                )

    edges_gdf = gpd.GeoDataFrame(
        {
            "u": [e[0] for e in edges],
            "v": [e[1] for e in edges],
            "key": [0] * len(edges),
            "osmid": [9_000_000 + i for i in range(len(edges))],
            "length": [LATTICE_EDGE_M] * len(edges),
        },
        geometry=[LineString([e[2], e[3]]) for e in edges],
        crs="EPSG:4326",
    )
    return nodes_gdf, edges_gdf

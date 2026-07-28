"""Shared seeding helpers for integration tests.

Historically these lived inside ``test_data_layer_contracts`` and other
tests imported them via ``from tests.integration.test_data_layer_contracts
import _register_source, ...``. That cross-test import made one test file
an implicit fixture library. Keeping the helpers here makes the dependency
explicit and keeps test files symmetric.
"""

from __future__ import annotations

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


def seed_catalog_committed(conn) -> None:
    """``seed_catalog`` on an autocommit connection, in one transaction.

    Tests that need committed writes visible to a worker's own connection set
    ``autocommit = True``, which breaks catalog seeding: a composite's
    dependency rows are validated by a deferred constraint trigger asserting the
    weights sum to 1.0, and under autocommit each row commits — and is checked —
    alone. Suspend autocommit for the seed so the trigger fires once, at the end,
    with every row present.
    """
    from reimagina.indicators.catalog import seed_catalog

    conn.autocommit = False
    try:
        seed_catalog(conn)
        conn.commit()
    finally:
        conn.autocommit = True

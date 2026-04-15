"""Observation loading with SCD Type 2 semantics."""
from __future__ import annotations
from typing import Any

from reimagina.storage import backend_for_connection

def upsert_observation(
    conn,
    *,
    feature_id: str,
    attribute_key: str,
    value_type: str,
    value: Any,
    source_id: str,
    ingestion_run_id: str,
    lineage_ref: dict | None = None,
) -> str:
    """Insert a new current observation, closing any prior current row for the same key.

    Implements the SCD Type 2 refresh rule from DATA_LAYER.md:
    mark old row is_current=false + valid_to=now() before inserting the new current row.

    Returns the new observation UUID.
    """
    return backend_for_connection(conn).observation_repo().upsert_current(
        feature_id=feature_id,
        attribute_key=attribute_key,
        value_type=value_type,
        value=value,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        lineage_ref=lineage_ref,
    )


def bulk_upsert_observations(
    conn,
    records: list[dict],
    *,
    source_id: str,
    ingestion_run_id: str,
) -> int:
    """Upsert a list of observations.

    Each record must have: feature_id, attribute_key, value_type, value.
    Optional: lineage_ref (dict).

    Returns the number of observations inserted.
    """
    return backend_for_connection(conn).observation_repo().bulk_upsert_current(
        records,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
    )

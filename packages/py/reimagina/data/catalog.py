"""Source registry and ingestion run management (data_source_catalog, ingestion_run)."""

from __future__ import annotations
from typing import Any

from reimagina.assets.publish import publish_version
from reimagina.storage import backend_for_connection


def register_source(
    conn,
    *,
    name: str,
    provider: str,
    storage_type: str,
    integration_role: str,
    storage_uri: str,
    version: str,
    license: str | None = None,
    notes: str | None = None,
) -> str:
    """Register a data source before any data is loaded. Returns the new source UUID."""
    return (
        backend_for_connection(conn)
        .catalog_repo()
        .register_or_get_source(
            name=name,
            provider=provider,
            storage_type=storage_type,
            integration_role=integration_role,
            storage_uri=storage_uri,
            version=version,
            license=license,
            notes=notes,
        )
    )


def get_source(
    conn,
    *,
    name: str,
    provider: str,
    version: str,
) -> dict[str, Any] | None:
    """Return a registered source row by provider/name/version, if present."""
    return (
        backend_for_connection(conn)
        .catalog_repo()
        .get_source(
            name=name,
            provider=provider,
            version=version,
        )
    )


def update_source_storage_uri(conn, source_id: str, storage_uri: str) -> None:
    """Update the canonical storage URI once the Bronze artifact is landed."""
    backend_for_connection(conn).catalog_repo().update_source_storage_uri(
        source_id, storage_uri
    )


def start_ingestion_run(
    conn,
    *,
    source_id: str,
    orchestrator: str,
    orchestrator_run_id: str,
    transformation_step: str,
    code_version: str,
) -> str:
    """Start an ingestion run. Returns the new run UUID."""
    return (
        backend_for_connection(conn)
        .catalog_repo()
        .start_ingestion_run(
            source_id=source_id,
            orchestrator=orchestrator,
            orchestrator_run_id=orchestrator_run_id,
            transformation_step=transformation_step,
            code_version=code_version,
        )
    )


def complete_ingestion_run(
    conn,
    run_id: str,
    *,
    status: str,
    details: dict[str, Any] | None = None,
    asset_key: str | None = None,
    rows_written: int | None = None,
    dataset_version: str | None = None,
) -> None:
    """Mark a run as succeeded, failed, or cancelled.

    When ``asset_key`` names a registered asset and the run succeeded having
    written rows, this also publishes a new version for it — which is what turns
    "the buildings layer was reloaded" into "every indicator that reads buildings
    is now stale", without anything having to remember to say so.

    Published *before* delegating to the repository, because that call refreshes
    ``serving_geo.feature_current_attributes_mv`` non-CONCURRENTLY and holds an
    AccessExclusiveLock for the rest of the transaction. Nothing else should ride
    inside that window.

    ⚠ Publishing is skipped when zero rows were written. Vector loads are
    ON CONFLICT DO NOTHING, so re-running an ingest at the same dataset_version
    changes nothing — and bumping the version for it would stale every
    downstream indicator to recompute identical numbers.

    ``rows_written`` is an explicit parameter rather than something read back out
    of ``details``, because ``details`` has no agreed shape: the vector loader
    writes ``records_written`` while ``inegi_censo`` writes
    ``observations_written``. Fishing for one key would have silently never
    published the census layer.
    """
    if asset_key and status == "succeeded" and rows_written:
        publish_version(
            conn,
            asset_key=asset_key,
            version=str(run_id),
            ingestion_run_id=run_id,
            dataset_version=dataset_version,
            row_count=rows_written,
        )
        # Column has existed since r002 and was never written by anything.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE curated_geo.data_source_catalog SET ingested_at = now() "
                "WHERE id = (SELECT source_id FROM curated_geo.ingestion_run "
                "WHERE id = %s)",
                (run_id,),
            )

    backend_for_connection(conn).catalog_repo().complete_ingestion_run(
        run_id,
        status=status,
        details=details,
    )

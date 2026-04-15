"""Source registry and ingestion run management (data_source_catalog, ingestion_run)."""
from __future__ import annotations
from typing import Any

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
    return backend_for_connection(conn).catalog_repo().register_or_get_source(
        name=name,
        provider=provider,
        storage_type=storage_type,
        integration_role=integration_role,
        storage_uri=storage_uri,
        version=version,
        license=license,
        notes=notes,
    )


def get_source(
    conn,
    *,
    name: str,
    provider: str,
    version: str,
) -> dict[str, Any] | None:
    """Return a registered source row by provider/name/version, if present."""
    return backend_for_connection(conn).catalog_repo().get_source(
        name=name,
        provider=provider,
        version=version,
    )


def update_source_storage_uri(conn, source_id: str, storage_uri: str) -> None:
    """Update the canonical storage URI once the Bronze artifact is landed."""
    backend_for_connection(conn).catalog_repo().update_source_storage_uri(source_id, storage_uri)


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
    return backend_for_connection(conn).catalog_repo().start_ingestion_run(
        source_id=source_id,
        orchestrator=orchestrator,
        orchestrator_run_id=orchestrator_run_id,
        transformation_step=transformation_step,
        code_version=code_version,
    )


def complete_ingestion_run(
    conn,
    run_id: str,
    *,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Mark a run as succeeded, failed, or cancelled."""
    backend_for_connection(conn).catalog_repo().complete_ingestion_run(
        run_id,
        status=status,
        details=details,
    )

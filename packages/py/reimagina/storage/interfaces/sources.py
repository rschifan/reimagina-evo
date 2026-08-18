"""Provenance: where data came from and what was ingested.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from typing import Any, Protocol


class SourceCatalogRepository(Protocol):
    def register_or_get_source(
        self,
        *,
        name: str,
        provider: str,
        storage_type: str,
        integration_role: str,
        storage_uri: str,
        version: str,
        license: str | None = None,
        notes: str | None = None,
    ) -> str: ...

    def get_source(self, *, name: str, provider: str, version: str) -> dict | None: ...

    def update_source_storage_uri(self, source_id: str, storage_uri: str) -> None: ...

    def start_ingestion_run(
        self,
        *,
        source_id: str,
        orchestrator: str,
        orchestrator_run_id: str,
        transformation_step: str,
        code_version: str,
    ) -> str: ...

    def complete_ingestion_run(
        self,
        run_id: str,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class FileAssetRepository(Protocol):
    def register_file_asset(
        self,
        *,
        source_id: str,
        ingestion_run_id: str,
        storage_uri: str,
        dataset_version: str,
        format: str | None = None,
        crs: str | None = None,
        resolution_m: float | None = None,
        band_description: dict | None = None,
        captured_at: Any | None = None,
    ) -> str: ...

    def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        """Return the ``file_asset`` row for a source_id, or None.

        Earns its place by centralising the lookup SQL — otherwise every
        caller writes its own ``SELECT ... WHERE source_id = %s`` with the
        RealDictCursor row handling. Same rationale as
        ``iter_geometries_by_type``.
        """
        ...

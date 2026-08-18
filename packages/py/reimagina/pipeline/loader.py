"""Silver → curated_geo loader. Routes by storage_type."""

from __future__ import annotations
from pathlib import Path
from typing import Any

import pandas as pd

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import observation as obs
from reimagina.assets.publish import asset_key_for_feature_type
from reimagina.storage import backend_for_connection
from . import silver


def _failure_details(exc: Exception) -> dict[str, str]:
    """Return a compact, durable error payload for ingestion_run.details."""
    return {
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }


def _rollback_if_possible(conn) -> None:
    """Best-effort rollback for backends with different transaction semantics.

    Failures are intentionally swallowed: if the connection is already dead
    or the backend does not support explicit rollback, the caller's original
    exception is still re-raised unchanged.  The data transaction is therefore
    always either rolled back or lost — never silently committed.
    """
    try:
        conn.rollback()
    except Exception:
        pass


def load_vector_source(
    conn,
    *,
    source_id: str,
    silver_path: str | Path,
    type_id: str,
    dataset_version: str,
    source_record_ref_col: str,
    orchestrator_run_id: str,
    code_version: str = "dev",
    name_col: str | None = None,
    attributes_cols: list[str] | None = None,
) -> dict[str, Any]:
    """Load a Silver GeoJSON/shapefile into curated_geo.feature.

    Creates and completes an ingestion_run for lineage.
    Commits on success; rolls back and marks run failed on error.

    Returns {"ingestion_run_id": str, "records_written": int}.
    """
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="manual",
        orchestrator_run_id=orchestrator_run_id,
        transformation_step="silver_to_curated",
        code_version=code_version,
    )
    # Commit the run-start row immediately so it is durably recorded before any
    # data is written.  This allows the failure path (except block below) to
    # UPDATE the same row to status='failed' even after the data transaction has
    # been rolled back.
    #
    # Trade-off: if the process is killed between this commit and the final
    # complete_ingestion_run() call, the run stays in status='running' indefinitely.
    # A periodic cleanup query (WHERE status='running' AND started_at < now()-interval)
    # is the intended remedy; see docs/ENGINEERING_NOTES.md.
    conn.commit()
    try:
        backend = backend_for_connection(conn)
        feature_repo = backend.feature_repo()
        if Path(silver_path).suffix.lower() == ".parquet" and hasattr(
            feature_repo, "bulk_insert_features_from_parquet"
        ):
            count = feature_repo.bulk_insert_features_from_parquet(
                parquet_path=silver_path,
                source_id=source_id,
                ingestion_run_id=run_id,
                type_id=type_id,
                dataset_version=dataset_version,
                source_record_ref_col=source_record_ref_col,
                name_col=name_col,
                attributes_cols=attributes_cols,
            )
        else:
            gdf = silver.read_vector(silver_path)
            count = feat.load_features(
                conn,
                gdf,
                source_id=source_id,
                ingestion_run_id=run_id,
                type_id=type_id,
                dataset_version=dataset_version,
                source_record_ref_col=source_record_ref_col,
                name_col=name_col,
                attributes_cols=attributes_cols,
            )
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="succeeded",
            details={"records_written": count},
            rows_written=count,
            dataset_version=dataset_version,
            # Derived, not threaded through every caller: this function already
            # holds the type_id, and an asset carries feature_type_id as an FK.
            # A layer with no registered asset publishes nothing — not an error,
            # and an ingest must not fail because the graph has not adopted it.
            asset_key=asset_key_for_feature_type(conn, type_id),
        )
        conn.commit()
        return {"ingestion_run_id": run_id, "records_written": count}
    except Exception as exc:
        _rollback_if_possible(conn)
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="failed",
            details=_failure_details(exc),
        )
        conn.commit()
        raise


def load_tabular_source(
    conn,
    *,
    source_id: str,
    df: pd.DataFrame,
    feature_id_col: str,
    attribute_key: str,
    value_type: str,
    value_col: str,
    orchestrator_run_id: str,
    code_version: str = "dev",
    asset_key: str | None = None,
) -> dict[str, Any]:
    """Load a tabular DataFrame as observations into curated_geo.observation.

    Each row produces one observation for the given feature_id and attribute_key.
    Commits on success; rolls back and marks run failed on error.

    Returns {"ingestion_run_id": str, "records_written": int}.
    """
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="manual",
        orchestrator_run_id=orchestrator_run_id,
        transformation_step="silver_to_curated",
        code_version=code_version,
    )
    # See load_vector_source for the rationale behind this early commit.
    conn.commit()
    try:
        records = [
            {
                "feature_id": str(row[feature_id_col]),
                "attribute_key": attribute_key,
                "value_type": value_type,
                "value": row[value_col],
            }
            for _, row in df.iterrows()
        ]
        count = obs.bulk_upsert_observations(
            conn, records, source_id=source_id, ingestion_run_id=run_id
        )
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="succeeded",
            details={"records_written": count},
            asset_key=asset_key,
            rows_written=count,
        )
        conn.commit()
        return {"ingestion_run_id": run_id, "records_written": count}
    except Exception as exc:
        _rollback_if_possible(conn)
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="failed",
            details=_failure_details(exc),
        )
        conn.commit()
        raise

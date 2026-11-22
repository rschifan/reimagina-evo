"""Silver → curated_geo loader. Routes by storage_type."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import geopandas as gpd
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
    after_insert: Callable[[str, int], int] | None = None,
    supersede: bool = False,
) -> dict[str, Any]:
    """Load a Silver GeoJSON/shapefile into curated_geo.feature.

    Creates and completes an ingestion_run for lineage.
    Commits on success; rolls back and marks run failed on error.

    ``after_insert(run_id, inserted)`` runs inside the load's transaction,
    before the run is completed and a version published, and returns how many
    of the inserted rows it kept — the AOI clip, which must be able to roll
    the whole load back and must not publish rows it is about to delete.
    ``supersede`` retires other releases of this source; see
    :func:`supersede_other_versions`.

    Returns {"ingestion_run_id": str, "records_written": int,
    "records_superseded": int}; the version is published when either moved.
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
    # is the intended remedy.
    conn.commit()
    try:
        backend = backend_for_connection(conn)
        feature_repo = backend.feature_repo()
        gdf = None
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
        if after_insert is not None:
            count = after_insert(run_id, count)
        superseded = 0
        if supersede:
            if gdf is None:
                gdf = gpd.read_parquet(silver_path, columns=["geometry"])
            geoms = gdf.geometry  # in 4326, as load_features stores it
            if geoms.crs is not None and geoms.crs.to_epsg() != 4326:
                geoms = geoms.to_crs(epsg=4326)
            superseded = supersede_other_versions(
                conn,
                source_id=source_id,
                type_id=type_id,
                bounds=tuple(geoms.total_bounds),
            )
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="succeeded",
            details={"records_written": count, "records_superseded": superseded},
            rows_written=count + superseded,
            dataset_version=dataset_version,
            # Derived, not threaded through every caller: this function already
            # holds the type_id, and an asset carries feature_type_id as an FK.
            # A layer with no registered asset publishes nothing — not an error,
            # and an ingest must not fail because the graph has not adopted it.
            asset_key=asset_key_for_feature_type(conn, type_id),
        )
        conn.commit()
        return {
            "ingestion_run_id": run_id,
            "records_written": count,
            "records_superseded": superseded,
        }
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


class SupersededFeaturesInUse(RuntimeError):
    """A superseded feature carries a scenario edit, so it cannot be deleted."""


def supersede_other_versions(
    conn,
    *,
    source_id: str,
    type_id: str,
    bounds: tuple[float, float, float, float],
) -> int:
    """Delete this layer's features from other releases of the same source.

    A source row is keyed on ``(provider, name, version)`` and Overture's
    version is its release, so a release bump mints a new source and
    ``ON CONFLICT (source_id, dataset_version, source_record_ref)`` cannot see
    the previous release's rows: every GERS id lands twice, and a block counts
    each of its buildings twice. Overture deletes old releases within weeks,
    so the bump is not optional.

    Scoped to the same provider + name (a source name is one extent — "…
    Mexicali BBox") and to ``bounds``, the envelope of the input file, so
    another city's rows under a shared name (places) are untouched. The input,
    not the whole source: two cities loaded at the new release would otherwise
    span an envelope over every city between them. And the input, not the rows
    this run inserted: a re-run of a release already loaded inserts nothing
    (every row conflicts), and must still clean a database doubled before
    this supersede existed. Deleting a feature
    cascades to its derived rows (AOI membership, overlaps, observations,
    results), which the next membership/overlap/indicator run rebuilds — but
    also to scenario edits, which are user data: the load refuses rather than
    drop one.
    """
    if any(math.isnan(b) for b in bounds):  # an empty input covers nothing
        return 0
    other_versions = cat.source_versions_sql("%(source_id)s::uuid", include_self=False)
    superseded = f"""
        SELECT f.id
        FROM curated_geo.feature f
        WHERE f.type_id = %(type_id)s::uuid
          AND f.source_id IN ({other_versions})
          AND ST_Intersects(
              f.geometry, ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326)
          )
    """
    x0, y0, x1, y1 = (float(b) for b in bounds)
    params = {"source_id": source_id, "type_id": type_id, "x0": x0, "y0": y0}
    params |= {"x1": x1, "y1": y1}
    # One statement finds the superseded features once, counts the scenario
    # edits on them, and deletes them only when there are none.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH doomed AS MATERIALIZED ({superseded}),
            edited AS (
                SELECT count(*) AS n FROM curated_geo.scenario_override
                WHERE feature_id IN (SELECT id FROM doomed)
            ),
            deleted AS (
                DELETE FROM curated_geo.feature
                WHERE id IN (SELECT id FROM doomed)
                  AND (SELECT n FROM edited) = 0
                RETURNING 1
            )
            SELECT (SELECT n FROM edited), (SELECT count(*) FROM deleted)
            """,
            params,
        )
        edited, deleted = (int(v) for v in cur.fetchone())
    if edited:
        raise SupersededFeaturesInUse(
            f"{edited} scenario edit(s) reference features of an earlier "
            f"release of this source; deleting those features would delete "
            f"the edits. Move or delete the edits, then reload."
        )
    return deleted


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

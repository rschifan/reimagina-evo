"""Observation → observation rollup worker.

Second arm of the aggregation-recipe dispatcher. Given a set of current
observations at a finer grain (e.g. block-level population) and a
target feature_type (e.g. municipality), this worker joins
``feature_overlap``, applies the recipe's aggregation method via
:func:`reimagina.jobs.aggregation.aggregate`, and writes one
observation per ``(target_feature, source_attribute_key)`` through the
same SCD retire+upsert contract the R3 raster worker uses.

Why structurally parallel to :mod:`raster_zonal_stats` but with a
different entry point:

- The input is observation rows (already in the DB), not raster pixels
  — no rasterstats, no rasterio, no coverage threshold.
- The attribute_key suffix carries through: an input
  ``pop_worldpop_2020_sum`` at block grain rolls up to
  ``<output_prefix>_sum`` at the target grain. The input prefix on the
  recipe describes the *family* we read; the output prefix on the
  recipe describes the *family* we write. One recipe may produce N
  output keys in one run if the input family has N suffixes (sum,
  count, …).

The advisory-lock discipline (prefix lock, dedicated Postgres
transaction) is reused verbatim from R3 so any two recipes writing
the same *output* prefix serialize correctly.

FOLLOW-UP: the private helpers imported from :mod:`raster_zonal_stats`
(``_run_critical_section_postgres``, ``_finalise_run_safely``,
``_maybe_commit``) should be extracted into a shared
``reimagina.jobs._prefix_writer`` module when a third worker needs
them. Imported via their underscore names for now to keep the R3
commit stable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from reimagina.data import catalog as cat
from reimagina.jobs.aggregation import (
    MissingAggregationInputs,
    UnsupportedAggregationMethod,
    aggregate,
)
from reimagina.jobs.raster_zonal_stats import (
    ConcurrentRasterRun,
    _finalise_run_safely,
    _maybe_commit,
    _run_critical_section_postgres,
)
from reimagina.storage import backend_for_connection


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservationRollupResult:
    """Outcome of a single :func:`run_observation_rollup` call.

    ``source_id`` is the id of a dedicated synthetic source registered
    for this rollup run (see :func:`_ensure_rollup_source`). Rollup
    observations aren't produced by any one raster, so they need their
    own source row for lineage completeness — an operator inspecting
    ``observation.source_id`` lands on a source whose ``name`` includes
    ``rollup`` + the recipe id.
    """

    source_id: str
    ingestion_run_id: str
    attribute_key_prefix: str
    targets_written: int
    observations_written: int


def run_observation_rollup(
    conn,
    *,
    input_attribute_key_prefix: str,
    input_feature_type_slug: str,
    target_feature_type_slug: str,
    output_attribute_key_prefix: str,
    method: str,
    weight_basis: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    direct_dsn: str | None = None,
    recipe_id: str | None = None,
) -> ObservationRollupResult:
    """Roll current observations at one grain up to a coarser grain.

    Contract mirrors :func:`run_raster_zonal_stats` — ``conn`` owns the
    ``ingestion_run`` bookkeeping (committed early), a dedicated
    connection performs the retire+upsert inside a prefix-lock-held
    transaction.

    ``method`` must be one of the values accepted by
    :func:`reimagina.jobs.aggregation.aggregate`. ``weight_basis`` is
    only meaningful for ``weighted_mean``; for other methods the
    overlap measure is still threaded through as the weight but
    ignored by :func:`aggregate`.
    """
    if method == "weighted_mean" and weight_basis is None:
        raise ValueError(
            "weighted_mean rollup requires weight_basis ('area'|'length'|'presence')"
        )

    backend = backend_for_connection(conn)

    # Rollup observations need their own source row for lineage. If we
    # reused the raster's source_id the observation would imply the
    # raster is its direct producer — which is wrong: a block→muni
    # rollup's physical input is the block observations, not pixels.
    source_id = _ensure_rollup_source(
        conn,
        recipe_id=recipe_id,
        output_prefix=output_attribute_key_prefix,
        target_feature_type=target_feature_type_slug,
    )

    ingestion_run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="worker",
        orchestrator_run_id=str(uuid4()),
        transformation_step="observation_rollup",
        code_version=code_version,
    )
    _maybe_commit(conn)

    # Compute records outside the critical section. Same trade-off as
    # raster_zonal_stats: rows written between the compute pass and
    # retire would be vacuously stale, but lock-held iteration would
    # starve concurrent runs for no gain at prototype scale.
    try:
        records, targets_written = _compute_rollup_records(
            backend=backend,
            input_prefix=input_attribute_key_prefix,
            input_feature_type=input_feature_type_slug,
            target_feature_type=target_feature_type_slug,
            output_prefix=output_attribute_key_prefix,
            method=method,
            aoi_id=aoi_id,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
        )
    except Exception as exc:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "ObservationRollupError",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise

    try:
        _run_critical_section_postgres(
            direct_dsn=direct_dsn,
            attribute_key_prefix=output_attribute_key_prefix,
            records=records,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
        )
    except ConcurrentRasterRun:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "ConcurrentObservationRollup",
                "prefix": output_attribute_key_prefix,
            },
        )
        raise
    except Exception as exc:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "ObservationRollupWriteError",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise

    _finalise_run_safely(
        conn,
        ingestion_run_id,
        status="succeeded",
        details={
            "targets_written": targets_written,
            "observations_written": len(records),
            "method": method,
            "input_prefix": input_attribute_key_prefix,
            "input_feature_type": input_feature_type_slug,
            "target_feature_type": target_feature_type_slug,
        },
    )

    return ObservationRollupResult(
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        attribute_key_prefix=output_attribute_key_prefix,
        targets_written=targets_written,
        observations_written=len(records),
    )


# ─── Compute ──────────────────────────────────────────────────────────


def _compute_rollup_records(
    *,
    backend,
    input_prefix: str,
    input_feature_type: str,
    target_feature_type: str,
    output_prefix: str,
    method: str,
    aoi_id: str | None,
    source_id: str,
    ingestion_run_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Group overlap × observation rows by (target, source_attribute_key),
    aggregate each group, return observation records ready for bulk upsert.

    Returns ``(records, targets_written)`` where targets_written counts
    distinct target feature ids that got at least one output row.
    """
    flat = backend.overlap_repo().rollup_observation_child_rows(
        input_attribute_key_prefix=input_prefix,
        source_feature_type=input_feature_type,
        target_feature_type=target_feature_type,
        aoi_id=aoi_id,
    )

    # groups: {(target_id, source_attribute_key): [(value, weight), ...]}
    groups: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for target_id, source_attr_key, value, weight in flat:
        groups.setdefault((target_id, source_attr_key), []).append((value, weight))

    records: list[dict[str, Any]] = []
    targets: set[str] = set()
    for (target_id, source_attr_key), values_and_weights in groups.items():
        try:
            aggregated = aggregate(method, values_and_weights)
        except (MissingAggregationInputs, UnsupportedAggregationMethod) as exc:
            logger.warning(
                "observation_rollup: skipping target=%s key=%s: %s",
                target_id,
                source_attr_key,
                exc,
            )
            continue
        output_key = _rewrite_prefix(source_attr_key, input_prefix, output_prefix)
        # Record shape matches ObservationRepository.bulk_upsert_current:
        # ``value_type`` + a single ``value`` cell the repo routes into
        # value_numeric / value_text / value_json.
        records.append(
            {
                "feature_id": target_id,
                "attribute_key": output_key,
                "value_type": "numeric",
                "value": float(aggregated),
                "lineage_ref": {
                    "source_attribute_key": source_attr_key,
                    "input_feature_type": input_feature_type,
                    "target_feature_type": target_feature_type,
                    "method": method,
                    "children": len(values_and_weights),
                },
            }
        )
        targets.add(target_id)

    return records, len(targets)


def _rewrite_prefix(attribute_key: str, input_prefix: str, output_prefix: str) -> str:
    """Swap the prefix on an attribute_key.

    ``pop_worldpop_2020_sum`` with input_prefix=``pop_worldpop_2020``
    and output_prefix=``pop_worldpop_2020_muni`` becomes
    ``pop_worldpop_2020_muni_sum``. An exact match (no suffix) becomes
    just the output prefix.
    """
    if attribute_key == input_prefix:
        return output_prefix
    # Separator-safe: the repo's LIKE matches ``:prefix_<suffix>`` only.
    assert attribute_key.startswith(input_prefix + "_")
    suffix = attribute_key[len(input_prefix) + 1 :]
    return f"{output_prefix}_{suffix}"


# ─── Lineage ──────────────────────────────────────────────────────────


def _ensure_rollup_source(
    conn,
    *,
    recipe_id: str | None,
    output_prefix: str,
    target_feature_type: str,
) -> str:
    """Upsert a per-recipe synthetic source in ``data_source_catalog``.

    Name is stable across runs so repeat rollups write into the same
    source_id (idempotent). storage_uri is a sentinel since there is
    no physical file for a rollup source.
    """
    # ``recipe_id`` is stable when supplied by the dispatcher. The
    # fallback key hashes the prefix so the ad-hoc caller in tests
    # still gets determinism.
    name = (
        f"observation_rollup::{recipe_id}"
        if recipe_id
        else f"observation_rollup::{target_feature_type}::{output_prefix}"
    )
    return cat.register_source(
        conn,
        name=name,
        provider="reimagina-rollup",
        storage_type="tabular",
        integration_role="enrichment",
        storage_uri=f"rollup://observation/{target_feature_type}/{output_prefix}",
        version="current",
        notes=(
            "Synthetic source row for an observation→observation rollup recipe. "
            "Not backed by any physical file — lineage_ref on each observation "
            "carries the real input attribute key + feature type."
        ),
    )

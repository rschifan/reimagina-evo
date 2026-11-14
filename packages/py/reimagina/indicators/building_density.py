"""Building density: pure math + compute worker.

Counts Overture buildings (MEX:building features) whose centroid falls inside
each MEX:block, divides by block area in hectares, and writes one per-block
`indicator_result` row. Blocks with no buildings get an explicit
`value_numeric = 0.0` row (quality_flag='measured') — a block that has simply
not been observed is semantically different from one we have confirmed is
empty, and zero-building blocks are real signal in urban form analysis.

⚠ That distinction only holds while the building layer is actually loaded.
With it absent, every block reports a *confirmed* zero and the claim above
becomes a lie told 67,167 times — which is what happened, and what fed
`urban_form_block_composite` a half-weight of nothing while reporting success.
The two guards below establish the precondition the paragraph assumes.

Centroid-in-polygon is the counting rule: a building is attributed to exactly
one block, avoiding the double-counting that a geometric-intersection rule
would produce at block boundaries. Standard convention in urban analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

from reimagina.data.feature import get_type_id
from reimagina.indicators._compute import ComputeResult as _ComputeResult
from reimagina.indicators._compute import (
    require_measured,
    require_nonempty,
    require_seeded,
)
from reimagina.indicators._hamilton_driver import build_indicator_driver
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_BUILDING_DENSITY

# Re-exported for callers/tests that import the kernel from here; the compute
# below runs it through the Hamilton dataflow, not directly.
from reimagina.indicators.kernels import count_to_density as count_to_density
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class ComputeResult(_ComputeResult):
    """The shared result plus the one statistic only this indicator has.

    Kept out of the shared shape rather than widening it with a field five
    other indicators would leave at None. `features_considered` is the block
    count, matching every other indicator.
    """

    buildings_attributed: int = 0


def compute_block_building_density(
    conn,
    *,
    aoi_id: str | None = None,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Compute buildings-per-hectare for every MEX:block feature.

    Scope the run with `aoi_id` (the AOI's geometry filters *blocks*; every
    building whose centroid falls in an in-scope block is counted regardless
    of whether the building's polygon also touches the AOI). Writes rows
    through `bulk_write_numeric_results` so the block-level uniqueness and
    validation rules fire consistently.
    """
    backend = backend_for_connection(conn)
    spec = URBAN_FORM_BLOCK_BUILDING_DENSITY
    require_seeded(backend, spec)
    block_type_id = get_type_id(conn, "MEX", "block")
    building_type_id = get_type_id(conn, "global", "building")

    with job_run(
        backend,
        model_family=spec.model_family or "urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        # Inside job_run so a refusal still lands with provenance -- see the
        # warning in _compute.py about indicator_result.job_run_id being a
        # NOT NULL FK.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM curated_geo.feature WHERE type_id = %s)",
                (building_type_id,),
            )
            require_nonempty(
                cur.fetchone()[0],
                indicator_slug=spec.slug,
                needs="the global:building feature layer",
                loader="`python -m apps.cli.app overture extract` then `overture load`",
            )

        rows = backend.feature_repo().centroid_in_parent_counts(
            parent_type_id=block_type_id,
            child_type_id=building_type_id,
            aoi_id=aoi_id,
            feature_ids=feature_ids,
        )
        buildings_attributed = sum(count for _, count, _ in rows)
        require_measured(
            buildings_attributed,
            len(rows),
            indicator_slug=spec.slug,
            what="buildings",
            hint=(
                "buildings are loaded but none fall inside these blocks — check "
                "the ingest bbox covers this AOI, and that `aoi compute-features` "
                "has run for it."
            ),
        )
        frame = build_indicator_driver().execute(
            ["density_result_frame"],
            inputs={"centroid_counts": rows},
        )["density_result_frame"]
        numeric_rows = list(frame.itertuples(index=False, name=None))
        result_repo = backend.indicator_result_repo()
        rows_written = result_repo.bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(rows),
        buildings_attributed=buildings_attributed,
        rows_written=rows_written,
        aoi_id=aoi_id,
    )

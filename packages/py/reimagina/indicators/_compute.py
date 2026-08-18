"""The envelope every direct indicator shares, in one place.

Each indicator differs in three ways only: what it **reads**, which Hamilton
node turns those rows into a frame, and what that node calls its input. The
surrounding work — resolve the backend, check the catalog is seeded, open a
``job_run``, write the rows, report a ``ComputeResult`` — was written out once
per indicator and had already drifted: ``planned_floors`` was the one of five
that never grew the catalog check, so against an unseeded database it failed
later with a foreign-key error instead of the actionable message the others
raise.

``accessibility`` had already generalised this into a spec-bound closure
factory. This is that factory, lifted so every indicator can use it.

⚠ **The read happens inside the ``job_run`` context, not before it.**
``indicator_result.job_run_id`` is a NOT NULL FK (r016), so a read that raises
outside the context leaves a failure with no provenance row anywhere. Keep it
inside when editing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from reimagina.indicators._hamilton_driver import build_indicator_driver
from reimagina.indicators.catalog import IndicatorSpec
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class ComputeResult:
    """Outcome of one indicator compute.

    One shape for every indicator. Six identical copies of this used to live
    beside the six compute modules.
    """

    job_run_id: str
    indicator_slug: str
    features_considered: int
    rows_written: int
    aoi_id: str | None


class Reader(Protocol):
    """Reads the rows one indicator computes from.

    ``scenario_id`` is passed to every reader even though the geometry-derived
    ones ignore it, so a single call shape covers both — attribute-reading
    indicators resolve the effective value (override, then observation, then
    base) and must see the scenario.
    """

    def __call__(
        self,
        conn: Any,
        *,
        aoi_id: str | None,
        feature_ids: list[str] | None,
        scenario_id: str | None,
    ) -> Sequence[tuple]: ...


def require_seeded(backend, spec: IndicatorSpec) -> None:
    """Fail with the actionable message rather than a foreign-key violation."""
    if backend.indicator_catalog_repo().get_indicator(spec.slug) is None:
        raise RuntimeError(
            f"Indicator {spec.slug!r} not found in indicator_catalog. "
            "Run `indicators seed-catalog` first."
        )


def compute_indicator(
    spec: IndicatorSpec,
    *,
    reader: Reader,
    node: str,
    input_key: str,
    default_family: str = "urban_form",
) -> Callable[..., ComputeResult]:
    """Bind one indicator's compute fn.

    Returns the callable the runner registry expects. Indicators whose shape
    does not fit — ``building_density`` reports an extra statistic,
    ``planned_floors`` has no Hamilton stage — keep their own body rather than
    growing a hook here for a single caller.
    """

    def run(
        conn,
        *,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
        scenario_id: str | None = None,
        code_version: str = "dev",
    ) -> ComputeResult:
        backend = backend_for_connection(conn)
        require_seeded(backend, spec)

        with job_run(
            backend,
            model_family=spec.model_family or default_family,
            code_version=code_version,
            scenario_id=scenario_id,
        ) as job_run_id:
            rows = reader(
                conn,
                aoi_id=aoi_id,
                feature_ids=feature_ids,
                scenario_id=scenario_id,
            )
            frame = build_indicator_driver().execute([node], inputs={input_key: rows})[
                node
            ]
            rows_written = backend.indicator_result_repo().bulk_write_numeric_results(
                list(frame.itertuples(index=False, name=None)),
                indicator_slug=spec.slug,
                job_run_id=job_run_id,
                scenario_id=scenario_id,
            )

        return ComputeResult(
            job_run_id=job_run_id,
            indicator_slug=spec.slug,
            features_considered=len(rows),
            rows_written=rows_written,
            aoi_id=aoi_id,
        )

    run.__doc__ = spec.description
    return run

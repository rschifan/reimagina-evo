"""Generic composite-indicator computation (Phase 3, F3).

A composite indicator is a weighted combination of its ``depends_on`` inputs'
current results, per feature. ``compute_composite`` reads each input's current
``indicator_result``, **normalises each to 0–1 against its declared catalog
bounds**, combines them per feature (only where every input has a value), and
writes the composite result. It works for any composite ``IndicatorSpec`` — the
platform computes a new composite by registering a catalog entry with weighted
``depends_on``, no new code.

**Why normalise, and why against declared bounds rather than the population.**

Summing raw values makes the weights decorative: the input with the widest range
wins regardless of what the weights say. Measured on Tlalpan before this
changed, ``urban_form_block_composite`` was declared 0.5/0.5 and behaved
98.75 / 1.25 —

    compactness       mean contribution  0.265   (unit ratio_0_1,       range 0–1)
    building_density  mean contribution 20.946   (unit buildings_per_ha, range 0–500)

— because a 500-wide input and a 1-wide input were added directly. That is not a
weighting bug, it is a units bug: metres and dwellings-per-hectare do not add.

Normalising against each input's **declared** ``valid_min`` / ``valid_max``
(already required by the catalog and enforced by r016's write trigger) fixes it
without introducing a population scan. The alternative — percentile or min-max
across the AOI — would make every value depend on every other value, so editing
one block would invalidate all of them and the dirty set could no longer be a
subset of features. That is a genuinely different scheduling shape and is
deliberately *not* adopted here.

⚠ **This makes the maths sound; it does not by itself make a score balanced.**
Normalisation bounds each input's contribution by its weight — no input can
exceed 0.5 of a 0.5-weighted composite, and the result cannot leave 0–1. What it
cannot fix is a bound chosen as a *safety ceiling* rather than a realistic
maximum. ``building_density`` declares ``valid_max=500`` ("a generous safety cap
that catches pathological bugs"); the observed maximum on CDMX is 212.1. So its
normalised values occupy the bottom 42% of their range and the same 0.5/0.5
composite now measures 86% compactness rather than the old 1.25%.

That is a real improvement — before, the weights were decorative and the result
was unbounded — but it exposes that ``valid_min``/``valid_max`` are being asked
to play two roles: the **validation ceiling** r016's trigger enforces on writes,
and the **normalisation reference** a score divides by. Those are different
numbers. Tightening density's bound to 250 would balance the score and start
rejecting legitimately dense blocks elsewhere. Separating the two is the right
move when a real score needs a defensible balance; it is deliberately not done
speculatively here.

**Direction is handled, and has to be.** ``indicator_dependency`` enforces
``weight >= 0`` (r016:93), so a lower-is-better input cannot be folded in with a
negative weight. An accessibility *distance* added raw would make "far from a
school" score well — silently, because the arithmetic stays sound and only the
meaning inverts. Each spec declares ``higher_is_better``; a ``False`` input is
flipped to ``1 - normalised`` after clamping and before weighting, so every
contribution points the same way.

Clamp first, then orient: a value stored above its declared bound would
otherwise orient to a negative contribution.
"""

from __future__ import annotations

from reimagina.indicators._compute import ComputeResult as ComputeResult
from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection


def compute_composite(
    conn,
    *,
    spec: IndicatorSpec,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
    aoi_id: str | None = None,
) -> ComputeResult:
    """Compute ``spec`` = Σ weightᵢ · normalise(inputᵢ) over common features."""
    if spec.computation_kind != "composite" or not spec.depends_on:
        raise ValueError(f"{spec.slug!r} is not a composite with dependencies")

    backend = backend_for_connection(conn)
    result_repo = backend.indicator_result_repo()

    catalog = {s.slug: s for s in SHIPPED_INDICATORS}

    weighted_inputs: list[tuple[float, dict[str, float]]] = []
    for dep_slug, weight in spec.depends_on:
        dep = catalog.get(dep_slug)
        if dep is None or dep.valid_min is None or dep.valid_max is None:
            raise ValueError(
                f"{spec.slug!r} depends on {dep_slug!r}, which declares no "
                "valid_min/valid_max — there is nothing to normalise against. "
                "Falling back to the raw value would silently restore the "
                "units bug this normalisation exists to fix, so it refuses."
            )
        span = dep.valid_max - dep.valid_min
        if span <= 0:
            raise ValueError(
                f"{dep_slug!r} declares valid_min >= valid_max "
                f"({dep.valid_min} >= {dep.valid_max}); cannot normalise."
            )
        lo = dep.valid_min
        rows = result_repo.list_current_for_indicator(
            indicator_slug=dep_slug, scenario_id=scenario_id
        )
        # Orient before weighting, so every contribution points the same way.
        # `weight >= 0` is enforced by r016, so this cannot be expressed as a
        # negative weight; without it a distance input would score "far" as good.
        orient = (lambda x: x) if dep.higher_is_better else (lambda x: 1.0 - x)
        values = {
            # Clamped, because r016's trigger bounds what is *written* but a
            # bound can be widened later while old rows persist, and a value
            # outside 0–1 would let one input exceed its weight. Clamp before
            # orienting, or a value above the bound would orient negative.
            str(r["feature_id"]): orient(
                min(1.0, max(0.0, (r["value_numeric"] - lo) / span))
            )
            for r in rows
            if r.get("value_numeric") is not None
            # Only this composite's own grain. An input rolled up to a coarser
            # feature type carries rows the composite does not apply to, and
            # r016's trigger rejects them at write time — "feature type ageb is
            # not in indicator applies_to ({block})". Filtering here rather than
            # letting the write fail keeps a rollup of an input from breaking
            # the composite that consumes it.
            and r.get("feature_type") in spec.applies_to
        }
        weighted_inputs.append((weight or 0.0, values))

    common = set.intersection(*(set(v) for _, v in weighted_inputs))
    if feature_ids is not None:
        common &= {str(f) for f in feature_ids}

    numeric_rows = [
        (fid, sum(weight * values[fid] for weight, values in weighted_inputs))
        for fid in common
    ]

    with job_run(
        backend,
        model_family=spec.model_family or "composite",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        rows_written = result_repo.bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(common),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )

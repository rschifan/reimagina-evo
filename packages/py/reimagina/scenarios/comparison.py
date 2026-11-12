"""Comparing a scenario against the baseline it departs from.

One read, one answer: per feature, what the baseline says, what the scenario
says, and the difference — plus a summary small enough for a header strip.

Two things make this more than a subtraction.

The first is the join. ``scripts/acceptance_run.sh`` prints the same
comparison as a ``LEFT JOIN`` anchored on the baseline, which is correct for
``urban_form_block_planned_floors`` and wrong for the edificability family:
those five indicators are computed *only* under a scenario, so a baseline
anchor returns nothing at all for them. The join here is therefore a ``FULL
OUTER JOIN``, and a row the scenario alone has is ``new`` rather than missing.

The second is what "the scenario's value" means for a block the scenario never
touched. A scenario stores only what it changed; everything else it reads
through to the baseline, which is the rule
``reimagina.storage._utils.scenario_overlay`` already applies to every other
scenario read. So the ``scenario`` column reported here is the *effective*
value, ``COALESCE(scenario_row, baseline_row)``, resolved before anything is
classified. Classify on the raw scenario column instead and every block
outside the drawn area — nearly all of them, on a real AOI — comes back NULL,
and a map that should read "we changed three blocks" reads "we have no data".

Nothing here writes, so the caller's transaction is untouched; it still owns
it, as everywhere else in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.scenarios.overrides import _require_postgres

#: The three states a feature can be in. ``changed`` and ``unchanged`` both
#: carry a numeric delta; ``new`` cannot, because there is nothing to subtract
#: from.
CHANGED = "changed"
UNCHANGED = "unchanged"
NEW = "new"


@dataclass(frozen=True)
class ComparisonResult:
    """Per-feature rows and the summary counted off them.

    ``summary`` is counted from exactly the rows in ``rows`` rather than
    recomputed by a second query. A header that disagrees with the table below
    it is the failure mode worth designing out, and it is the one a second
    aggregate query invites — the two would be separate statements about the
    same population, free to drift the moment a filter is added to one.
    """

    rows: list[dict[str, Any]]
    summary: dict[str, Any]


def compare(
    conn: Any,
    *,
    scenario_id: str,
    indicator_slug: str,
    aoi_id: str | None = None,
) -> ComparisonResult:
    """Compare one indicator under one scenario against the baseline.

    Each row is ``{feature_id, baseline, scenario, delta, state}``, where
    ``scenario`` is the effective value and ``state`` is one of ``changed``,
    ``unchanged`` or ``new``. Four input combinations collapse onto those
    three:

    ======== ============== =========== =========================
    baseline scenario row   state       delta
    ======== ============== =========== =========================
    yes      yes, different ``changed``   ``scenario - baseline``
    yes      yes, equal     ``unchanged`` ``0.0``
    yes      none           ``unchanged`` ``0.0``
    none     yes            ``new``       ``None``
    ======== ============== =========== =========================

    The third line is the common case, not the corner one: it is every block
    the scenario did not draw over.

    ``summary`` carries the three counts, ``mean_delta``, and
    ``higher_is_better`` — which says whether a positive delta is an
    improvement, and is read from the indicator catalog because only the
    catalog knows. Four of the twelve shipped indicators are distances to the
    nearest amenity, where an increase is a loss.

    ``aoi_id`` scopes the comparison to that AOI's members. Omitted, the
    comparison spans every feature the indicator has a *numeric* result for,
    on one side or the other. The qualifier is the whole of what is left out:
    a feature carrying no numeric value on *either* side is absent from
    ``rows`` and from the counts. A valueless scenario row is not in that set
    — it does not shadow the baseline, so the feature still appears, reading
    ``unchanged`` at its baseline value. See ``_joined``.

    Raises ``LookupError`` for an unknown indicator slug, an unknown scenario,
    or an unknown ``aoi_id``. The last two refusals matter more than they
    look: an id that matches nothing has no results, so every surviving row
    would read as ``unchanged`` — or there would be no rows at all — and the
    answer to "what did my what-if do?" would be a confident "nothing". It is
    the same reasoning that makes ``delete_scenario`` check existence rather
    than report four zeros for a typo, and the two ids deserve it equally: a
    typo in either produces an answer that looks like a finding.
    """
    _require_postgres(conn)
    higher_is_better = _direction(indicator_slug)
    _require_scenario(conn, scenario_id)
    if aoi_id is not None:
        _require_aoi(conn, aoi_id)

    rows = [
        _classify(feature_id, baseline, scenario)
        for feature_id, baseline, scenario in _joined(
            conn, scenario_id=scenario_id, indicator_slug=indicator_slug, aoi_id=aoi_id
        )
    ]
    return ComparisonResult(
        rows=rows, summary=_summarize(rows, higher_is_better=higher_is_better)
    )


def _direction(indicator_slug: str) -> bool:
    """``higher_is_better`` for this slug, from the catalog.

    Not a default and not inferred from the numbers. The field is deliberately
    Python-side (``IndicatorSpec.higher_is_better``; there is no catalog
    column), so this is where the catalog lives for a reader outside
    ``compute_composite`` — and a comparison UI wanting to colour a positive
    delta green is precisely the second consumer that comment anticipated.

    An unknown slug raises rather than falling back to ``True``: a fallback
    would be a hardcoded direction wearing a lookup's clothes, and it would
    say "higher is better" about an indicator nobody has described.
    """
    for spec in SHIPPED_INDICATORS:
        if spec.slug == indicator_slug:
            return spec.higher_is_better
    raise LookupError(f"no indicator in the catalog with slug {indicator_slug!r}")


def _require_scenario(conn: Any, scenario_id: str) -> None:
    """Existence check, without ``_lock_scenario``'s ``FOR UPDATE``.

    The lock in ``authoring`` serialises writers against each other. This is a
    read and takes no lock: holding one would make a dashboard refresh block a
    materialisation for no gain, since there is nothing here for a concurrent
    write to corrupt — a comparison is a snapshot of one transaction's view
    either way.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_core.scenario WHERE id = %s", (scenario_id,))
        if cur.fetchone() is None:
            raise LookupError(f"no scenario with id {scenario_id}")


def _require_aoi(conn: Any, aoi_id: str) -> None:
    """The same refusal as ``_require_scenario``, for the other id.

    An ``aoi_id`` nobody recognises makes the ``EXISTS`` in ``_joined`` match
    nothing, so the comparison comes back with zero rows, ``mean_delta`` of
    ``None`` and all three counts at zero — the shape of a real, honest "this
    scenario changed nothing", produced by a typo. Refusing is the choice
    already made for the scenario id one function up; leaving the AOI
    unchecked would make the pair asymmetric for no reason other than that
    the brief only named one of them.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM curated_geo.area_of_interest WHERE id = %s", (aoi_id,)
        )
        if cur.fetchone() is None:
            raise LookupError(f"no area_of_interest with id {aoi_id}")


def _joined(
    conn: Any,
    *,
    scenario_id: str,
    indicator_slug: str,
    aoi_id: str | None,
) -> list[tuple[str, float | None, float | None]]:
    """``(feature_id, baseline, effective scenario)``, one row per feature.

    Both sides filter ``status = 'current'``: a superseded row is what a run
    replaced, and reading it would report a value the scenario no longer
    holds.

    Both sides also filter ``value_numeric IS NOT NULL``, which is what makes
    the four-way table above exhaustive. ``write_result(value_numeric=None,
    quality_flag='missing')`` is a supported call, so a *current* row with no
    value exists by design — and ``scenario_overlay`` has already ruled that
    such a row does not shadow the baseline, since the overlay is a COALESCE
    over values and COALESCE skips a NULL. Treating a valueless row as absent
    here keeps the two reads saying the same thing, and leaves "no row" and
    "no value" from ever having to be told apart downstream.

    Be precise about what that drops, because it is narrower than "rows
    flagged ``missing`` are dropped". A row is emitted whenever *either* side
    carries a number. So a valueless *scenario* row over a numeric baseline
    changes nothing visible: the baseline survives the join, the COALESCE
    resolves to it, and the feature reads ``unchanged`` at its baseline value
    — exactly as ``scenario_overlay`` says it should. The set that really
    disappears is features with no numeric value on *either* side. Dropping
    those is the load-bearing half: without the baseline-side filter such a
    feature reaches ``_classify`` as ``(None, None)`` and comes back as a
    ``new`` row whose ``baseline``, ``scenario`` and ``delta`` are all null —
    a fifth combination the table above has no cell for, reported as though
    the scenario had added something.

    The join is one-to-one by construction: r016's two partial unique indexes
    allow at most one ``current`` row per ``(feature, indicator)`` baseline and
    per ``(feature, scenario, indicator)``.

    The AOI test is an ``EXISTS`` rather than a join, for the reason every
    other AOI-scoped read in this repository gives: a feature can belong to
    more than one AOI, and a join would multiply it.
    """
    aoi_clause = (
        ""
        if aoi_id is None
        else """
             WHERE EXISTS (
                   SELECT 1 FROM curated_geo.aoi_feature af
                    WHERE af.aoi_id = %(aoi)s::uuid
                      AND af.feature_id = COALESCE(b.feature_id, s.feature_id)
             )"""
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH baseline AS (
                SELECT feature_id, value_numeric
                  FROM curated_geo.indicator_result
                 WHERE indicator_slug = %(slug)s
                   AND status = 'current'
                   AND scenario_id IS NULL
                   AND value_numeric IS NOT NULL
            ), scenario AS (
                SELECT feature_id, value_numeric
                  FROM curated_geo.indicator_result
                 WHERE indicator_slug = %(slug)s
                   AND status = 'current'
                   AND scenario_id = %(scenario)s::uuid
                   AND value_numeric IS NOT NULL
            )
            SELECT COALESCE(b.feature_id, s.feature_id)::text,
                   b.value_numeric,
                   COALESCE(s.value_numeric, b.value_numeric)
              FROM baseline b
              FULL OUTER JOIN scenario s ON s.feature_id = b.feature_id
              {aoi_clause}
             ORDER BY 1
            """,
            {"slug": indicator_slug, "scenario": scenario_id, "aoi": aoi_id},
        )
        return [
            (str(feature_id), _float(baseline), _float(scenario))
            for feature_id, baseline, scenario in cur.fetchall()
        ]


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _classify(
    feature_id: str, baseline: float | None, scenario: float | None
) -> dict[str, Any]:
    """One row of the table in ``compare``'s docstring.

    ``scenario`` arriving here is already the effective value, so the only
    thing a null in it can mean is that the feature has no value on either
    side — and ``_joined`` has already excluded that. The branch order is
    therefore: no baseline is ``new``; otherwise the two numbers either agree
    or they do not.

    ``delta`` is ``None`` for ``new`` and not ``scenario`` itself. A number
    with nothing to compare against is not a change of that size: summing
    deltas across a mixed set would otherwise treat "this block went from
    nothing to 120 units/ha" as a gain of 120 over a value that never existed.
    """
    if baseline is None:
        return {
            "feature_id": feature_id,
            "baseline": None,
            "scenario": scenario,
            "delta": None,
            "state": NEW,
        }
    if scenario == baseline:
        return {
            "feature_id": feature_id,
            "baseline": baseline,
            "scenario": scenario,
            "delta": 0.0,
            "state": UNCHANGED,
        }
    assert scenario is not None  # COALESCE'd to the baseline at worst
    return {
        "feature_id": feature_id,
        "baseline": baseline,
        "scenario": scenario,
        "delta": scenario - baseline,
        "state": CHANGED,
    }


def _summarize(rows: list[dict[str, Any]], *, higher_is_better: bool) -> dict[str, Any]:
    """Count the states off the rows, and average the deltas that exist.

    ``mean_delta`` averages over every *comparable* row — the ones with a
    baseline — including the zeros of the blocks the scenario left alone, and
    is ``None`` when there are none. Including the zeros is the point: it is
    the mean shift across the territory, so upzoning two blocks of 19,058
    reads as the small effect it is. Averaging only the blocks that moved
    answers a different question ("how hard did it hit where it hit?"), and
    answering that one under this name would make every scenario look equally
    transformative.
    """
    counts = {CHANGED: 0, UNCHANGED: 0, NEW: 0}
    for row in rows:
        counts[row["state"]] += 1
    deltas = [row["delta"] for row in rows if row["delta"] is not None]
    return {
        "changed": counts[CHANGED],
        "unchanged": counts[UNCHANGED],
        "new": counts[NEW],
        "mean_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "higher_is_better": higher_is_better,
    }

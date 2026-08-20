"""The submit prelude — everything between "a scenario changed" and "run it".

Resolving the effective change-set and deriving the dirty-set is needed by
every entry point and by every ops-plane: an engine replaces the *execution* of
a run, never the decision of what the run contains. Extracting it here is why
swapping the ops-plane did not touch the dirty-set logic at all.

It is a module of its own rather than a function in ``intake`` because both are
true at once — the sync path and every async engine call it — so it must not
live inside either. Nothing here touches a queue, a worker or a run table.

**Noop.** An empty dirty-set means nothing was invalidated, which is not the
same as a run that succeeded at doing nothing. Both callers had their own
opinion: the async path returned a distinct ``noop`` while the sync path logged
a warning and reported ``succeeded``, so the same edit answered differently
depending on which entry point you used. The decision lives here now, once, and
``noop_handle`` is the one shape a caller returns for it.

``noop`` is deliberately **return-only** — ``orchestration_run.status`` has no
such value (``r028_orchestration_run_and_job_unit``) and a noop persists no run
to give it to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.graph import topological_order
from reimagina.orchestration.impact import DirtyUnit, compute_dirty_set
from reimagina.orchestration.port import RunHandle, RunStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunPlan:
    """What a run will do, before anything decides how to execute it."""

    units: list[DirtyUnit]
    # scenario_id / aoi_id / code_version — the run-level arguments every
    # executor forwards to its runners. Kept as a dict because both
    # ``run_dirty_set`` and ``runstore.create_run`` accept exactly these as
    # keywords, and an engine has to serialize them onto a queue.
    params: dict[str, Any]

    @property
    def is_noop(self) -> bool:
        return not self.units


def _units_from_freshness(
    conn: object,
    registry: Sequence[IndicatorSpec],
    *,
    scenario_id: str | None = None,
) -> list[DirtyUnit]:
    """Dirty units derived from what is actually out of date.

    Whole-grain by design: ``DirtyUnit.features`` empty already means "every
    feature this indicator applies to", which ``runners`` collapses to
    ``feature_ids=None``. A layer change invalidates the indicator, not a
    knowable subset of its rows — narrowing that would mean joining the changed
    source rows onto the indicator's own grain, which is the
    ``FeatureExpander`` hook's job and still unimplemented.

    Contributes nothing when the asset graph is not present: a deployment that
    has not seeded the registry, or is still on r040, must keep planning runs the
    way it always did. That check is a ``to_regclass`` probe inside
    ``stale_units`` rather than a caught exception here — a failed query would
    leave psycopg2's transaction aborted and poison the connection the caller is
    about to submit on.
    """
    from reimagina.assets.freshness import stale_units

    return [
        DirtyUnit(indicator=slug, features=frozenset())
        for slug in stale_units(conn, registry, scenario_id=scenario_id)
    ]


class UnknownIndicators(ValueError):
    """Named indicators that no registry entry matches.

    Raised at submit rather than left to fail inside the unit: an unregistered
    slug reaches ``s_compute`` as ``LookupError: no runner registered``, which
    surfaces as one failed unit in a run that looks like it was going to work.
    A caller that named a slug wrong deserves to be told immediately.
    """

    def __init__(self, unknown: Sequence[str]) -> None:
        self.unknown = tuple(unknown)
        super().__init__(f"unknown indicator(s): {', '.join(sorted(self.unknown))}")


def plan_run(
    conn: object,
    *,
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
    indicators: Sequence[str] | None = None,
) -> RunPlan:
    """Resolve the change-set and derive the dirty-set, in dependency order.

    Reads the scenario's overrides when the change-set is not given explicitly,
    so a caller can submit ``scenario_id`` alone and have the diff derived from
    the override rows.

    **With nothing specified at all, the dirty set comes from freshness.** A
    submit naming no attribute, no feature and no scenario used to be a
    guaranteed noop with a warning; it now means "recompute what is actually out
    of date", answered by comparing each indicator's materialized version against
    the version its inputs imply. That is what lets an ingest stop guessing which
    attribute name stands in for the layer it just reloaded.

    ⚠ Only ``stale`` and ``partial`` count on the baseline, never ``absent`` —
    see ``assets.freshness``. On a database where nothing has been computed yet
    every indicator is absent, and an empty submit must not mean "compute the
    entire platform".

    **A scenario submit is the same mechanism, at that scenario's partition.**
    Editing a what-if publishes a new version for that *attribute's* override
    asset in that scenario's partition, so the indicators reading that attribute
    go out of date, along with whatever is downstream of them — and nothing
    else, and never the baseline.

    That replaces a deliberate over-approximation: ``resolve_change_set``
    returns the flat union of every attribute and feature the scenario touches,
    so editing one block's ``floors`` recomputed every indicator reading
    ``floors`` across every block. The attribute half is now exact; the feature
    half is still whole-indicator, which is ``impact.FeatureExpander``'s axis.
    """
    params: dict[str, Any] = {
        "scenario_id": scenario_id,
        "aoi_id": aoi_id,
        "code_version": code_version,
    }

    # An explicit indicator list short-circuits derivation entirely.
    #
    # This is the "recompute exactly these" request — a retry of the units that
    # failed, or an operator asking for a named set — and neither freshness nor
    # a change-set can express it. Freshness in particular cannot: it counts
    # only `stale` and `partial`, never `absent`, so asking it to recompute an
    # indicator that has never materialised returns nothing at all.
    if indicators is not None:
        known = {spec.slug for spec in registry}
        unknown = [slug for slug in indicators if slug not in known]
        if unknown:
            raise UnknownIndicators(unknown)
        deps = {
            spec.slug: [dep for dep, _weight in spec.depends_on] for spec in registry
        }
        # Dependency order, not the caller's order: a unit whose prerequisite is
        # also in the set has to be scheduled after it. `nodes=` takes the
        # induced subgraph, so prerequisites outside the set are treated as
        # already materialised, which is exactly right for a partial retry.
        ordered = topological_order(deps, nodes=set(indicators))
        # Empty `features` means "every feature this indicator applies to" —
        # the original per-feature scope lived in `run_plan` and is gone by the
        # time anyone retries, so this is deliberately the wider recompute.
        return RunPlan(
            units=[DirtyUnit(indicator=slug, features=frozenset()) for slug in ordered],
            params=params,
        )

    from reimagina.scenarios.overrides import resolve_change_set

    # Materialised before anything reads them twice. These are typed `Iterable`,
    # and resolve_change_set consumes them — so testing the raw arguments below
    # would see an exhausted generator as truthy and route to the attribute path
    # with an empty change set, silently skipping freshness.
    requested_attributes = tuple(changed_attributes)
    requested_features = tuple(edited_features)
    changed, edited = resolve_change_set(
        conn,
        changed_attributes=requested_attributes,
        edited_features=requested_features,
        scenario_id=scenario_id,
    )
    from reimagina.assets.freshness import graph_available

    if requested_attributes or requested_features:
        # An explicit change-set wins: the caller knows something the graph does
        # not, and says so.
        units = compute_dirty_set(changed, edited, registry)
    elif graph_available(conn):
        units = _units_from_freshness(conn, registry, scenario_id=scenario_id)
    else:
        # No asset graph in this database — an r040 deployment. Fall back to the
        # change-set implied by the scenario's override rows, exactly as before.
        #
        # Gated on the graph being *absent*, not on the freshness result being
        # empty: a graph that answers "nothing is dirty" is a real answer, and
        # retrying the attribute path behind it would resurrect the
        # over-approximation this replaces.
        units = compute_dirty_set(changed, edited, registry)
    if not units:
        logger.warning(
            "scenario %s produced an empty dirty-set — nothing to recompute "
            "(check changed_attributes / scenario overrides)",
            scenario_id,
        )
    return RunPlan(units=units, params=params)


def noop_handle(run_id: str) -> RunHandle:
    """The handle a port implementation returns when the plan was a noop.

    The id is minted and returned for log correlation, but no run exists to
    look up — which is why the API suppresses it (``jobs._submit_response``)
    rather than handing back an id that 404s.
    """
    return RunHandle(
        run_id=run_id, status=RunStatus(run_id=run_id, status="noop", units=())
    )

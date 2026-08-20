"""Impact engine — the dirty-set computation for selective recompute (F1).

Component 3 of the Phase 3 orchestration design
(docs/product/PHASE3_ORCHESTRATION_EXPLORATION.md). Given the attributes a
scenario changed and the features it changed them on, it decides the exact
set of indicator outputs to recompute: seeded from the attribute→indicator
read mapping (``IndicatorSpec.reads``), propagated downstream through the
indicator dependency graph (``IndicatorSpec.depends_on``), and ordered so
every input is computed before the output that needs it.

Pure: a function of (changed attributes, edited features, registry). No DB,
no side effects — the registry is the in-Python ``IndicatorSpec`` tuple, the
same source ``seed_catalog`` writes to ``curated_geo.indicator_catalog``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.graph import (
    dependents_of,
    downstream_closure,
    topological_order,
)


@dataclass(frozen=True)
class DirtyUnit:
    """One indicator to recompute, scoped to the features it must recompute on.

    ``features`` is the set of feature ids whose value is invalidated. An empty
    set means "every feature this indicator applies to" — used when a change
    is not feature-scoped.
    """

    indicator: str
    features: frozenset[str]


# Feature-expansion hook: given an indicator and the features directly hit,
# return the features whose value actually changes. The identity default keeps
# the core pure and topology-free.
#
# ponytail: identity default — wire real expansion when proximity and rollup
# indicators land. Proximity reads neighbours (edited features + their spatial
# neighbourhood); a rollup maps child features to the parent grain (block→AGEB).
# Both need the feature graph, which is not derivable from the registry, so it
# is injected here rather than baked into the engine.
FeatureExpander = Callable[[IndicatorSpec, frozenset[str]], frozenset[str]]


def indicators_for_datasets(
    changed_datasets: Iterable[str],
    registry: Sequence[IndicatorSpec],
) -> tuple[str, ...]:
    """Indicators to recompute when whole *datasets* change, in dependency order.

    The question ``compute_dirty_set`` cannot answer. A change-set names
    attributes, which suits an edit to a field on a feature — but "a street was
    added to the road network" is not an attribute edit. It invalidates every
    indicator that declared an edge to that dataset, whatever attributes it
    reads, plus everything downstream of those.

    Seeds come from the indicator's typed ``inputs`` edges rather than from
    ``reads``: ``reads`` is the flattened attribute list and drops the dataset
    entirely, so it cannot see a dataset-level dependency at all. An indicator
    that relates to ``global.road_network`` spatially declares that edge with no
    attributes on it, which is exactly the case this exists for.

    Same propagate-then-order shape as ``compute_dirty_set``, and `sorted` for
    the same reason — a stable order across processes, not tidiness.
    """
    wanted = frozenset(changed_datasets)
    deps: dict[str, set[str]] = {
        spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry
    }

    seed = set()
    for spec in registry:
        for edge in spec.inputs:
            key = getattr(edge, "key", None) or getattr(edge, "asset_key", "")
            if key in wanted:
                seed.add(spec.slug)
                break

    dirty = downstream_closure(seed, dependents_of(deps))
    if not dirty:
        return ()
    return topological_order(
        {slug: sorted(deps.get(slug, ())) for slug in sorted(dirty)},
        nodes=sorted(dirty),
    )


def compute_dirty_set(
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    registry: Sequence[IndicatorSpec],
    *,
    expand: FeatureExpander | None = None,
) -> list[DirtyUnit]:
    """Return the indicators to recompute, in dependency order.

    1. **Seed** — every indicator whose declared ``reads`` intersects the
       changed attributes.
    2. **Propagate** — any indicator depending (directly or transitively) on a
       dirty indicator is itself dirty.
    3. **Order** — topological over the dependency graph restricted to the
       dirty set, so inputs precede dependents. Raises ``graphlib.CycleError``
       on a cycle (the catalog's acyclicity invariant should prevent this).

    Feature scoping: a seed indicator gets ``edited_features``; a dependent
    gets the union of its dirty inputs' features. Each set is passed through
    ``expand`` (identity by default) so proximity/rollup indicators can widen
    or remap it.
    """
    changed = frozenset(changed_attributes)
    edited = frozenset(edited_features)
    expand = expand or (lambda _spec, feats: feats)

    by_slug = {spec.slug: spec for spec in registry}
    # dependency edges: slug -> the slugs it depends on (its inputs)
    deps: dict[str, set[str]] = {
        spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry
    }

    # 1. seed from attribute reads
    seed = {spec.slug for spec in registry if changed & frozenset(spec.reads)}

    # 2. downstream closure over reverse edges (the dependents of each input)
    dirty = downstream_closure(seed, dependents_of(deps))

    if not dirty:
        return []

    # 3. order the dirty set so inputs precede dependents.
    #
    # `sorted(dirty)` is load-bearing, not tidiness. Feeding the sorter a set
    # makes its output depend on set iteration order, which varies with
    # PYTHONHASHSEED — so this pure function returned a *different order every
    # process*. Measured: ten processes, ten distinct orders on a 20-indicator
    # DAG. Topologically all of them are valid; that is exactly what makes it
    # dangerous, because nothing downstream looks wrong until it matters.
    #
    # It matters twice. A durable-execution ops-plane replays a workflow by
    # matching steps positionally, so a recovering process that re-derived a
    # different order would fail the replay rather than resume. And an engine
    # that declares the indicator DAG statically builds it from this order at
    # import, so two processes could disagree about the workflow they are
    # running. Sorting here fixes both at the source instead of asking every
    # consumer to checkpoint the order it saw.
    # Both halves must be sorted. Sorting only the keys still leaves the
    # predecessors as sets, and the sorter's ready-batch order depends on those
    # too — measured at two distinct orders across processes rather than ten,
    # which is the kind of partial fix that reads as success.
    ordered = topological_order(deps, nodes=dirty)

    # feature scoping, computed in dependency order so a dependent can read its
    # inputs' already-resolved feature sets
    resolved: dict[str, frozenset[str]] = {}
    units: list[DirtyUnit] = []
    for slug in ordered:
        sources = edited if slug in seed else frozenset()
        inputs = deps[slug] & dirty
        if inputs:
            sources = sources.union(*(resolved[i] for i in inputs))
        resolved[slug] = expand(by_slug[slug], sources)
        units.append(DirtyUnit(indicator=slug, features=resolved[slug]))
    return units

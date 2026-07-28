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

import graphlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from reimagina.indicators.catalog import IndicatorSpec


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
    dependents: dict[str, set[str]] = {spec.slug: set() for spec in registry}
    for slug, inputs in deps.items():
        for inp in inputs:
            if inp in dependents:
                dependents[inp].add(slug)

    dirty: set[str] = set()
    stack = list(seed)
    while stack:
        slug = stack.pop()
        if slug in dirty:
            continue
        dirty.add(slug)
        stack.extend(dependents.get(slug, ()))

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
    ordered = graphlib.TopologicalSorter(
        {slug: tuple(sorted(deps[slug] & dirty)) for slug in sorted(dirty)}
    ).static_order()

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

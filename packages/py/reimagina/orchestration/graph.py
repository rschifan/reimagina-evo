"""Engine-neutral, deterministic operations on indicator dependency graphs.

This module intentionally knows only ``slug -> prerequisite slugs``. It has no
DBOS, Hatchet, catalog, database, or task-status dependency, so both ops-plane
tracks share the exact graph semantics while retaining their own schedulers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from graphlib import TopologicalSorter

DependencyMap = Mapping[str, Iterable[str]]


def dependents_of(dependencies: DependencyMap) -> dict[str, frozenset[str]]:
    """Return the reverse edge map for nodes declared in ``dependencies``."""
    reverse: dict[str, set[str]] = {slug: set() for slug in dependencies}
    for slug, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            if prerequisite in reverse:
                reverse[prerequisite].add(slug)
    return {slug: frozenset(children) for slug, children in reverse.items()}


def downstream_closure(
    seeds: Iterable[str], dependents: Mapping[str, Iterable[str]]
) -> frozenset[str]:
    """Return every seed and its transitively dependent nodes."""
    closure: set[str] = set()
    pending = list(seeds)
    while pending:
        slug = pending.pop()
        if slug in closure:
            continue
        closure.add(slug)
        pending.extend(dependents.get(slug, ()))
    return frozenset(closure)


def topological_order(
    dependencies: DependencyMap, *, nodes: Iterable[str] | None = None
) -> tuple[str, ...]:
    """Return a deterministic prerequisite-before-dependent order.

    ``nodes`` selects an induced subgraph. Dependencies outside it are already
    materialized inputs and therefore do not block the selected work. Sorting
    both nodes and prerequisite lists is required: ``TopologicalSorter`` keeps
    insertion order among otherwise-ready nodes.
    """
    selected = set(dependencies) if nodes is None else set(nodes)
    unknown_nodes = selected - dependencies.keys()
    if unknown_nodes:
        raise ValueError(
            f"dependency graph names unknown node(s): {sorted(unknown_nodes)!r}"
        )
    if nodes is None:
        unknown = {
            prerequisite
            for prerequisites in dependencies.values()
            for prerequisite in prerequisites
            if prerequisite not in dependencies
        }
        if unknown:
            raise ValueError(
                f"dependency graph names unknown prerequisite(s): {sorted(unknown)!r}"
            )
    graph = {
        slug: tuple(sorted(set(dependencies.get(slug, ())) & selected))
        for slug in sorted(selected)
    }
    return tuple(TopologicalSorter(graph).static_order())

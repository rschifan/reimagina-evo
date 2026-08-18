"""Engine-neutral graph contract shared by the DBOS and Hatchet tracks."""

from __future__ import annotations

from graphlib import CycleError

import pytest

from reimagina.orchestration.graph import (
    dependents_of,
    downstream_closure,
    topological_order,
)


GRAPH = {
    "green": (),
    "walk": (),
    "liveability": ("green", "walk"),
}


def test_orders_prerequisites_deterministically():
    assert topological_order(GRAPH) == ("green", "walk", "liveability")


def test_selects_an_induced_subgraph_for_dirty_work():
    assert topological_order(GRAPH, nodes={"walk", "liveability"}) == (
        "walk",
        "liveability",
    )


def test_finds_every_downstream_dependent():
    assert downstream_closure({"green"}, dependents_of(GRAPH)) == {
        "green",
        "liveability",
    }


def test_full_graph_rejects_unknown_prerequisites():
    with pytest.raises(ValueError, match="unknown prerequisite"):
        topological_order({"derived": ("missing",)})


def test_rejects_unknown_selected_node_and_cycles():
    with pytest.raises(ValueError, match="unknown node"):
        topological_order(GRAPH, nodes={"missing"})
    with pytest.raises(CycleError):
        topological_order({"one": ("two",), "two": ("one",)})

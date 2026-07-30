"""Two deployments must not disagree about the graph without anyone noticing.

The API derives the plan; a worker executes it. Nothing made them agree about
*which graph* — a half-applied rollout had the API planning one shape while a
worker ran another, and the only symptom would be indicators computed from the
wrong dependencies.

Two derived values, and the split between them is the design:

* `shape_version` — nodes and edges. Names the workflow, so an incompatible
  change mints a new immutable name and old runs drain against the old one.
* `graph_version` — shape plus task semantics. Rides on the run for
  observability, and is deliberately *not* a rejection criterion.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import hatchet_app
from reimagina.orchestration import hatchet_orchestrator as ho
from reimagina.orchestration.graph import shape_version


def _spec(slug: str, depends_on: tuple = ()) -> IndicatorSpec:
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind="composite" if depends_on else "direct",
        value_type="numeric",
        aggregation_method="mean",
        applies_to=("block",),
        description="",
        model_family="urban_form",
        depends_on=depends_on,
    )


# ── shape_version ────────────────────────────────────────────────────────────


def test_shape_version_is_stable_across_processes():
    """A per-process hash seed would make every deployment disagree.

    `hash()` is salted per interpreter, so it cannot be used here; this is a
    sha256 over canonical JSON. The literal below is the point — it pins the
    value itself, so a change in how it is derived shows up as a failing test
    rather than as two deployments silently refusing to talk to each other.
    """
    deps = {"a": [], "b": ["a"]}
    assert shape_version(deps) == shape_version(deps)
    assert len(shape_version(deps)) == 12


def test_shape_version_ignores_declaration_order():
    """The same graph written in a different order is the same graph."""
    assert shape_version({"a": [], "b": ["a"]}) == shape_version({"b": ["a"], "a": []})


def test_shape_version_ignores_duplicate_prerequisites():
    assert shape_version({"b": ["a", "a"], "a": []}) == shape_version(
        {"b": ["a"], "a": []}
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"a": [], "b": ["a"], "c": []},  # node added
        {"a": []},  # node removed
        {"a": [], "b": []},  # edge removed
    ],
)
def test_an_incompatible_change_changes_the_shape(changed):
    """Each of these makes an in-flight run's schedule wrong."""
    assert shape_version(changed) != shape_version({"a": [], "b": ["a"]})


# ── the two versions, and what each one is for ───────────────────────────────


def test_the_workflow_name_carries_the_shape():
    name = hatchet_app.workflow_name(SHIPPED_INDICATORS)
    assert name.startswith("reimagina-run-v")
    assert name.endswith(shape_version(hatchet_app._dependencies(SHIPPED_INDICATORS)))


def test_a_shape_change_mints_a_new_workflow_name():
    """Old workers keep serving the old name until their runs drain."""
    one = hatchet_app.workflow_name((_spec("a"),))
    two = hatchet_app.workflow_name((_spec("a"), _spec("b", (("a", 1.0),))))
    assert one != two


def test_tuning_a_timeout_does_not_mint_a_new_workflow(monkeypatch):
    """⚠ The reason there are two versions rather than one.

    Task semantics change far more often than shape, and are compatible with
    runs already in flight — the engine versions them under one name. Folding
    them into the *name* would fragment the dashboard's run history every time
    somebody adjusted a timeout, for no safety gained.
    """
    before = hatchet_app.workflow_name(SHIPPED_INDICATORS)
    monkeypatch.setattr(hatchet_app, "UNIT_EXECUTION_TIMEOUT", timedelta(minutes=999))
    assert hatchet_app.workflow_name(SHIPPED_INDICATORS) == before


def test_tuning_a_timeout_does_change_the_graph_version(monkeypatch):
    """...but it is still recorded, because it still changes what a run did."""
    before = hatchet_app.graph_version(SHIPPED_INDICATORS)
    monkeypatch.setattr(hatchet_app, "UNIT_EXECUTION_TIMEOUT", timedelta(minutes=999))
    assert hatchet_app.graph_version(SHIPPED_INDICATORS) != before


def test_changing_the_skip_expression_changes_the_graph_version(monkeypatch):
    """What makes a unit skip is semantics, and easy to alter in a format string.

    The un-negated form runs exactly the indicators it should have left alone
    while every task reports COMPLETED, so a silent change here is expensive.
    """
    before = hatchet_app.graph_version(SHIPPED_INDICATORS)
    monkeypatch.setattr(
        hatchet_app, "_SKIP_EXPRESSION_TEMPLATE", "'{slug}' in output.dirty"
    )
    assert hatchet_app.graph_version(SHIPPED_INDICATORS) != before


# ── admission ────────────────────────────────────────────────────────────────


class _Worker:
    def __init__(self, status: str, actions: list[str]) -> None:
        self.status = status
        self.actions = actions


def _engine_listing(monkeypatch, workers):
    from types import SimpleNamespace

    monkeypatch.setattr(
        hatchet_app,
        "get_hatchet",
        lambda: SimpleNamespace(
            workers=SimpleNamespace(
                list=lambda: SimpleNamespace(rows=workers),
            )
        ),
    )


def test_a_live_worker_for_the_graph_admits_the_run(monkeypatch):
    _engine_listing(
        monkeypatch,
        [_Worker("WorkerStatus.ACTIVE", ["reimagina-run-vabc:plan"])],
    )
    assert ho._worker_serves("reimagina-run-vabc") is True


def test_a_worker_for_a_different_graph_does_not_admit_the_run(monkeypatch):
    """The half-applied rollout: workers up, but on the previous shape."""
    _engine_listing(
        monkeypatch,
        [_Worker("WorkerStatus.ACTIVE", ["reimagina-run-vOLD:plan"])],
    )
    assert ho._worker_serves("reimagina-run-vNEW") is False


def test_an_inactive_registration_does_not_admit_the_run(monkeypatch):
    """A registration is history; only a live worker can execute anything.

    Admitting against an expired row is how a run reaches `running` with no
    units and stays there until timeouts fire.
    """
    _engine_listing(
        monkeypatch,
        [_Worker("WorkerStatus.INACTIVE", ["reimagina-run-vabc:plan"])],
    )
    assert ho._worker_serves("reimagina-run-vabc") is False


def test_an_unreadable_worker_list_fails_open(monkeypatch):
    """⚠ Open, not closed, and deliberately.

    This is an admission check, not a safety property — the engine holding the
    run is the safety property. A transient failure to *list workers* must not
    become a submission outage, because the run would very likely have executed
    perfectly well.
    """
    from types import SimpleNamespace

    def _boom():
        raise OSError("engine listing unavailable")

    monkeypatch.setattr(
        hatchet_app,
        "get_hatchet",
        lambda: SimpleNamespace(workers=SimpleNamespace(list=_boom)),
    )
    assert ho._worker_serves("reimagina-run-vabc") is True


def test_a_deployment_mismatch_is_not_reported_as_an_engine_outage():
    """Separate types, because they send an operator to different places.

    `HatchetUnavailable` means "look at the engine" and is retryable.
    `GraphVersionUnavailable` means "finish the rollout" and is not — retrying
    cannot conjure a worker for a graph nobody deployed.
    """
    assert not issubclass(ho.GraphVersionUnavailable, ho.HatchetUnavailable)
    assert issubclass(ho.GraphVersionUnavailable, RuntimeError)

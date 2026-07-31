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


def _engine(monkeypatch, *, workflows=(), workers=()):
    from types import SimpleNamespace

    monkeypatch.setattr(
        hatchet_app,
        "get_hatchet",
        lambda: SimpleNamespace(
            # ⚠ Accepts `workflow_name=`, because the real call filters
            # server-side rather than scanning a page. The engine caps an
            # unfiltered list at 50 in arbitrary order, and this branch mints a
            # permanent workflow per graph shape plus one per bench workload.
            workflows=SimpleNamespace(
                list=lambda workflow_name=None, **_kw: SimpleNamespace(
                    rows=[
                        SimpleNamespace(name=n)
                        for n in workflows
                        if workflow_name is None or n == workflow_name
                    ]
                )
            ),
            workers=SimpleNamespace(list=lambda: SimpleNamespace(rows=list(workers))),
        ),
    )


def _declared(name: str):
    """Stands in for the Workflow object `submit_run` will actually trigger."""
    from types import SimpleNamespace

    return SimpleNamespace(name=name)


def test_submission_is_admitted_when_the_graph_has_been_deployed(monkeypatch):
    _engine(monkeypatch, workflows=["reimagina-run-vabc"])
    assert ho._graph_is_deployed(_declared("reimagina-run-vabc")) is True


def test_the_gate_checks_the_workflow_the_submit_will_trigger(monkeypatch):
    """⚠ The object, not a separately derived name.

    The gate used to check `workflow_name(self._registry)` while the trigger
    went through `get_run_workflow()` — two independent objects with nothing
    tying them together. The workload suite patches `_dag` to a
    `reimagina-bench-*` workflow while passing its own registry, so the gate
    asked about a `reimagina-run-v*` name nobody had registered and refused
    every bench submit against a perfectly healthy engine.
    """
    _engine(monkeypatch, workflows=["reimagina-bench-lattice"])

    assert ho._graph_is_deployed(_declared("reimagina-bench-lattice")) is True
    assert ho._graph_is_deployed(_declared("reimagina-run-vabc")) is False


def test_submission_is_refused_when_nothing_ever_deployed_the_graph(monkeypatch):
    """The half-applied rollout: workers up, but on the previous shape."""
    _engine(monkeypatch, workflows=["reimagina-run-vOLD"])
    assert ho._graph_is_deployed(_declared("reimagina-run-vNEW")) is False


def test_submission_does_not_depend_on_a_worker_being_free_right_now(monkeypatch):
    """⚠ Registration, not liveness — and the distinction is the whole fix.

    Gating submissions on a live ACTIVE worker looked stricter and was wrong:
    worker status moves moment to moment, and a healthy deployment with a busy
    or briefly-restarting worker refused runs it should have accepted. It was
    caught by a conformance run failing mid-suite *with a worker running*.

    A rejected submission destroys that request's work; a momentarily missing
    worker only delays it, because the engine queues.
    """
    _engine(
        monkeypatch,
        workflows=["reimagina-run-vabc"],
        workers=[_Worker("WorkerStatus.INACTIVE", ["reimagina-run-vabc:plan"])],
    )
    assert ho._graph_is_deployed(_declared("reimagina-run-vabc")) is True


def test_an_unreadable_workflow_list_admits_the_run(monkeypatch):
    """⚠ Open, not closed, and deliberately.

    This is an admission check, not a safety property — the engine holding the
    run is the safety property. A transient listing failure must not become a
    submission outage for work that would have executed perfectly well.
    """
    from types import SimpleNamespace

    def _boom():
        raise OSError("engine listing unavailable")

    monkeypatch.setattr(
        hatchet_app,
        "get_hatchet",
        lambda: SimpleNamespace(workflows=SimpleNamespace(list=_boom)),
    )
    assert ho._graph_is_deployed(_declared("reimagina-run-vabc")) is True


# ── readiness uses the stricter check ────────────────────────────────────────


def test_readiness_requires_a_live_worker(monkeypatch):
    _engine(
        monkeypatch,
        workers=[_Worker("WorkerStatus.ACTIVE", ["reimagina-run-vabc:plan"])],
    )
    assert ho._live_worker_for("reimagina-run-vabc") is True


def test_readiness_rejects_an_expired_registration(monkeypatch):
    """A registration is history; only a live worker is capacity."""
    _engine(
        monkeypatch,
        workers=[_Worker("WorkerStatus.INACTIVE", ["reimagina-run-vabc:plan"])],
    )
    assert ho._live_worker_for("reimagina-run-vabc") is False


def test_readiness_fails_closed_when_it_cannot_tell(monkeypatch):
    """⚠ The opposite bias to admission, for the opposite consequence.

    Failing readiness takes the instance out of rotation, which is recoverable
    and reversible. A probe that cannot tell should do that rather than claim
    everything is fine.
    """
    from types import SimpleNamespace

    def _boom():
        raise OSError("engine listing unavailable")

    monkeypatch.setattr(
        hatchet_app,
        "get_hatchet",
        lambda: SimpleNamespace(workers=SimpleNamespace(list=_boom)),
    )
    assert ho._live_worker_for("reimagina-run-vabc") is False


def test_a_deployment_mismatch_is_not_reported_as_an_engine_outage():
    """Separate types, because they send an operator to different places.

    `HatchetUnavailable` means "look at the engine" and is retryable.
    `GraphVersionUnavailable` means "finish the rollout" and is not — retrying
    cannot conjure a worker for a graph nobody deployed.
    """
    assert not issubclass(ho.GraphVersionUnavailable, ho.HatchetUnavailable)
    assert issubclass(ho.GraphVersionUnavailable, RuntimeError)

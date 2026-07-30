"""The read path — where a run's status comes from, and what it costs to get it.

Hatchet's status enum has five values and cannot say `partial` or `skipped`.
Neither of those needs a second status store, and both used to have one:

* `partial` is a *roll-up* of unit outcomes, not a stored fact. `runs.get`
  reports each task's own status, including CANCELLED, so `overall_status` —
  the same function the `sync` implementation uses — derives it on read.
* `skipped` is genuinely invisible: a unit skipped for being non-dirty reports
  COMPLETED. `ctx.was_skipped` is available only inside a finalizer, so the
  finalizer survives, emitting the skip list and nothing else.

These pin the reading half. Engine behaviour itself belongs in the qualification
suite — a unit test that mocked the engine into agreeing with us proves nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reimagina.orchestration.hatchet_orchestrator import (
    HatchetOrchestrator,
    HatchetUnavailable,
    _skipped_of,
    _units_of,
)


def _details(status: str, output: object = None, units: list | None = None):
    """Shaped like `V1WorkflowRunDetails`: `.run`, `.shape` and `.tasks`.

    ⚠ `shape` and `tasks` join on `task_external_id`, and `shape` carries
    `task_name` — the name as *declared*. The engine's `display_name` mangling
    (`compactness-1785250189293`, `summarise_failure-on-failure`) never appears
    on this path, which is the point of using it.
    """
    units = units or []
    return SimpleNamespace(
        run=SimpleNamespace(status=status, output=output),
        shape=[
            SimpleNamespace(task_name=name, task_external_id=f"ext-{i}")
            for i, (name, _s, _e) in enumerate(units)
        ],
        tasks=[
            SimpleNamespace(
                task_external_id=f"ext-{i}", status=status_, error_message=err
            )
            for i, (_n, status_, err) in enumerate(units)
        ],
    )


@pytest.fixture
def patched(monkeypatch):
    """Swap the engine for a canned answer; everything else is real code."""

    def _install(details):
        import reimagina.orchestration.hatchet_app as app

        monkeypatch.setattr(
            app,
            "hatchet",
            SimpleNamespace(runs=SimpleNamespace(get=lambda _id: details)),
        )

    return _install


# ── The claim the redesign rests on ──────────────────────────────────────────


def test_partial_is_rolled_up_from_native_task_outcomes(patched):
    """A FAILED run with one surviving unit reports `partial`.

    No finalizer verdict involved: the engine says one task COMPLETED and one
    FAILED, and `overall_status` — shared with `sync` and Track A — turns that
    into `partial`. The enum never had to express it.
    """
    patched(
        _details(
            "FAILED",
            units=[("a", "COMPLETED", None), ("b", "FAILED", "RuntimeError: boom")],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert run.status == "partial"
    assert {u.indicator: u.status for u in run.units} == {
        "a": "succeeded",
        "b": "failed",
    }
    assert run.units[1].detail == "RuntimeError: boom"


def test_a_cancelled_dependent_is_read_not_inferred(patched):
    """The engine states CANCELLED; nothing has to deduce it.

    This used to be inferred from "no error, not skipped, and `task_output`
    raised" — and the inference was wrong once, reporting a composite that never
    ran as `succeeded`, which is a value that was never computed presented as a
    good one.
    """
    patched(
        _details(
            "FAILED",
            units=[
                ("doomed", "FAILED", "boom"),
                ("dependent", "CANCELLED", None),
                ("independent", "COMPLETED", None),
            ],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert {u.indicator: u.status for u in run.units} == {
        "doomed": "failed",
        "dependent": "cancelled",
        "independent": "succeeded",
    }
    assert run.status == "partial"  # `independent` survived


def test_skipped_survives_a_completed_task_status(patched):
    """The one fact the engine will not hand back: a skip reads as COMPLETED."""
    patched(
        _details(
            "COMPLETED",
            output={"skipped": ["not_dirty"]},
            units=[("computed", "COMPLETED", None), ("not_dirty", "COMPLETED", None)],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert {u.indicator: u.status for u in run.units} == {
        "computed": "succeeded",
        "not_dirty": "skipped",
    }


def test_an_all_skipped_run_is_not_reported_as_success(patched):
    """Nothing ran, so the run did not achieve its purpose.

    ⚠ Behaviour change. The deleted in-finalizer roll-up folded "no failures
    seen" straight to `succeeded`, so an all-skipped run was `succeeded` here
    and `failed` under `sync` — the same product API answering two ways. There
    is one roll-up now, and `port.overall_status` is explicit that all-skipped
    is `failed`.
    """
    patched(
        _details(
            "COMPLETED",
            output={"skipped": ["a", "b"]},
            units=[("a", "COMPLETED", None), ("b", "COMPLETED", None)],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert [u.status for u in run.units] == ["skipped", "skipped"]
    assert run.status == "failed"


# ── While the run is still open ──────────────────────────────────────────────


def test_an_in_flight_run_reports_the_engine_status_not_a_rollup(patched):
    """⚠ The reason the status branch cannot be collapsed.

    `overall_status` is a *terminal* roll-up with no `running` case: given
    `{"running"}` it falls through every branch and returns **failed**. Rolling
    up an open run would report every one of them as failed.
    """
    patched(
        _details(
            "RUNNING",
            units=[("compactness", "COMPLETED", None), ("density", "RUNNING", None)],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert run.status == "running"
    assert {u.indicator: u.status for u in run.units} == {
        "compactness": "succeeded",
        "density": "running",
    }


def test_a_unit_with_no_task_row_yet_is_queued_not_missing(patched):
    """`shape` is the declared graph, so the unit list is stable from the start.

    Building it from `tasks` alone meant units blinked into existence as the
    engine created rows, and a caller polling early saw a shorter run than the
    one it submitted.
    """
    details = _details("RUNNING", units=[("early", "RUNNING", None)])
    details.shape.append(
        SimpleNamespace(task_name="not_started_yet", task_external_id="ext-99")
    )
    patched(details)

    run = HatchetOrchestrator().get_run(None, "r1")

    assert {u.indicator: u.status for u in run.units} == {
        "early": "running",
        "not_started_yet": "queued",
    }


def test_scaffolding_is_not_reported_as_an_indicator(patched):
    """`plan` is ours, not a unit a caller asked for.

    The finalizers no longer need filtering by name: `shape` reports declared
    names, so the engine's `-on-failure` rename never reaches this code.
    """
    patched(
        _details(
            "RUNNING",
            units=[("plan", "COMPLETED", None), ("compactness", "RUNNING", None)],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")

    assert [u.indicator for u in run.units] == ["compactness"]


# ── The helpers, where the sharp edges are ───────────────────────────────────


def test_skipped_of_ignores_output_that_is_not_a_skip_list():
    """`plan` returns `{"dirty": [...]}`, and a run in flight serves it as output.

    Reading that as a skip list would mark arbitrary units skipped.
    """
    assert _skipped_of(SimpleNamespace(output={"dirty": ["a", "b"]})) == frozenset()
    assert _skipped_of(SimpleNamespace(output=None)) == frozenset()
    assert _skipped_of(SimpleNamespace(output="not a dict")) == frozenset()
    assert _skipped_of(SimpleNamespace(output={"skipped": "a"})) == frozenset()
    assert _skipped_of(SimpleNamespace(output={"skipped": ["a"]})) == frozenset({"a"})


def test_units_join_shape_to_tasks_by_external_id_not_by_order():
    """The join key is explicit, because list order is not a contract."""
    details = SimpleNamespace(
        shape=[
            SimpleNamespace(task_name="first", task_external_id="ext-a"),
            SimpleNamespace(task_name="second", task_external_id="ext-b"),
        ],
        # Deliberately reversed relative to `shape`.
        tasks=[
            SimpleNamespace(
                task_external_id="ext-b", status="FAILED", error_message="b"
            ),
            SimpleNamespace(
                task_external_id="ext-a", status="COMPLETED", error_message=None
            ),
        ],
    )

    units = _units_of(details, skipped=frozenset())

    assert {u.indicator: u.status for u in units} == {
        "first": "succeeded",
        "second": "failed",
    }


def test_get_run_only_treats_a_real_404_as_unknown(monkeypatch):
    """An engine outage must not be converted into a misleading API 404."""
    import reimagina.orchestration.hatchet_app as app

    monkeypatch.setattr(
        app,
        "hatchet",
        SimpleNamespace(
            runs=SimpleNamespace(get=lambda _id: (_ for _ in ()).throw(OSError("down")))
        ),
    )
    with pytest.raises(HatchetUnavailable):
        HatchetOrchestrator().get_run(None, "r1")


def test_retry_taxonomy_excludes_deterministic_runner_errors():
    """Only database transport/contention failures should spend retry budget."""
    import psycopg2

    from reimagina.orchestration.hatchet_app import _is_retryable

    assert _is_retryable(psycopg2.OperationalError("connection reset"))
    assert not _is_retryable(ValueError("bad indicator definition"))

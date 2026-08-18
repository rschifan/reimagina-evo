"""Unit tests for the synchronous orchestrator behind the port — pure, no DB.

A None connection makes runstore no-op (persistence skipped), so the
orchestrator runs entirely in-memory with fake runners here. The wave-scheduler
tests live in test_wave_scheduler.py.
"""

from __future__ import annotations

import asyncio

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.synchronous import SynchronousOrchestrator


def _spec(slug, *, reads=(), depends_on=(), kind="direct"):
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind=kind,
        value_type="numeric",
        aggregation_method="weighted_mean",
        applies_to=("block",),
        reads=reads,
        depends_on=depends_on,
    )


GREEN = _spec("green_coverage", reads=("land_use",))
WALK = _spec("walkability", reads=("land_use",))
LIVE = _spec(
    "liveability_index",
    kind="composite",
    depends_on=(("walkability", 0.5), ("green_coverage", 0.5)),
)
REG = (GREEN, WALK, LIVE)


def _runner(log, name, *, fail=False):
    def run(conn, *, features, scenario_id, code_version, aoi_id):
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    return run


def test_synchronous_orchestrator_runs_and_reports_status():
    log: list[str] = []
    runners = {
        s: _runner(log, s)
        for s in ("green_coverage", "walkability", "liveability_index")
    }
    handle = SynchronousOrchestrator(registry=REG, runners=runners).submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )
    assert handle.status is not None
    assert handle.status.status == "succeeded"
    assert {u.indicator for u in handle.status.units} == {
        "green_coverage",
        "walkability",
        "liveability_index",
    }
    assert log.index("green_coverage") < log.index("liveability_index")


def test_synchronous_orchestrator_reports_partial_on_failure():
    log: list[str] = []
    runners = {
        "green_coverage": _runner(log, "green_coverage"),
        "walkability": _runner(log, "walkability", fail=True),
        "liveability_index": _runner(log, "liveability_index"),
    }
    handle = SynchronousOrchestrator(registry=REG, runners=runners).submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )
    assert handle.status.status == "partial"
    by_status = {u.indicator: u.status for u in handle.status.units}
    assert by_status["walkability"] == "failed"
    assert by_status["liveability_index"] == "skipped"


def test_get_run_is_none_for_an_unknown_run():
    orch = SynchronousOrchestrator(registry=REG, runners={})
    assert orch.get_run(None, "missing") is None


def test_submitted_run_round_trips_through_the_default_store():
    """The in-memory store makes submit → get_run work with no database.

    Previously the orchestrator wrote straight to jobs_runtime, so a run
    submitted on a None connection silently vanished and get_run returned None.
    """
    log: list[str] = []
    runners = {
        s: _runner(log, s)
        for s in ("green_coverage", "walkability", "liveability_index")
    }
    orch = SynchronousOrchestrator(registry=REG, runners=runners)
    handle = orch.submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )

    fetched = orch.get_run(None, handle.run_id)
    assert fetched is not None
    assert fetched.status == "succeeded"
    assert {u.indicator for u in fetched.units} == {
        "green_coverage",
        "walkability",
        "liveability_index",
    }


def _collect(agen):
    async def _run():
        return [event async for event in agen]

    return asyncio.run(_run())


def test_cancel_is_always_false_here():
    """No run is ever in flight: submit_run does not return until it is over."""
    orch = SynchronousOrchestrator(registry=REG, runners={})
    handle = orch.submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )
    assert orch.cancel_run(None, handle.run_id) is False


def test_subscribe_progress_yields_the_final_status_then_stops():
    log: list[str] = []
    runners = {
        s: _runner(log, s)
        for s in ("green_coverage", "walkability", "liveability_index")
    }
    orch = SynchronousOrchestrator(registry=REG, runners=runners)
    handle = orch.submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )

    events = _collect(orch.subscribe_progress(handle.run_id))

    assert len(events) == 1
    assert events[0].indicator is None  # run-level: the event that closes a stream
    assert events[0].status == "succeeded"


def test_subscribe_progress_reports_unknown_for_a_missing_run():
    """A subscriber must terminate on a bad id, not block forever."""
    orch = SynchronousOrchestrator(registry=REG, runners={})
    events = _collect(orch.subscribe_progress("missing"))
    assert [e.status for e in events] == ["unknown"]


def test_subscribe_progress_works_with_a_connection_bound_store():
    """The port gives subscribe_progress no connection, so it must not need one.

    It used to ask the store with ``conn=None``. That is fine for the in-memory
    default and silently wrong for any store that talks to a database: the
    Postgres store no-ops on a None connection, so every run — including one
    just finished — reported ``unknown``.
    """

    class _ConnectionBoundStore:
        """Rejects a None connection, the way a real database store no-ops on one."""

        def create_run(self, conn, **kw):
            assert conn is not None
            return True

        def record_units(self, conn, **kw):
            assert conn is not None

        def finalize_run(self, conn, **kw):
            assert conn is not None

        def get_run(self, conn, run_id):
            assert conn is not None, "subscribe_progress must not reach the store"
            return None

    runners = {
        s: _runner([], s)
        for s in ("green_coverage", "walkability", "liveability_index")
    }
    orch = SynchronousOrchestrator(
        registry=REG, runners=runners, store=_ConnectionBoundStore()
    )
    handle = orch.submit_run(
        object(), changed_attributes={"land_use"}, edited_features={"b1"}
    )

    events = _collect(orch.subscribe_progress(handle.run_id))
    assert [e.status for e in events] == ["succeeded"]


def test_store_is_injectable():
    """Any object with the runstore signatures can back the orchestrator."""
    calls: list[str] = []

    class _RecordingStore:
        def create_run(self, conn, **kw):
            calls.append("create_run")
            return True

        def record_units(self, conn, **kw):
            calls.append("record_units")

        def finalize_run(self, conn, **kw):
            calls.append("finalize_run")

        def get_run(self, conn, run_id):
            return None

    SynchronousOrchestrator(
        registry=REG, runners={}, store=_RecordingStore()
    ).submit_run(None, changed_attributes={"land_use"}, edited_features={"b1"})

    assert calls == ["create_run", "record_units", "finalize_run"]

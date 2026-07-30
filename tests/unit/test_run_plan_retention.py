"""A plan is deleted only when nothing can ever read it again.

The old rule was "delete at terminal state, sweep the rest by age", and both
halves were wrong. A terminal run is exactly the run `runs.replay()` re-executes
— from the original input, which on this track is a `plan_id` handle — so
reclaiming at terminal state destroyed the input of every replayable run. And
the age sweep ran on the *submit* path with a 24 h horizon against the engine's
720 h retention, so an ordinary busy afternoon could delete a live run's plan.

What is left is two provable cases and one bias. The bias is the important part:
deleting a plan that a run still needs is unrecoverable and fails every
remaining unit; keeping a plan nothing needs costs one row until the scheduled
retention job runs. So anything ambiguous retains.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import hatchet_orchestrator as ho


class _Code:
    """Stands in for a grpc.StatusCode, which is an enum with a `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


class _RpcError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._name = name

    def code(self):
        return _Code(self._name)


@pytest.mark.parametrize(
    "status",
    [
        "INVALID_ARGUMENT",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "NOT_FOUND",
    ],
)
def test_a_refused_submission_is_provably_not_accepted(status):
    """The engine rejected the message; no run exists to read the plan."""
    assert ho._is_definitely_not_accepted(_RpcError(status)) is True


@pytest.mark.parametrize(
    "status",
    [
        "DEADLINE_EXCEEDED",  # the run may have been created after we gave up
        "UNAVAILABLE",  # ...or the connection dropped after it was accepted
        "INTERNAL",
        "UNKNOWN",
        "ABORTED",
    ],
)
def test_an_ambiguous_failure_retains_the_plan(status):
    """These are the statuses that a retry deduplicates onto an accepted run.

    That only works if the plan is still there for the accepted run to read.
    """
    assert ho._is_definitely_not_accepted(_RpcError(status)) is False


def test_an_exception_with_no_readable_status_retains():
    """An unrecognised failure is the ambiguous case, not the refused one."""
    assert ho._is_definitely_not_accepted(RuntimeError("something went wrong")) is False


def test_a_status_that_raises_when_read_retains():
    """Defensive: `.code()` is duck-typed, so it is allowed to be hostile."""

    class _Hostile(Exception):
        def code(self):
            raise ValueError("no status here")

    assert ho._is_definitely_not_accepted(_Hostile()) is False


def _discard_recorder(monkeypatch):
    """Capture `_discard_plan` calls without touching a database."""
    discarded: list[str] = []
    monkeypatch.setattr(
        ho, "_discard_plan", lambda dsn, plan_id: discarded.append(plan_id)
    )
    return discarded


def test_a_collision_discards_the_plan_it_just_wrote(monkeypatch):
    """The accepted run carries the *earlier* submit's handle.

    The plan minted by this call is unreferenced the moment the window collapses
    the two submits onto one run, so it is an orphan by construction — the one
    case where deleting immediately is provably safe.
    """
    from hatchet_sdk.exceptions import IdempotencyCollisionError

    discarded = _discard_recorder(monkeypatch)

    collision = IdempotencyCollisionError.__new__(IdempotencyCollisionError)
    collision.existing_run_external_id = "run-already-running"

    handle = _submit_with(monkeypatch, raises=collision)

    assert handle.run_id == "run-already-running"
    assert discarded == ["plan-under-test"], discarded


def test_an_ambiguous_trigger_failure_keeps_the_plan(monkeypatch):
    """A deadline may still have produced a run. Keep its input."""
    discarded = _discard_recorder(monkeypatch)

    with pytest.raises(_RpcError):
        _submit_with(monkeypatch, raises=_RpcError("DEADLINE_EXCEEDED"))

    assert discarded == [], discarded


def test_a_refused_trigger_discards_the_plan(monkeypatch):
    """The engine never took it, so nothing will ever read the handle."""
    discarded = _discard_recorder(monkeypatch)

    with pytest.raises(_RpcError):
        _submit_with(monkeypatch, raises=_RpcError("INVALID_ARGUMENT"))

    assert discarded == ["plan-under-test"], discarded


def _submit_with(monkeypatch, *, raises: BaseException):
    """Drive `submit_run` to the trigger, then fail it in a chosen way.

    Everything before the trigger is stubbed: this is about what happens to the
    plan afterwards, not about planning or persistence, which
    `test_run_plan_ordering.py` already pins.
    """
    from reimagina.orchestration import hatchet_app
    from reimagina.orchestration.impact import DirtyUnit
    from reimagina.orchestration.plan import RunPlan

    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")
    monkeypatch.setattr(ho, "assert_same_domain_database", lambda *a, **k: None)
    monkeypatch.setattr(
        ho,
        "plan_run",
        lambda *a, **k: RunPlan(
            units=[DirtyUnit("slug", frozenset({"f-1"}))],
            params={"scenario_id": None, "aoi_id": "aoi", "code_version": "v1"},
        ),
    )
    monkeypatch.setattr(ho, "_persist_plan", lambda *a, **k: "plan-under-test")

    class _Workflow:
        def run(self, *_a, **_k):
            raise raises

    # Not about admission control: this suite drives the plan/payload path, so
    # the "is a worker serving this graph?" gate is stubbed. Its own behaviour is
    # covered in `test_graph_version.py`.
    monkeypatch.setattr(ho, "_graph_is_deployed", lambda _name: True)
    monkeypatch.setattr(hatchet_app, "get_run_workflow", lambda: _Workflow())

    return ho.HatchetOrchestrator().submit_run(
        object(),
        changed_attributes=["geometry"],
        edited_features=["f-1"],
        aoi_id="aoi",
        code_version="v1",
    )

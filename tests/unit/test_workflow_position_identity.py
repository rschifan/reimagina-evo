"""The exact step-position sequence ``run_workflow`` emits, pinned.

DBOS matches replayed operations by ``(parent_workflow_id, function_id)``, and
the counter is consumed by far more than ``@DBOS.step`` — ``DBOS.write_stream``,
``handle.get_result``, ``DBOS.wait_first`` and every ``enqueue`` each take one
too (``dbos/_context.py``, ``dbos/_sys_db.py``). So ``_emit`` is *not*
position-free, and any change to how many calls the body makes per unit shifts
every position after it.

⚠ **The failure this guards is silent.** ``DBOSUnexpectedStepError`` fires only
when the function *name* recorded at a slot differs from the one replayed there.
Every child dispatched by ``run_workflow`` is ``unit_workflow``, so the names
always match: a reordered replay does not raise, it hands back a handle to the
*wrong* child and cross-attributes one indicator's result to another. Nothing in
DBOS detects that, which is why the sequence is asserted here rather than
described in a comment.

This file has now outlived two dispatch designs — it was written to guard
run-group dispatch, survived its removal, and is re-pinned here for as-completed
dispatch. The hazard belongs to the engine, not to any one feature.

The bodies are replayed against a recording double, so nothing here needs a
database, a queue, or DBOS itself.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import dbos_app


class _Recorder:
    """Stands in for the DBOS runtime, logging every position-consuming call."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.pending_id: str | None = None
        # Completion order, as `wait_first` would report it. Defaults to
        # enqueue order; a test sets it to prove the body follows the *winner*
        # rather than the order it dispatched in.
        self.finish_order: list[str] | None = None
        #: child workflow ids whose `get_result` should raise, i.e. units that
        #: failed. Keyed by id because that is what the body holds.
        self.failing_children: set[str] = set()
        self._enqueued: list[str] = []

    # ── the pieces run_workflow / unit_workflow reach for ──────────────────
    def s_open_run(self, run_id, units, params):
        self.calls.append("s_open_run")

    def s_set_status(self, run_id, slug, status, detail=None):
        self.calls.append(f"s_set_status:{slug}:{status}")
        return True

    def s_settle_unit(self, run_id, slug, outcome):
        self.calls.append(f"s_settle_unit:{slug}")
        return outcome

    def s_compute(self, run_id, slug, features, params):
        self.calls.append(f"s_compute:{slug}")
        if self.fail_on == slug:
            raise RuntimeError("boom")

    def s_finalize(self, run_id, status):
        self.calls.append("s_finalize")
        return True

    def s_release_plan(self, plan_id):
        self.calls.append("s_release_plan")
        return 0

    def emit(self, run_id, indicator, status, detail=None):
        self.calls.append(f"emit:{indicator}")

    def enqueue(self, *args):
        # args: (unit_workflow, run_id, slug, features, params)
        slug = args[2]
        self.calls.append(f"enqueue:{slug}")
        self._enqueued.append(slug)
        # Honour the id the body asked for, as SetWorkflowID + DBOS do on a
        # first execution. `pending_id` is set by the fake context manager.
        return _Handle(self, self.pending_id or slug)

    def close_stream(self, name):
        self.calls.append("close_stream")

    # ── DBOS classmethods the loop uses ────────────────────────────────────
    def retrieve_workflow(self, workflow_id, *, existing_workflow=True):
        """Zero function ids by contract — so it records nothing."""
        assert existing_workflow is False, (
            "retrieve_workflow must be called with existing_workflow=False; "
            "the default consumes a function id"
        )
        return _Handle(self, workflow_id)

    def wait_first(self, handles, *, polling_interval_sec: float = 1.0):
        self.calls.append("wait_first")
        ids = [h.workflow_id for h in handles]
        assert ids, "wait_first raises ValueError on an empty list"
        assert len(set(ids)) == len(ids), "wait_first rejects duplicate ids"
        by_id = {h.workflow_id: h for h in handles}
        for slug in self.finish_order or []:
            if (handle := by_id.get(f"run-{slug}")) is not None:
                self.finish_order.remove(slug)
                return handle
        return handles[0]


class _Handle:
    def __init__(self, recorder: _Recorder, workflow_id: str) -> None:
        self._recorder = recorder
        self.workflow_id = workflow_id

    def get_result(self, *, polling_interval_sec: float = 1.0):
        self._recorder.calls.append("get_result")
        if self.workflow_id in self._recorder.failing_children:
            raise RuntimeError("child failed")
        return "succeeded"


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(dbos_app, "s_open_run", rec.s_open_run)
    monkeypatch.setattr(dbos_app, "s_set_status", rec.s_set_status)
    monkeypatch.setattr(dbos_app, "s_settle_unit", rec.s_settle_unit)
    monkeypatch.setattr(dbos_app, "s_compute", rec.s_compute)
    monkeypatch.setattr(dbos_app, "s_finalize", rec.s_finalize)
    monkeypatch.setattr(dbos_app, "s_release_plan", rec.s_release_plan)
    monkeypatch.setattr(dbos_app, "_emit", rec.emit)
    monkeypatch.setattr(dbos_app, "units_queue", lambda: rec)

    class _FakeSetWorkflowID:
        def __init__(self, workflow_id):
            rec.pending_id = workflow_id

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            rec.pending_id = None
            return False

    monkeypatch.setattr(dbos_app, "SetWorkflowID", _FakeSetWorkflowID)

    class _DBOS:
        close_stream = staticmethod(rec.close_stream)
        retrieve_workflow = staticmethod(rec.retrieve_workflow)
        wait_first = staticmethod(rec.wait_first)

    monkeypatch.setattr(dbos_app, "DBOS", _DBOS)
    return rec


def _unit(slug, *, deps=None):
    return {"slug": slug, "features": [], "deps": deps or []}


def _body(run_workflow):
    """The undecorated function, so it runs without a DBOS context."""
    return getattr(run_workflow, "__wrapped__", run_workflow)


# ── The property ────────────────────────────────────────────────────────────


#: The exact position-consuming sequence for a two-unit run. Both units are
#: enqueued up front — they have no dependencies — then settled one at a time as
#: `wait_first` reports each winner. Four positions per completion:
#: wait_first, get_result, s_settle_unit, emit.
_DISPATCH_SEQUENCE = [
    "s_open_run",
    "enqueue:a",
    "enqueue:b",
    "wait_first",
    "get_result",
    "s_settle_unit:a",
    "emit:a",
    "wait_first",
    "get_result",
    "s_settle_unit:b",
    "emit:b",
    "s_finalize",
    "s_release_plan",
    "emit:None",
    "close_stream",
]


def test_run_workflow_emits_the_pinned_sequence(recorder):
    """Change this sequence and every run in flight at the deploy breaks."""
    _body(dbos_app.run_workflow)(
        "run", [_unit("a"), _unit("b")], {"code_version": "test"}
    )
    assert recorder.calls == _DISPATCH_SEQUENCE


def test_settles_in_completion_order_not_dispatch_order(recorder):
    """The whole point of as-completed: `b` finishing first settles first.

    Under the wave design this was impossible — units were awaited in enqueue
    order — so this test is what distinguishes the two implementations.
    """
    recorder.finish_order = ["b", "a"]
    _body(dbos_app.run_workflow)(
        "run", [_unit("a"), _unit("b")], {"code_version": "test"}
    )
    assert [c for c in recorder.calls if c.startswith("s_settle_unit:")] == [
        "s_settle_unit:b",
        "s_settle_unit:a",
    ]
    # Positions are unchanged in count and kind, only in which slug they carry —
    # which is exactly what `wait_first` makes durable.
    assert recorder.calls.count("wait_first") == 2
    assert len(recorder.calls) == len(_DISPATCH_SEQUENCE)


def test_a_dependent_is_not_dispatched_before_its_input_settles(recorder):
    """`b` depends on `a`, so only `a` is enqueued in the first pass."""
    _body(dbos_app.run_workflow)(
        "run", [_unit("a"), _unit("b", deps=["a"])], {"code_version": "test"}
    )
    assert recorder.calls == [
        "s_open_run",
        "enqueue:a",
        "wait_first",
        "get_result",
        "s_settle_unit:a",
        "emit:a",
        "enqueue:b",
        "wait_first",
        "get_result",
        "s_settle_unit:b",
        "emit:b",
        "s_finalize",
        "s_release_plan",
        "emit:None",
        "close_stream",
    ]


def test_children_are_enqueued_under_deterministic_ids(recorder):
    """`{run_id}-{slug}`, which is what makes a re-enqueue dedup instead of
    minting a second child, and what lets the body detect a bound-slot shift."""
    _body(dbos_app.run_workflow)(
        "run", [_unit("a"), _unit("b")], {"code_version": "test"}
    )
    assert recorder._enqueued == ["a", "b"]


def test_a_shifted_child_binding_raises_rather_than_cross_wiring(recorder, monkeypatch):
    """The tripwire for the one failure DBOS cannot detect.

    On replay DBOS resolves a child by position and returns the *recorded* id,
    silently discarding the one the body asked for. The function names match —
    every child is `unit_workflow` — so `DBOSUnexpectedStepError` never fires.
    Comparing the handle back against the requested id is the only detection.
    """

    class _WrongIdQueue:
        def enqueue(self, *args):
            recorder.enqueue(*args)
            return _Handle(recorder, "run-a-different-child")

    monkeypatch.setattr(dbos_app, "units_queue", _WrongIdQueue)

    with pytest.raises(RuntimeError, match="replay divergence"):
        _body(dbos_app.run_workflow)("run", [_unit("a")], {"code_version": "test"})


def test_a_failure_skips_the_whole_chain_below_it(recorder):
    """Transitive skip propagation, two levels deep.

    ⚠ Regression guard. `plan_wave` computes `finished` once per call, so it
    marks exactly **one layer** of skips per pass: with `a -> b -> c` and `a`
    failed, the first pass skips `b`, and only the *next* pass can see `b` as
    terminal and skip `c`. A loop that stops as soon as nothing is running exits
    after the first pass and strands `c` at `queued` — on a run the API reports
    as finished, since `overall_status` returns `failed` either way. The run
    looks right and one indicator silently never resolves, which is exactly the
    per-indicator status guarantee this platform sells.

    No shipped DAG catches this: the real catalog's composite depends directly
    on roots, and the `partial_failure` workload has a single edge. It needs a
    chain of two below the failure, which is what this builds.
    """
    recorder.failing_children.add("run-a")
    _body(dbos_app.run_workflow)(
        "run",
        [_unit("a"), _unit("b", deps=["a"]), _unit("c", deps=["b"])],
        {"code_version": "test"},
    )
    settled = [c for c in recorder.calls if c.startswith("s_settle_unit:")]
    assert settled == ["s_settle_unit:a", "s_settle_unit:b", "s_settle_unit:c"]
    # Every unit announced, so the progress stream matches the stored statuses.
    assert [c for c in recorder.calls if c.startswith("emit:")] == [
        "emit:a",
        "emit:b",
        "emit:c",
        "emit:None",
    ]


# ── unit_workflow ───────────────────────────────────────────────────────────


def test_unit_workflow_sequence(recorder):
    """Running, compute, succeeded — three positions, in that order."""
    _body(dbos_app.unit_workflow)("run-3", "solo", [], {})
    assert recorder.calls == [
        "s_set_status:solo:running",
        "s_compute:solo",
        "s_set_status:solo:succeeded",
    ]


def test_a_failing_compute_marks_failed_and_reraises(monkeypatch):
    """The parent branches on the raise, so it must not be swallowed."""
    rec = _Recorder(fail_on="m1")
    monkeypatch.setattr(dbos_app, "s_set_status", rec.s_set_status)
    monkeypatch.setattr(dbos_app, "s_compute", rec.s_compute)

    with pytest.raises(RuntimeError):
        _body(dbos_app.unit_workflow)("run-4", "m1", [], {})

    assert "s_set_status:m1:failed" in rec.calls
    assert not [c for c in rec.calls if c.endswith(":succeeded")]

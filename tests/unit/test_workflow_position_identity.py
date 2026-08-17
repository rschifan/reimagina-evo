"""The exact step-position sequence ``run_workflow`` emits, pinned.

DBOS matches replayed steps by ``(position, function name)``, and the counter is
consumed by more than ``@DBOS.step`` — ``DBOS.write_stream`` and
``handle.get_result`` each take one too (``dbos/_context.py``,
``dbos/_sys_db.py``). So ``_emit`` is *not* position-free, and any change to how
many calls the body makes per unit shifts every position after it.

A run in flight when that happens cannot resume: it fails on replay with
``DBOSUnexpectedStepError``, or worse, hands one indicator's checkpointed output
to another. Neither is visible from reading a diff, which is why the sequence is
asserted here rather than described in a comment.

This file was written to guard the introduction of run-group dispatch and
survived its removal, because the hazard belongs to DBOS rather than to that
feature: any future edit to the dispatch loop faces it again.

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
        return _Handle(self)

    def close_stream(self, name):
        self.calls.append("close_stream")


class _Handle:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def get_result(self, *, polling_interval_sec: float = 1.0):
        self._recorder.calls.append("get_result")
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

    class _DBOS:
        close_stream = staticmethod(rec.close_stream)

    monkeypatch.setattr(dbos_app, "DBOS", _DBOS)
    return rec


def _unit(slug, *, deps=None):
    return {"slug": slug, "features": [], "deps": deps or []}


def _body(run_workflow):
    """The undecorated function, so it runs without a DBOS context."""
    return getattr(run_workflow, "__wrapped__", run_workflow)


# ── The property ────────────────────────────────────────────────────────────


#: The exact position-consuming sequence for a two-unit run. One enqueue per
#: unit, then per unit a `get_result`, a settle and an emit — in that order,
#: awaited in enqueue order because child workflow ids are positional.
_DISPATCH_SEQUENCE = [
    "s_open_run",
    "enqueue:a",
    "enqueue:b",
    "get_result",
    "s_settle_unit:a",
    "emit:a",
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
        "run-1", [_unit("a"), _unit("b")], {"code_version": "test"}
    )
    assert recorder.calls == _DISPATCH_SEQUENCE


def test_units_are_awaited_in_enqueue_order(recorder):
    """An as-completed loop would shift child ids and mint duplicate children."""
    _body(dbos_app.run_workflow)(
        "run-2",
        [_unit("a"), _unit("b"), _unit("c")],
        {"code_version": "test"},
    )
    assert [c for c in recorder.calls if c.startswith("enqueue:")] == [
        "enqueue:a",
        "enqueue:b",
        "enqueue:c",
    ]
    assert [c for c in recorder.calls if c.startswith("s_settle_unit:")] == [
        "s_settle_unit:a",
        "s_settle_unit:b",
        "s_settle_unit:c",
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

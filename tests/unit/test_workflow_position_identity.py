"""Grouping must not move a single step position for an ungrouped run.

DBOS matches replayed steps by ``(position, function name)``, and the counter is
consumed by more than ``@DBOS.step`` — ``DBOS.write_stream`` and
``handle.get_result`` each take one too (``dbos/_context.py``,
``dbos/_sys_db.py``). So ``_emit`` is *not* position-free, and any change to how
many calls the body makes per unit shifts every position after it.

Every run in flight at the deploy that introduced grouping was submitted before
``UnitInput`` had a ``group`` key, so each of its units is a singleton group. If
the grouped body emits a different sequence for that input, those runs cannot
resume: they fail on replay with ``DBOSUnexpectedStepError``, or worse, hand one
indicator's checkpointed output to another.

This asserts the sequence rather than trusting the comment in ``run_workflow``.
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
        # args: (unit_workflow, run_id, slug, features, params[, produces])
        slug = args[2]
        self.calls.append(f"enqueue:{slug}")
        return _Handle(self)

    def close_stream(self, name):
        self.calls.append("close_stream")


class _Handle:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def get_result(self):
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


def _unit(slug, *, deps=None, group=None):
    unit = {"slug": slug, "features": [], "deps": deps or []}
    if group is not None:
        unit["group"] = group
    return unit


def _body(run_workflow):
    """The undecorated function, so it runs without a DBOS context."""
    return getattr(run_workflow, "__wrapped__", run_workflow)


# ── The property ────────────────────────────────────────────────────────────


#: The exact position-consuming sequence for a two-unit, ungrouped run — the
#: shape every in-flight run was checkpointed against before grouping existed.
_PRE_GROUPING_SEQUENCE = [
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


def test_ungrouped_input_emits_the_pre_grouping_sequence(recorder):
    """A run submitted before `group` existed must replay unchanged."""
    _body(dbos_app.run_workflow)(
        "run-1", [_unit("a"), _unit("b")], {"code_version": "test"}
    )
    assert recorder.calls == _PRE_GROUPING_SEQUENCE


def test_singleton_groups_match_ungrouped_exactly(recorder):
    """An explicit group per slug is the same shape as no groups at all.

    Asserted against the golden sequence rather than against a second live run:
    comparing two runs would still pass if both drifted together, which is the
    one failure this file exists to catch.
    """
    _body(dbos_app.run_workflow)(
        "run-2",
        [_unit("a", group="a"), _unit("b", group="b")],
        {"code_version": "test"},
    )
    assert recorder.calls == _PRE_GROUPING_SEQUENCE


def test_a_group_dispatches_once_and_settles_per_member(recorder):
    """Three indicators, one enqueue, one get_result — three settles and emits."""
    units = [
        _unit("x1", group="g"),
        _unit("x2", group="g"),
        _unit("x3", group="g"),
    ]
    _body(dbos_app.run_workflow)("run-4", units, {"code_version": "test"})

    assert recorder.calls.count("enqueue:x1") == 1
    assert [c for c in recorder.calls if c.startswith("enqueue")] == ["enqueue:x1"]
    assert recorder.calls.count("get_result") == 1
    assert [c for c in recorder.calls if c.startswith("s_settle_unit")] == [
        "s_settle_unit:x1",
        "s_settle_unit:x2",
        "s_settle_unit:x3",
    ]
    # Ask 4 is preserved: one event per indicator, not one per dispatched unit.
    assert [c for c in recorder.calls if c.startswith("emit:x")] == [
        "emit:x1",
        "emit:x2",
        "emit:x3",
    ]


# ── unit_workflow ───────────────────────────────────────────────────────────


def test_unit_workflow_without_produces_is_unchanged(recorder):
    """The replay path for a child persisted before `produces` existed."""
    _body(dbos_app.unit_workflow)("run-5", "solo", [], {})
    assert recorder.calls == [
        "s_set_status:solo:running",
        "s_compute:solo",
        "s_set_status:solo:succeeded",
    ]


def test_unit_workflow_computes_each_member(recorder):
    """Group members are different indicators, so each needs its own compute."""
    _body(dbos_app.unit_workflow)("run-6", "m1", [], {}, ["m1", "m2"])
    assert recorder.calls == [
        "s_set_status:m1:running",
        "s_set_status:m2:running",
        "s_compute:m1",
        "s_compute:m2",
        "s_set_status:m1:succeeded",
        "s_set_status:m2:succeeded",
    ]


def test_a_failure_fails_every_member(monkeypatch):
    """Partial success inside one dispatched unit has nowhere to live."""
    rec = _Recorder(fail_on="m2")
    monkeypatch.setattr(dbos_app, "s_set_status", rec.s_set_status)
    monkeypatch.setattr(dbos_app, "s_compute", rec.s_compute)

    with pytest.raises(RuntimeError):
        _body(dbos_app.unit_workflow)("run-7", "m1", [], {}, ["m1", "m2"])

    assert "s_set_status:m1:failed" in rec.calls
    assert "s_set_status:m2:failed" in rec.calls
    assert not [c for c in rec.calls if c.endswith(":succeeded")]


# ── validation ──────────────────────────────────────────────────────────────


def test_heterogeneous_group_is_rejected_at_submit():
    """The invariant that makes a split-wave double-compute impossible."""
    units = [
        _unit("x1", group="g"),
        _unit("x2", group="g", deps=["other"]),
        _unit("other"),
    ]
    with pytest.raises(ValueError, match="differing dependencies"):
        dbos_app.validate_workflow_input(units, {"code_version": "test"})


def test_homogeneous_group_passes():
    units = [
        _unit("x1", group="g", deps=["other"]),
        _unit("x2", group="g", deps=["other"]),
        _unit("other"),
    ]
    dbos_app.validate_workflow_input(units, {"code_version": "test"})


def test_input_without_group_still_validates():
    dbos_app.validate_workflow_input([_unit("a"), _unit("b")], {"code_version": "t"})


def test_empty_group_string_is_rejected():
    with pytest.raises(ValueError, match="invalid group"):
        dbos_app.validate_workflow_input(
            [{"slug": "a", "features": [], "deps": [], "group": ""}],
            {"code_version": "t"},
        )

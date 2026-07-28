"""The verdict path — the part of this track that is not a status translation.

Hatchet's status enum cannot say `partial` or `skipped`. Its finalizer output
can, and spike B8 confirmed `on_failure_task` runs on a half-failed run and can
read per-unit outcomes. These tests pin the *reading* half of that: that
`get_run` prefers the verdict over the enum, and that it still answers sensibly
before the finalizer has produced one.

The engine behaviour itself is verified in the spike, not here — a unit test
that mocked the engine into agreeing with us would prove nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reimagina.orchestration.hatchet_orchestrator import (
    HatchetOrchestrator,
    _strip_suffix,
    _verdict_of,
)


def _details(status: str, output: object = None, tasks: list | None = None):
    """Shaped like `V1WorkflowRunDetails`: a `.run` plus a `.tasks` list."""
    return SimpleNamespace(
        run=SimpleNamespace(status=status, output=output),
        tasks=tasks or [],
    )


def _task(name: str, status: str, error: str | None = None):
    return SimpleNamespace(display_name=name, status=status, error_message=error)


@pytest.fixture
def patched(monkeypatch):
    """Swap the engine for a canned answer; everything else is real code."""

    def _install(details):
        import reimagina.orchestration.hatchet_app as app

        monkeypatch.setattr(
            app, "hatchet", SimpleNamespace(runs=SimpleNamespace(get=lambda _id: details))
        )

    return _install


# ── The claim the redesign rests on ──────────────────────────────────────────


def test_partial_survives_a_failed_run_status(patched):
    """A FAILED run whose verdict says `partial` must report `partial`.

    This is the whole correction. The first cut of this module mapped
    FAILED -> "failed" unconditionally and documented `partial` as
    inexpressible; it is expressible, just not in the enum.
    """
    patched(
        _details(
            "FAILED",
            output={
                "overall": "partial",
                "units": {
                    "a": {"status": "succeeded", "detail": None},
                    "b": {"status": "failed", "detail": "RuntimeError: boom"},
                },
            },
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")
    assert run.status == "partial"
    assert {u.indicator: u.status for u in run.units} == {
        "a": "succeeded",
        "b": "failed",
    }
    assert run.units[1].detail == "RuntimeError: boom"


def test_skipped_survives_a_completed_task_status(patched):
    """`skipped` and `succeeded` both arrive as COMPLETED; the verdict splits them."""
    patched(
        _details(
            "COMPLETED",
            output={
                "overall": "succeeded",
                "units": {
                    "computed": {"status": "succeeded", "detail": None},
                    "not_dirty": {"status": "skipped", "detail": None},
                },
            },
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")
    assert {u.indicator: u.status for u in run.units} == {
        "computed": "succeeded",
        "not_dirty": "skipped",
    }


# ── Before the finalizer has run ─────────────────────────────────────────────


def test_falls_back_to_the_enum_while_in_flight(patched):
    """No verdict yet is normal, not an error: translate the enum instead."""
    patched(
        _details(
            "RUNNING",
            output=None,
            tasks=[
                _task("compactness-1785250189293", "COMPLETED"),
                _task("density-1785250189293", "RUNNING"),
            ],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")
    assert run.status == "running"
    assert {u.indicator: u.status for u in run.units} == {
        "compactness": "succeeded",
        "density": "running",
    }


def test_scaffolding_is_not_reported_as_an_indicator(patched):
    """`plan` and the two finalizers are ours, not units a caller asked for."""
    patched(
        _details(
            "RUNNING",
            output=None,
            tasks=[
                _task("plan-1785250189225", "COMPLETED"),
                _task("summarise_failure-1785250189647", "COMPLETED"),
                _task("compactness-1785250189293", "RUNNING"),
            ],
        )
    )
    run = HatchetOrchestrator().get_run(None, "r1")
    assert [u.indicator for u in run.units] == ["compactness"]


# ── The two helpers, where the sharp edges are ───────────────────────────────


def test_strip_suffix_only_removes_a_numeric_tail():
    # The engine appends a millisecond timestamp.
    assert _strip_suffix("urban_form_block_compactness-1785250189293") == (
        "urban_form_block_compactness"
    )
    # A slug that merely contains hyphens must survive intact.
    assert _strip_suffix("some-hyphenated-slug") == "some-hyphenated-slug"
    assert _strip_suffix("plan") == "plan"


def test_verdict_of_rejects_output_that_is_not_a_verdict():
    """`plan` returns `{"dirty": [...]}`, and a run in flight serves it as output.

    Treating that as a verdict would report status=None with zero units on
    every running workflow.
    """
    assert _verdict_of(SimpleNamespace(output={"dirty": ["a", "b"]})) is None
    assert _verdict_of(SimpleNamespace(output=None)) is None
    assert _verdict_of(SimpleNamespace(output="not a dict")) is None
    assert _verdict_of(SimpleNamespace(output={"overall": "partial"})) is None  # no units
    assert _verdict_of(SimpleNamespace(output={"overall": "x", "units": {}})) == {
        "overall": "x",
        "units": {},
    }

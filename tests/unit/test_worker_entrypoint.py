"""`python -m apps.worker` argument handling — no engine actually started."""

from __future__ import annotations

import pytest

from apps.worker.__main__ import main


def _capture_dbos(monkeypatch) -> list[bool]:
    called: list[bool] = []
    monkeypatch.setattr(
        "apps.worker.__main__.run_dbos_worker", lambda: called.append(True)
    )
    return called


def _capture_procrastinate(monkeypatch) -> dict:
    seen: dict = {}
    monkeypatch.setattr(
        "apps.worker.__main__.run_worker",
        lambda queues, **kw: seen.update(queues=queues, **kw),
    )
    return seen


def test_worker_defaults_to_the_branch_ops_plane(monkeypatch):
    """No flag means DBOS — the engine this branch exists to run."""
    called = _capture_dbos(monkeypatch)
    _capture_procrastinate(monkeypatch)

    main([])

    assert called == [True]


def test_dbos_worker_takes_no_queue_arguments(monkeypatch):
    """A durable-execution library has no queues for the operator to name.

    Concurrency is a property of the registered queue (`REIMAGINA_UNIT_CONCURRENCY`),
    not of the process, so the flags below are inert here rather than silently
    doing something different.
    """
    called = _capture_dbos(monkeypatch)
    seen = _capture_procrastinate(monkeypatch)

    main(["--queues", "compute", "--concurrency", "4"])

    assert called == [True]
    assert seen == {}  # the Procrastinate runtime was not started


def test_procrastinate_engine_still_parses_queues_and_concurrency(monkeypatch):
    """Kept runnable while the two ops-planes are compared side by side."""
    seen = _capture_procrastinate(monkeypatch)
    _capture_dbos(monkeypatch)

    main(
        [
            "--engine",
            "procrastinate",
            "--queues",
            "compute, control ",
            "--concurrency",
            "4",
        ]
    )

    assert seen == {"queues": ["compute", "control"], "concurrency": 4}


def test_procrastinate_engine_defaults_to_both_queues_at_concurrency_one(monkeypatch):
    seen = _capture_procrastinate(monkeypatch)
    _capture_dbos(monkeypatch)

    main(["--engine", "procrastinate"])

    assert seen == {"queues": None, "concurrency": 1}


def test_worker_rejects_an_unknown_engine(monkeypatch):
    _capture_dbos(monkeypatch)
    with pytest.raises(SystemExit):
        main(["--engine", "hatchet"])


def test_worker_rejects_non_integer_concurrency(monkeypatch):
    _capture_dbos(monkeypatch)
    _capture_procrastinate(monkeypatch)

    with pytest.raises(SystemExit):
        main(["--engine", "procrastinate", "--concurrency", "lots"])

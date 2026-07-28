"""`python -m apps.worker` argument handling — no engine actually started."""

from __future__ import annotations

import pytest

from apps.worker.__main__ import main


def _capture(monkeypatch) -> list[bool]:
    called: list[bool] = []
    monkeypatch.setattr(
        "apps.worker.__main__.run_dbos_worker", lambda: called.append(True)
    )
    return called


def test_worker_starts_the_dbos_ops_plane(monkeypatch):
    called = _capture(monkeypatch)
    main([])
    assert called == [True]


@pytest.mark.parametrize("flag", ["--queues", "--concurrency", "--engine"])
def test_worker_rejects_the_queue_era_flags(monkeypatch, flag):
    """These belonged to the queue-based ops-plane and must not silently no-op.

    Concurrency is now a property of the registered queue, not the process, and
    there is one queue. An operator carrying an old command line should get an
    error rather than a worker quietly ignoring what they asked for.
    """
    _capture(monkeypatch)
    with pytest.raises(SystemExit):
        main([flag, "compute"])

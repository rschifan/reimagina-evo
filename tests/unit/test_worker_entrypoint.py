from __future__ import annotations

import pytest

from apps.worker.__main__ import main


def test_worker_defaults_to_both_queues_at_concurrency_one(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "apps.worker.__main__.run_worker",
        lambda queues, **kw: seen.update(queues=queues, **kw),
    )

    main([])

    assert seen == {"queues": None, "concurrency": 1}


def test_worker_parses_queues_and_concurrency(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "apps.worker.__main__.run_worker",
        lambda queues, **kw: seen.update(queues=queues, **kw),
    )

    main(["--queues", "compute, control ", "--concurrency", "4"])

    assert seen == {"queues": ["compute", "control"], "concurrency": 4}


def test_worker_rejects_non_integer_concurrency(monkeypatch):
    monkeypatch.setattr("apps.worker.__main__.run_worker", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        main(["--concurrency", "lots"])

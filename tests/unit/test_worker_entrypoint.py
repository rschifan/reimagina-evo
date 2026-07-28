"""`python -m apps.worker` — argument parsing only, no engine.

The queue flags are gone with the queue. What a Hatchet worker can be told is
its name and how many units it runs at once; readiness, ordering and dispatch
are the engine's, so there is nothing to select.
"""

from __future__ import annotations

import pytest

from apps.worker.__main__ import main


def test_worker_defaults_name_and_slots_to_the_environment(monkeypatch):
    """No flags means "decide from the environment", not "decide here".

    Passing concrete defaults from argparse would silently override
    REIMAGINA_UNIT_CONCURRENCY, which is where the slot count is configured in
    a deployment.
    """
    seen = {}
    monkeypatch.setattr(
        "apps.worker.__main__.run_hatchet_worker", lambda **kw: seen.update(kw)
    )

    main([])

    assert seen == {"name": None, "slots": None}


def test_worker_parses_name_and_slots(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "apps.worker.__main__.run_hatchet_worker", lambda **kw: seen.update(kw)
    )

    main(["--name", "ingest-2", "--slots", "8"])

    assert seen == {"name": "ingest-2", "slots": 8}


def test_worker_rejects_non_integer_slots(monkeypatch):
    monkeypatch.setattr(
        "apps.worker.__main__.run_hatchet_worker", lambda **kw: None
    )

    with pytest.raises(SystemExit):
        main(["--slots", "lots"])


def test_worker_refuses_to_start_without_a_domain_dsn(monkeypatch):
    """Fail at startup, not in every unit minutes later.

    Without DATABASE_URL_DIRECT the engine would happily schedule work, hand it
    to this process, and watch each task die deep inside a runner.
    """
    from apps.worker.app import run_hatchet_worker

    monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL_DIRECT"):
        run_hatchet_worker()

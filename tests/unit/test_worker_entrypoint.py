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
    monkeypatch.setattr("apps.worker.__main__.run_hatchet_worker", lambda **kw: None)

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


def test_a_worker_refuses_to_start_when_a_runner_is_missing(monkeypatch):
    """A deployment error should stop the worker registering, not each task.

    ⚠ Checked at **worker startup**, not at DAG declaration. Declaring a graph
    and being able to execute it are separate concerns with separate lifetimes:
    the workload suite builds one DAG per synthetic workload in a session-scoped
    fixture and installs the matching computes per test, so a build-time check
    killed that whole suite in setup before a single benchmark ran. A worker,
    by contrast, has no business advertising a workflow it cannot execute.
    """
    from apps.worker import app as worker_app
    from reimagina.orchestration import hatchet_app

    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")
    monkeypatch.setenv("HATCHET_CLIENT_TOKEN", "irrelevant")

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _Conn(), raising=False)
    monkeypatch.setattr(
        "reimagina.db.verify_required_relations",
        lambda _conn, relations: dict.fromkeys(relations, True),
        raising=False,
    )
    monkeypatch.setattr(
        hatchet_app, "missing_runners", lambda *a, **k: ["no_such_indicator"]
    )

    with pytest.raises(RuntimeError, match="no runner registered"):
        worker_app.run_hatchet_worker()


def test_declaring_a_graph_without_runners_is_allowed(monkeypatch):
    """The workload suite depends on it, and it is not a worker.

    `build_dag` binds whatever runners exist and leaves the None check to the
    task body; only `run_hatchet_worker` refuses. Asserting this directly is
    what stops the build-time check being reintroduced as an "obvious" tidy-up.
    """
    from reimagina.orchestration import hatchet_app

    monkeypatch.setattr(hatchet_app, "default_runners", lambda registry=None: {})

    assert hatchet_app.missing_runners() == sorted(
        s.slug for s in hatchet_app.SHIPPED_INDICATORS
    )

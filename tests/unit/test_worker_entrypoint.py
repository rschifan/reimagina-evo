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


def test_worker_requires_the_run_plan_table_not_just_job_run(monkeypatch):
    """Every unit resolves its feature set out of `jobs_runtime.run_plan`.

    ⚠ Restored. Replacing this test in place with the missing-runner one
    deleted the only assertion that the preflight asks for `run_plan` at all —
    weakening a test to fit new code rather than covering both behaviours.
    Checking only `job_run` let a worker register against a database missing the
    plan table, so it advertised the workflow, took the work, and failed every
    task on the plan read.
    """
    from apps.worker import app as worker_app
    from reimagina.orchestration import hatchet_app

    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")
    monkeypatch.setenv("HATCHET_CLIENT_TOKEN", "irrelevant")
    monkeypatch.setattr(hatchet_app, "missing_runners", lambda *a, **k: [])

    asked: list[list[str]] = []

    class _Conn:
        def close(self):
            pass

    def _verify(_conn, relations):
        asked.append(list(relations))
        # `run_plan` absent: the case the old check could not see.
        return {r: r != "jobs_runtime.run_plan" for r in relations}

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _Conn(), raising=False)
    monkeypatch.setattr(
        "reimagina.db.verify_required_relations", _verify, raising=False
    )

    with pytest.raises(RuntimeError, match="jobs_runtime.run_plan"):
        worker_app.run_hatchet_worker()

    assert asked == [["jobs_runtime.job_run", "jobs_runtime.run_plan"]]


def test_a_runner_resolves_its_compute_fn_when_it_runs(monkeypatch):
    """Late binding, and it is load-bearing rather than incidental.

    ⚠ Restored. This was deleted outright while reworking the runner checks,
    leaving the property covered only by the conformance suite — which skips
    without a token, so nothing in the fast suites would have noticed it break.

    The Hatchet DAG resolves runners once, at declaration, so a worker builds
    its task closures when it registers and keeps them for its whole life. If a
    runner captured its compute fn at that moment, anything swapping
    `_DIRECT_COMPUTE` afterwards would be silently ignored — which is exactly
    what the conformance suite does to poison an indicator, and it reported the
    poisoned unit as *succeeded* while running the real compute.
    """
    from reimagina.orchestration import runners as r

    built = r.default_runners()  # resolved before the swap, as a worker would

    called: list[str] = []
    monkeypatch.setitem(
        r._DIRECT_COMPUTE,
        "urban_form_block_compactness",
        lambda *a, **k: called.append("swapped"),
    )

    built["urban_form_block_compactness"](
        None, features=frozenset(), scenario_id=None, code_version="v", aoi_id=None
    )

    assert called == ["swapped"], "the runner kept a stale compute fn"

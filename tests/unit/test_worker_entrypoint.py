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


def test_a_missing_runner_fails_at_declaration_not_at_the_unit(monkeypatch):
    """A deployment error should stop the worker registering, not each task.

    The runner used to be resolved inside the task body, once per attempt, and
    a missing one raised `NonRetryableException`. So a worker advertised a
    workflow it could not execute, the engine scheduled work to it, and each
    unit failed — after being dispatched, and after the retry policy had its
    say. Resolving at declaration turns that into a refusal to start.
    """
    from reimagina.indicators.catalog import IndicatorSpec
    from reimagina.orchestration import hatchet_app

    monkeypatch.setattr(hatchet_app, "default_runners", lambda registry=None: {})

    spec = IndicatorSpec(
        slug="no_such_indicator",
        name="No Such",
        computation_kind="direct",
        value_type="numeric",
        aggregation_method="mean",
        applies_to=("block",),
        description="",
        model_family="urban_form",
    )

    with pytest.raises(ValueError, match="no runner registered"):
        hatchet_app.build_dag((spec,), name="never-registered")


def test_worker_requires_the_run_plan_table_not_just_job_run(monkeypatch):
    """Every unit resolves its feature set out of `jobs_runtime.run_plan`.

    Checking only `job_run` let a worker register against a database missing
    the plan table, so it advertised the workflow, took the work, and failed
    every task on the plan read — which is exactly the late failure this
    preflight exists to move forward.
    """
    from apps.worker import app as worker_app

    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")
    monkeypatch.setenv("HATCHET_CLIENT_TOKEN", "irrelevant")

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

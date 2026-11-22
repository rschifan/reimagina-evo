"""The executor id decides what crash recovery reclaims — pure, no DBOS launch.

Both failure modes are silent, which is why they get a test rather than a
comment:

* an id that changes between restarts means a restarted process reclaims
  nothing, so durable execution stops being durable with no error anywhere;
* an id shared by two live processes means one reclaims work the other is still
  running, which on dbos 2.28.0 is a double execution.
"""

from __future__ import annotations

import os

from reimagina.orchestration.dbos_app import _executor_id


def test_the_derived_id_is_stable_across_restarts(monkeypatch):
    """It must not embed anything that changes when the process does.

    It used to be `<hostname>-<pid>`. A restarted worker therefore had a new
    identity and reclaimed none of its own interrupted runs — the single defect
    that made the whole ops-plane's recovery inert by default.
    """
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ID", raising=False)
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ROLE", raising=False)

    first = _executor_id()
    assert first == _executor_id()
    assert str(os.getpid()) not in first, "the id changes when the process does"


def test_roles_separate_processes_sharing_a_host(monkeypatch):
    """An API and a worker on one host must not claim each other's work."""
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ID", raising=False)

    monkeypatch.setenv("REIMAGINA_EXECUTOR_ROLE", "app")
    api = _executor_id()
    monkeypatch.setenv("REIMAGINA_EXECUTOR_ROLE", "worker")
    worker = _executor_id()

    assert api != worker


def test_an_explicit_id_wins(monkeypatch):
    """Beyond one process per role per host, only the operator can be correct."""
    monkeypatch.setenv("REIMAGINA_EXECUTOR_ROLE", "worker")
    monkeypatch.setenv("REIMAGINA_EXECUTOR_ID", "worker-2")
    assert _executor_id() == "worker-2"


def test_the_worker_entrypoint_claims_its_own_role(monkeypatch):
    """The worker must not share the API's identity by default."""
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ID", raising=False)
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ROLE", raising=False)
    launched: list[bool] = []
    monkeypatch.setattr(
        "reimagina.orchestration.dbos_app.launch", lambda: launched.append(True)
    )
    monkeypatch.setattr(
        "reimagina.orchestration.executor_health.heartbeat", lambda: None
    )

    from apps.worker.app import run_dbos_worker

    # Stop before the blocking wait; the role is set before launch.
    monkeypatch.setattr("apps.worker.app._wait_for_shutdown", lambda stop: None)
    run_dbos_worker()

    assert launched == [True]
    assert os.environ["REIMAGINA_EXECUTOR_ROLE"] == "worker"


def test_an_embedded_cli_run_has_an_executor_id_of_its_own(monkeypatch):
    """Two `evaluate-scenario` processes on one host must not share an id.

    Both used to be `<host>:app`, so the second one's launch recovered the
    first one's in-flight run and its units executed twice. A CLI executor is
    short-lived; work it abandons is what the reclaim sweep picks up.
    """
    from contextlib import nullcontext
    from types import SimpleNamespace

    import apps.cli.indicators as cli

    monkeypatch.delenv("REIMAGINA_EXECUTOR_ID", raising=False)
    monkeypatch.delenv("REIMAGINA_EXECUTOR_ROLE", raising=False)
    seen: list[str] = []

    class _Orchestrator:
        def __init__(self, **_kw):
            pass

        def submit_run(self, conn, **_kw):
            seen.append(_executor_id())
            return SimpleNamespace(status=None, run_id="r1")

    monkeypatch.setattr(
        "reimagina.orchestration.dbos_orchestrator.DBOSOrchestrator", _Orchestrator
    )
    monkeypatch.setattr(cli, "connect", lambda dsn: nullcontext(object()))
    monkeypatch.setattr(
        cli,
        "_await_run",
        lambda *a, **k: SimpleNamespace(run_id="r1", status="succeeded", units=()),
    )
    monkeypatch.setattr(cli, "_retire_executor", lambda: None)

    cli.evaluate_scenario_cli(dsn="x", changed_attributes=[], edited_features=[])

    assert seen and str(os.getpid()) in seen[0]


def _embedded_cli(monkeypatch, *, handle_status, await_run):
    """`evaluate_scenario_cli` in embedded mode with DBOS and the DB faked out;
    returns the list `_retire_executor` appends to."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    import apps.cli.indicators as cli

    monkeypatch.delenv("REIMAGINA_EXECUTOR_ID", raising=False)

    class _Orchestrator:
        def __init__(self, **_kw):
            pass

        def submit_run(self, conn, **_kw):
            return SimpleNamespace(status=handle_status, run_id="r1")

    monkeypatch.setattr(
        "reimagina.orchestration.dbos_orchestrator.DBOSOrchestrator", _Orchestrator
    )
    monkeypatch.setattr(cli, "connect", lambda dsn: nullcontext(object()))
    monkeypatch.setattr(cli, "_await_run", await_run)
    retired: list[bool] = []
    monkeypatch.setattr(cli, "_retire_executor", lambda: retired.append(True))
    return cli, retired


def test_an_embedded_noop_retires_its_executor(monkeypatch):
    """A noop returns at submit; its per-process liveness row must not be left
    behind to read as a dead executor."""
    from types import SimpleNamespace

    cli, retired = _embedded_cli(
        monkeypatch,
        handle_status=SimpleNamespace(status="noop"),
        await_run=lambda *a, **k: None,
    )
    cli.evaluate_scenario_cli(dsn="x", changed_attributes=[], edited_features=[])
    assert retired == [True]


def test_an_embedded_timeout_retires_its_executor(monkeypatch):
    import pytest

    def _timeout(*_a, **_k):
        raise TimeoutError("run r1 did not finish")

    cli, retired = _embedded_cli(monkeypatch, handle_status=None, await_run=_timeout)
    with pytest.raises(TimeoutError):
        cli.evaluate_scenario_cli(dsn="x", changed_attributes=[], edited_features=[])
    assert retired == [True]

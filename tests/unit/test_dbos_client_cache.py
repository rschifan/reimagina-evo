"""The shared ``DBOSClient`` must follow the configured system database.

``_client()`` caches, and it has to: ``DBOSClient`` is not a handle but a
SQLAlchemy engine with its own connection pool, built eagerly, on a path the
SSE endpoint polls several times a second.

⚠ It cached a single process-wide client, built on first use and never
revisited. So the **first** value of ``DATABASE_URL_DIRECT`` this process ever
saw became the database every later call read, whatever the environment said
by then.

Nothing raised. ``get_run`` uses the engine as its existence oracle, so a
client pinned to the wrong database answers "that run does not exist" for a
run that plainly does — and ``cancel_run`` returns ``False`` for a live run,
and ``subscribe_progress`` yields ``unknown`` and closes. Three confident
wrong answers, no error anywhere, indistinguishable from a genuine not-found.

It surfaced as cross-suite interference: `tests/integration/test_api_routes`
reaches a jobs route and builds the client against `.env`'s `reimagina_app`;
`tests/conformance` then points `DATABASE_URL_DIRECT` at `reimagina_test`,
enqueues there, and asks a client still bound to `reimagina_app`. Three
conformance tests failed under `pytest tests/` and passed when that directory
ran alone, which is the shape of bug that gets written off as flakiness.

CI could not have caught it. Its integration job points `TEST_DATABASE_URL`,
`REIMAGINA_APP_DSN` and `DATABASE_URL_DIRECT` at the *same* database, and runs
each suite as a separate process besides — so neither the DSN change nor the
shared process ever happens there. A developer's `.env` has them distinct,
which `.env.example` now prescribes, so this failed for a reader and not for
the pipeline. Hence the cover here, at the layer where the caching lives
rather than where the symptom showed up.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import dbos_orchestrator


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    """Never touch the real cache: a stub client must not outlive the test."""
    monkeypatch.setattr(dbos_orchestrator, "_CLIENTS", {})


class _StubClient:
    def __init__(self, *, system_database_url: str, use_listen_notify: bool) -> None:
        self.dsn = system_database_url
        self.use_listen_notify = use_listen_notify


@pytest.fixture
def built(monkeypatch):
    """Record every client construction, so caching is observable.

    Patched on the real ``dbos`` module rather than by replacing it in
    ``sys.modules``: ``reimagina.orchestration.dbos_app`` imports ``dbos``
    itself, so a stub module breaks the DSN lookup this test is about.
    """
    calls: list[str] = []

    def _factory(**kwargs):
        calls.append(kwargs["system_database_url"])
        return _StubClient(**kwargs)

    monkeypatch.setattr("dbos.DBOSClient", _factory)
    return calls


def _set_dsn(monkeypatch, dsn: str) -> None:
    monkeypatch.setattr(
        "reimagina.orchestration.dbos_app.dbos_dsn", lambda: dsn, raising=True
    )


def test_a_client_is_built_once_per_dsn(monkeypatch, built):
    """The caching this exists for: one pooled engine, not one per call."""
    _set_dsn(monkeypatch, "postgresql://host/alpha")
    first = dbos_orchestrator._client()
    second = dbos_orchestrator._client()
    assert first is second
    assert built == ["postgresql://host/alpha"]


def test_changing_the_dsn_changes_the_client(monkeypatch, built):
    """The regression. A cached client must not outlive its configuration.

    Pinned to the first DSN, `get_run` consults the wrong database and reports
    a live run as absent — the port's documented answer for "no such run".
    """
    _set_dsn(monkeypatch, "postgresql://host/alpha")
    alpha = dbos_orchestrator._client()

    _set_dsn(monkeypatch, "postgresql://host/beta")
    beta = dbos_orchestrator._client()

    assert beta is not alpha, (
        "the client did not follow DATABASE_URL_DIRECT, so every engine query "
        "answers against the database this process happened to see first"
    )
    assert beta.dsn == "postgresql://host/beta"
    assert built == ["postgresql://host/alpha", "postgresql://host/beta"]


def test_returning_to_an_earlier_dsn_reuses_its_client(monkeypatch, built):
    """Keyed by DSN rather than rebuilt on change.

    A process that legitimately talks to two system databases keeps one pooled
    client for each, instead of thrashing an engine and a connection every
    time it alternates — which is the whole reason this is cached.
    """
    _set_dsn(monkeypatch, "postgresql://host/alpha")
    alpha = dbos_orchestrator._client()
    _set_dsn(monkeypatch, "postgresql://host/beta")
    dbos_orchestrator._client()
    _set_dsn(monkeypatch, "postgresql://host/alpha")

    assert dbos_orchestrator._client() is alpha
    assert built == ["postgresql://host/alpha", "postgresql://host/beta"]


def test_listen_notify_stays_on(monkeypatch, built):
    """Without it `read_stream` degrades to one-second polling.

    That is precisely the latency the SSE progress endpoint exists to avoid,
    and it is the kind of keyword a refactor of this function drops silently.
    """
    _set_dsn(monkeypatch, "postgresql://host/alpha")
    assert dbos_orchestrator._client().use_listen_notify is True

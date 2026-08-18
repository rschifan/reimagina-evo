"""Executor heartbeat writes are isolated from the worker loop."""

from __future__ import annotations

from reimagina.orchestration import executor_health


def test_heartbeat_upserts_executor_identity(monkeypatch):
    calls: dict[str, object] = {}

    class Cursor:
        def execute(self, query, params):
            calls["query"] = query
            calls["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls["committed"] = True

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(executor_health.psycopg2, "connect", lambda dsn: Conn())
    monkeypatch.setattr(executor_health, "dbos_dsn", lambda: "postgresql://direct")
    monkeypatch.setattr(executor_health, "_executor_id", lambda: "worker-a")
    monkeypatch.setenv("REIMAGINA_EXECUTOR_ROLE", "worker")
    monkeypatch.setenv("REIMAGINA_APP_VERSION", "build-42")

    executor_health.heartbeat()

    assert "ON CONFLICT" in calls["query"]
    assert calls["params"] == ("worker-a", "worker", "build-42")
    assert calls["committed"] is True
    assert calls["closed"] is True

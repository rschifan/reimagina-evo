"""`dbos_app.launch()` ordering and version promotion — DBOS faked, no database."""

from __future__ import annotations

import pytest

from reimagina.orchestration import dbos_app


class FakeDBOS:
    constructed = 0
    promoted: list[str] = []
    application_version = "derived-md5"

    def __init__(self, config):
        FakeDBOS.constructed += 1

    @staticmethod
    def launch():
        pass

    @staticmethod
    def set_latest_application_version(version):
        FakeDBOS.promoted.append(version)

    @staticmethod
    def register_queue(name, **kwargs):
        return object()


@pytest.fixture
def fake(monkeypatch):
    FakeDBOS.constructed = 0
    FakeDBOS.promoted = []
    monkeypatch.setattr(dbos_app, "DBOS", FakeDBOS)
    monkeypatch.setattr(dbos_app, "_assert_domain_database", lambda dsn: None)
    monkeypatch.setattr(dbos_app, "dbos_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(dbos_app, "config", lambda: {"executor_id": "e"})
    monkeypatch.setattr(dbos_app, "_start_liveness", lambda: None)
    monkeypatch.setattr(dbos_app, "_runs_queue", None)
    monkeypatch.setattr(dbos_app, "_units_queue", None)
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setenv("REIMAGINA_EXECUTOR_ID", "e")
    return FakeDBOS


def test_unpinned_version_claims_latest(fake, monkeypatch):
    """Otherwise a worker on older source dequeues none of the API's NULL-version rows."""
    monkeypatch.delenv("REIMAGINA_APP_VERSION", raising=False)
    dbos_app.launch()
    assert fake.promoted == ["derived-md5"]


def test_pinned_version_is_not_promoted(fake, monkeypatch):
    """Blue-green pins deliberately; promoting would let an old colour steal runs."""
    monkeypatch.setenv("REIMAGINA_APP_VERSION", "build-42")
    dbos_app.launch()
    assert fake.promoted == []


def test_failed_first_beat_leaves_queues_unbound_and_retries(fake, monkeypatch):
    """A retry must reach liveness again, not short-circuit with queues draining."""
    beats: list[int] = []

    def beat():
        beats.append(1)
        if len(beats) == 1:
            raise RuntimeError("heartbeat table missing")

    monkeypatch.setattr(dbos_app, "_start_liveness", beat)
    with pytest.raises(RuntimeError):
        dbos_app.launch()
    assert dbos_app._runs_queue is None and dbos_app._units_queue is None

    dbos_app.launch()
    assert len(beats) == 2
    assert fake.constructed == 1, "DBOS's constructor is not idempotent"
    assert dbos_app._runs_queue is not None and dbos_app._units_queue is not None

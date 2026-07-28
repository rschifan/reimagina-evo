"""Unit tests for the orchestrator factory — the ops-plane seam.

Constructing an orchestrator is cheap and touches no database or queue, so
these are pure unit tests. What matters is that the *set* of modes is a closed,
declared vocabulary: an ops-plane swap rewrites this module, and anything it
silently stops offering must fail loudly at the call site.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration.factory import (
    DEFAULT_MODE,
    DURABLE_MODES,
    MODES,
    get_orchestrator,
)


@pytest.mark.parametrize("mode", MODES)
def test_every_declared_mode_builds_an_orchestrator(mode):
    """MODES is the advertised vocabulary; each entry must actually resolve."""
    orchestrator = get_orchestrator(mode)
    for method in ("submit_run", "get_run", "cancel_run", "subscribe_progress"):
        assert callable(
            getattr(orchestrator, method)
        ), f"{mode!r} orchestrator is missing {method}"


def test_unknown_mode_raises_and_names_the_known_ones():
    with pytest.raises(ValueError) as exc:
        get_orchestrator("dagster")
    assert "dagster" in str(exc.value)
    for mode in MODES:
        assert mode in str(exc.value)


def test_default_mode_is_used_when_nothing_is_specified(monkeypatch):
    monkeypatch.delenv("REIMAGINA_ORCHESTRATOR", raising=False)
    assert type(get_orchestrator()) is type(get_orchestrator(DEFAULT_MODE))


def test_env_var_selects_the_mode(monkeypatch):
    monkeypatch.setenv("REIMAGINA_ORCHESTRATOR", "embedded")
    assert type(get_orchestrator()) is type(get_orchestrator("embedded"))


def test_explicit_mode_beats_the_env_var(monkeypatch):
    """A caller that names a mode means it — POST /jobs picks embedded vs detach
    to honour its own 200-vs-202 contract, which deployment config must not
    silently override."""
    monkeypatch.setenv("REIMAGINA_ORCHESTRATOR", "embedded")
    assert type(get_orchestrator("detach")) is not type(get_orchestrator("embedded"))


def test_a_memory_backed_mode_cannot_be_the_deployment_default(monkeypatch):
    """`sync` from the environment would split writes from reads.

    The factory builds a fresh instance per call and `sync` keeps run state on
    the instance, so `POST /jobs` (which names its own durable mode) would write
    to Postgres while `GET /jobs/{run_id}` — which takes the configured one —
    read an empty dict and 404 a run that exists. Explicit is still allowed:
    the conformance suite wants an engine-free target and holds its instance.
    """
    monkeypatch.setenv("REIMAGINA_ORCHESTRATOR", "sync")
    with pytest.raises(ValueError) as exc:
        get_orchestrator()
    assert "sync" in str(exc.value)
    for mode in DURABLE_MODES:
        assert mode in str(exc.value)

    assert get_orchestrator("sync") is not None  # explicit still works


def test_every_durable_mode_is_a_known_mode():
    assert set(DURABLE_MODES) <= set(MODES)

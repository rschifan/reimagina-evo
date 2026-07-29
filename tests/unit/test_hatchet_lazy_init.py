"""Lazy construction must survive concurrent cold starts.

`POST /jobs` is a **sync** def, so FastAPI dispatches it on the anyio
threadpool: two requests against a freshly started process really do race here.
An unguarded `if x is None: x = build()` would then construct two clients — two
gRPC channels with their background threads, one overwritten and leaked — and
declare the same workflow twice.
"""

from __future__ import annotations

import threading

from reimagina.orchestration import hatchet_app


def _hammer(fn, threads: int = 8):
    """Call `fn` from `threads` threads released as close to together as we can."""
    start = threading.Barrier(threads)
    results: list[object] = []
    lock = threading.Lock()

    def _worker():
        start.wait()
        value = fn()
        with lock:
            results.append(value)

    workers = [threading.Thread(target=_worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return results


def test_concurrent_cold_starts_build_exactly_one_client(monkeypatch):
    built = 0

    class _FakeHatchet:
        def __init__(self):
            nonlocal built
            built += 1

    monkeypatch.setattr(hatchet_app, "Hatchet", _FakeHatchet)
    monkeypatch.setattr(hatchet_app, "hatchet", None)

    clients = _hammer(hatchet_app.get_hatchet)

    assert built == 1, f"constructed {built} clients"
    assert len({id(c) for c in clients}) == 1, "callers saw different clients"


def test_concurrent_cold_starts_build_exactly_one_dag(monkeypatch):
    built = 0

    def _fake_build_dag():
        nonlocal built
        built += 1
        return ("workflow", {"slug": "node"})

    monkeypatch.setattr(hatchet_app, "build_dag", _fake_build_dag)
    monkeypatch.setattr(hatchet_app, "_dag", None)

    workflows = _hammer(hatchet_app.get_run_workflow)

    assert built == 1, f"declared the DAG {built} times"
    assert set(workflows) == {"workflow"}


def test_the_workflow_and_its_nodes_always_come_from_one_build(monkeypatch):
    """Why `_dag` is one tuple rather than two globals.

    `get_run_workflow()` used to return early whenever `run_workflow` was set —
    without ever assigning `_nodes` — so a caller that replaced the workflow
    (the workload suite does exactly that) left `nodes()` either asserting or
    describing a different graph.
    """
    monkeypatch.setattr(hatchet_app, "_dag", ("bench-workflow", {"bench": "node"}))

    assert hatchet_app.get_run_workflow() == "bench-workflow"
    assert hatchet_app.nodes() == {"bench": "node"}

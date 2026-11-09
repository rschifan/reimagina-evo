"""The liveness gate decides whether a peer's running work gets re-enqueued.

Under DBOS 3 a *duplicate* recovery request is idempotent — recovery
re-enqueues, and the queue's atomic ENQUEUED->PENDING dequeue admits exactly
one runner. What is **not** idempotent is recovering an executor that never
stopped: that re-enqueues a workflow its original runner is still executing,
and admits a second one alongside it.

So every assertion here is about one question — when is a peer dead enough to
sweep — and the bias is one-directional. Reclaiming a dead executor late costs
latency. Reclaiming a live one costs a duplicate execution of a unit.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import reclaim


def test_the_threshold_must_be_several_beats(monkeypatch):
    """A threshold near the beat interval turns one missed beat into a death.

    Both values come from separate environment variables, so nothing else
    would notice them drifting together into a configuration where a GC pause
    is indistinguishable from a destroyed host.
    """
    monkeypatch.setenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", "30")
    monkeypatch.setenv("REIMAGINA_EXECUTOR_STALE_S", "60")
    with pytest.raises(RuntimeError, match="less than"):
        reclaim.stale_after_seconds()


def test_a_generous_threshold_is_accepted(monkeypatch):
    monkeypatch.setenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", "30")
    monkeypatch.setenv("REIMAGINA_EXECUTOR_STALE_S", "300")
    assert reclaim.stale_after_seconds() == 300


def test_the_default_threshold_clears_the_default_beat(monkeypatch):
    """The shipped defaults must not fail their own validation."""
    monkeypatch.delenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", raising=False)
    monkeypatch.delenv("REIMAGINA_EXECUTOR_STALE_S", raising=False)
    assert reclaim.stale_after_seconds() == reclaim.DEFAULT_STALE_AFTER_S


def test_raising_the_beat_interval_alone_is_refused(monkeypatch):
    """Slowing the beat without widening the window is the dangerous edit.

    It is also the tempting one — beats look like chatter against a database,
    so the interval is what an operator reaches for first.
    """
    monkeypatch.setenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", "120")
    monkeypatch.delenv("REIMAGINA_EXECUTOR_STALE_S", raising=False)
    with pytest.raises(RuntimeError, match="heartbeat interval"):
        reclaim.stale_after_seconds()


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((" ".join(sql.split()), params))
        self._rows = self._conn._next_rows()

    def fetchall(self):
        return self._rows


class _Conn:
    """A psycopg2 connection stub that records SQL and replays result sets.

    ``rows`` answers every query with the same rows. ``results`` answers each
    query in turn, which is what the two-statement read-then-delete in
    ``forget_executor`` needs: a stub that returned its rows to both would
    make the guard look like it had found unfinished work when all it found
    was the fixture.
    """

    def __init__(self, rows=None, results=None):
        if rows is not None and results is not None:
            raise ValueError("pass rows or results, not both")
        self._fixed = None if rows is None else list(rows)
        self._queue = None if results is None else [list(r) for r in results]
        self.statements: list[tuple] = []

    def _next_rows(self) -> list:
        if self._queue is not None:
            return self._queue.pop(0) if self._queue else []
        return list(self._fixed or [])

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass


def test_this_executor_is_never_a_candidate():
    """A sweeper that declared itself dead would re-enqueue its own work.

    Its own row can legitimately look stale — a process busy in a long unit
    with a wedged heartbeat thread is exactly the case — so excluding it has
    to be in the query rather than left to the caller.
    """
    conn = _Conn(rows=[("vm:worker-2",)])
    reclaim.dead_executors(conn, stale_after=300, exclude="vm:worker-1")
    sql, params = conn.statements[0]
    assert "executor_id <> %s" in sql
    assert params == (300, "vm:worker-1", "vm:worker-1")


def test_candidates_come_back_oldest_first():
    """A sweep interrupted part-way should have helped the worst case first."""
    conn = _Conn(rows=[("oldest",), ("newer",)])
    assert reclaim.dead_executors(conn, stale_after=300) == ["oldest", "newer"]
    sql, _ = conn.statements[0]
    assert "ORDER BY heartbeat_at" in sql


def test_an_executor_with_no_heartbeat_row_is_not_swept():
    """Absence is not evidence of death.

    The query can only return executors that have a row, and that is the
    design: a process running code too old to publish liveness is invisible
    here, and guessing about it is worst precisely then.
    """
    conn = _Conn(rows=[])
    assert reclaim.dead_executors(conn, stale_after=300) == []


def test_a_reclaimed_executor_is_forgotten():
    """Otherwise the same dead executor is re-swept on every pass, forever,
    and shows in `explore.ops_executor_health_v` as permanent noise."""
    conn = _Conn(results=[[]])  # nothing unfinished
    assert reclaim.forget_executor(conn, "vm:worker-9") is True
    checked, deleted = conn.statements
    assert "FROM dbos.workflow_status" in checked[0]
    assert deleted[0].startswith("DELETE FROM jobs_runtime.ops_executor_heartbeat")
    assert deleted[1] == ("vm:worker-9",)


def test_an_executor_with_unfinished_work_keeps_its_row():
    """⚠ Recovering zero workflows is not evidence that there were none.

    ``DBOS._recover_pending_workflows`` asks
    ``get_pending_workflows(executor_id, app_version)``, and that query filters
    on ``application_version = <this process's version>``. A sweeper on a
    different version than the executor that died — the blue-green case the
    version pinning in ``dbos_app.config`` exists to support — recovers
    nothing, every time, however much work is stranded.

    Deleting the liveness row on that result destroyed the last pointer to it:
    the workflows stay PENDING forever, no later sweep looks at that id again,
    and ``ops_executor_health_v`` stops showing there was ever a worker there.
    A permanent leak created by the cleanup step, with no symptom anywhere.
    """
    conn = _Conn(results=[[("wf-1", "v-old"), ("wf-2", "v-old")]])
    assert reclaim.forget_executor(conn, "vm:worker-9") is False
    assert len(conn.statements) == 1, "the row must not be deleted"
    assert "DELETE" not in conn.statements[0][0]


def test_the_unfinished_check_covers_enqueued_as_well_as_pending():
    """A workflow waiting for a dequeue is as unreclaimed as one in flight.

    Checking only PENDING would forget an executor whose stranded work had
    not yet been picked up — the easiest case to recover and the one most
    likely to exist right after a death.
    """
    conn = _Conn(results=[[]])
    reclaim.unfinished_workflows(conn, "vm:worker-9")
    sql, params = conn.statements[0]
    assert "status IN (%s, %s)" in sql
    assert params[1:] == ("PENDING", "ENQUEUED")


def test_the_report_names_what_it_could_not_reclaim():
    """A strand has to be visible, or the fleet looks healthy while leaking."""
    report = reclaim.ReclaimReport(
        dead_executors=("vm:worker-9",), stranded_executors=("vm:worker-9",)
    )
    assert report.acted
    assert report.stranded_executors == ("vm:worker-9",)

"""``OpsRunReader`` against a real database — the Ops dashboard's only reader.

These are the properties the mocked contract tests cannot express, because every
one of them is a claim about SQL: that the counts are per-run rather than
global, that a run with no units reports zero rather than one, that an empty
status list means "no filter" rather than "no runs", and that a malformed id is
an empty answer rather than a ``DataError`` the API turns into a 500.

Writes go through ``orchestration.runstore`` — the single writer — so these also
pin the two modules to the same rows rather than to each other's assumptions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from reimagina.orchestration import runstore
from reimagina.storage import backend_for_connection


def _reader(conn):
    return backend_for_connection(conn).ops_run_reader()


def _make_run(conn, *, status="running", units=(), code_version="test", aoi_id=None):
    """One run envelope plus its units, via the writer the workflow uses."""
    run_id = str(uuid4())
    runstore.create_run(
        conn,
        run_id=run_id,
        scenario_id=None,
        aoi_id=aoi_id,
        code_version=code_version,
        status=status,
    )
    runstore.record_units(
        conn,
        run_id=run_id,
        units=[
            {"indicator": slug, "features": [], "status": "queued", "detail": None}
            for slug, _ in units
        ],
    )
    for slug, unit_status in units:
        if unit_status != "queued":
            runstore.set_unit_status(
                conn, run_id=run_id, indicator_slug=slug, status=unit_status
            )
    return run_id


def test_counts_are_per_run_not_global(conn):
    """The lateral aggregate must be correlated — two runs must not share counts."""
    a = _make_run(conn, units=[("alpha", "succeeded"), ("beta", "failed")])
    b = _make_run(conn, units=[("gamma", "succeeded")])

    rows = {r["run_id"]: r for r in _reader(conn).list_runs(limit=50)}

    assert rows[a]["units_total"] == 2
    assert rows[a]["units_succeeded"] == 1
    assert rows[a]["units_failed"] == 1
    assert rows[b]["units_total"] == 1
    assert rows[b]["units_failed"] == 0


def test_a_run_with_no_units_counts_zero(conn):
    """``count(*)`` in the lateral would say 1 here. It must say 0."""
    run_id = _make_run(conn, units=[])

    row = _reader(conn).get_run_row(run_id)

    assert row["units_total"] == 0
    assert row["units_terminal"] == 0
    assert row["units_last_terminal_at"] is None


def test_empty_status_list_means_no_filter(conn):
    """``[]`` binds as an empty array, which matches nothing — it must not reach SQL.

    The failure this pins is silent: an empty list is what ``body.x or []``
    produces, and the dashboard would show no runs at all while reporting success.
    """
    run_id = _make_run(conn, status="running", units=[("alpha", "queued")])
    reader = _reader(conn)

    assert run_id in {r["run_id"] for r in reader.list_runs(statuses=[], limit=50)}
    assert reader.count_runs(statuses=[]) == reader.count_runs()


def test_status_filter_selects(conn):
    running = _make_run(conn, status="running", units=[("alpha", "queued")])
    failed = _make_run(conn, status="failed", units=[("beta", "failed")])

    ids = {r["run_id"] for r in _reader(conn).list_runs(statuses=["failed"], limit=50)}

    assert failed in ids
    assert running not in ids


def test_status_counts_are_unfiltered(conn):
    _make_run(conn, status="failed", units=[("alpha", "failed")])
    _make_run(conn, status="succeeded", units=[("beta", "succeeded")])

    counts = _reader(conn).count_runs_by_status()

    assert counts.get("failed", 0) >= 1
    assert counts.get("succeeded", 0) >= 1


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "1; DROP TABLE x", "42"])
def test_a_malformed_run_id_is_an_empty_answer_not_an_error(conn, bad_id):
    """Reaching Postgres with a non-uuid raises DataError → 500 + aborted txn."""
    reader = _reader(conn)

    assert reader.get_run_row(bad_id) is None
    assert reader.list_units_detailed(bad_id) == []


def _time_unit(conn, run_id, slug, started, completed):
    """Pin a unit's timings; ``set_unit_status`` stamps ``now()``, the same
    instant for every row in a test's transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.job_unit SET started_at = %s, completed_at = %s "
            "WHERE run_id = %s::uuid AND indicator_slug = %s",
            (started, completed, run_id, slug),
        )


def test_expected_duration_is_the_last_same_aoi_success_before_this_run(conn):
    old = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, old, "a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:07Z")
    newer = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, newer, "a", "2026-01-02T00:00:00Z", "2026-01-02T00:00:05Z")
    this = _make_run(conn, units=[("a", "queued")], aoi_id="x")

    assert _reader(conn).expected_durations(this) == {"a": 5000}


def test_expected_duration_ignores_failures_other_aois_and_later_runs(conn):
    """Each would make the estimate a guess, or let it move while the run is live."""
    failed = _make_run(conn, status="failed", units=[("a", "failed")], aoi_id="x")
    _time_unit(conn, failed, "a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z")
    other = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="y")
    _time_unit(conn, other, "a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:04Z")
    later = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, later, "a", "2099-01-01T00:00:00Z", "2099-01-01T00:00:06Z")
    this = _make_run(conn, units=[("a", "queued")], aoi_id="x")

    assert _reader(conn).expected_durations(this) == {}


def test_a_finished_run_is_not_its_own_estimate(conn):
    """Reopened after it finished, a run's own successes must not become its
    forecast: they completed after it was submitted."""
    this = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, this, "a", "2099-01-01T00:00:00Z", "2099-01-01T00:00:09Z")

    assert _reader(conn).expected_durations(this) == {}


def test_whole_dataset_runs_match_each_other(conn):
    """``aoi_id`` NULL is the launch form's default; ``=`` would never match it."""
    old = _make_run(conn, status="succeeded", units=[("a", "succeeded")])
    _time_unit(conn, old, "a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02.5Z")
    this = _make_run(conn, units=[("a", "queued"), ("b", "queued")])

    assert _reader(conn).expected_durations(this) == {"a": 2500}


def test_expected_durations_guards_the_id(conn):
    assert _reader(conn).expected_durations("not-a-uuid") == {}
    assert _reader(conn).expected_durations(str(uuid4())) == {}


def test_units_are_ordered_by_slug(conn):
    """Display order is the client's; start order would reshuffle it mid-run."""
    run_id = _make_run(conn, units=[("beta", "running"), ("alpha", "queued")])
    _time_unit(conn, run_id, "beta", "2026-01-01T00:00:00Z", None)

    units = _reader(conn).list_units_detailed(run_id)

    assert [u["indicator"] for u in units] == ["alpha", "beta"]


def test_units_carry_their_timings(conn):
    """``started_at``/``completed_at`` are what the dashboard renders as duration."""
    run_id = _make_run(conn, units=[("beta", "queued"), ("alpha", "running")])
    runstore.set_unit_status(
        conn, run_id=run_id, indicator_slug="alpha", status="succeeded"
    )

    units = {u["indicator"]: u for u in _reader(conn).list_units_detailed(run_id)}

    assert units["alpha"]["started_at"] is not None
    assert units["alpha"]["completed_at"] is not None
    assert units["beta"]["started_at"] is None
    assert units["beta"]["completed_at"] is None


def test_last_terminal_at_is_set_for_every_terminal_status(conn):
    """The stall grace reads this column; a NULL max would make the badge never fire.

    ``skipped`` and ``cancelled`` are terminal too — the interesting case, since
    neither goes through the ``running`` transition that stamps ``started_at``.
    """
    for terminal in ("succeeded", "failed", "skipped", "cancelled"):
        run_id = _make_run(conn, units=[("alpha", terminal)])
        row = _reader(conn).get_run_row(run_id)
        assert row["units_terminal"] == 1, terminal
        assert row["units_last_terminal_at"] is not None, terminal


def test_executor_health_reports_age_as_a_number(conn):
    """An ``interval`` here would be a 500 at the Pydantic boundary."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs_runtime.ops_executor_heartbeat
              (executor_id, role, app_version)
            VALUES ('test-exec', 'worker', 'v0')
            ON CONFLICT (executor_id) DO UPDATE SET heartbeat_at = clock_timestamp()
            """
        )

    rows = {r["executor_id"]: r for r in _reader(conn).list_executor_health()}

    assert isinstance(rows["test-exec"]["heartbeat_age_s"], float)
    assert rows["test-exec"]["heartbeat_age_s"] >= 0


def test_deleting_a_run_takes_its_units_and_leaves_everything_else(conn):
    """History goes; what the run computed does not.

    `job_unit` cascades from the envelope (r028), but `indicator_result` and
    `job_run` are separate tables — forgetting that a run happened must not undo
    what it produced.
    """
    keep = _make_run(conn, units=[("alpha", "succeeded")])
    drop = _make_run(conn, status="succeeded", units=[("beta", "succeeded")])
    reader = _reader(conn)

    assert reader.delete_run(drop) is True

    assert reader.get_run_row(drop) is None
    assert reader.list_units_detailed(drop) == []
    # The untouched run is still whole.
    assert reader.get_run_row(keep)["units_total"] == 1


def test_deleting_an_unknown_or_malformed_run_reports_false(conn):
    reader = _reader(conn)

    assert reader.delete_run(str(uuid4())) is False
    assert reader.delete_run("not-a-uuid") is False


def test_clearing_history_spares_live_runs(conn):
    """A running run's workflow is still writing to these rows."""
    live = _make_run(conn, status="running", units=[("alpha", "running")])
    done = _make_run(conn, status="succeeded", units=[("beta", "succeeded")])
    reader = _reader(conn)

    removed = reader.delete_terminal_runs()

    assert done in removed
    assert live not in removed
    assert reader.get_run_row(done) is None
    assert reader.get_run_row(live) is not None

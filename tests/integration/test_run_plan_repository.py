"""The run plan round-trips through Postgres, against a real database.

Deliberately an integration test rather than a mocked one. The failure mode this
guards is a *type* mismatch — psycopg2 adapts a Python list of strings to a text
array, and Postgres will not implicitly cast ``text[]`` to ``uuid[]`` — and a
recording cursor would happily accept a statement the server rejects.

The other property under test is the loud failure: a unit that finds no plan row
must raise, not compute zero features and report success.
"""

from __future__ import annotations

import pathlib

import uuid

import pytest

from reimagina.storage import backend_for_connection


def _plan_id() -> str:
    return str(uuid.uuid4())


def _features(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def test_a_plan_round_trips_through_postgres(conn):
    repo = backend_for_connection(conn).run_plan_repo()
    plan_id = _plan_id()
    compactness = _features(3)
    density = _features(2)

    repo.write(
        plan_id=plan_id,
        units={
            "urban_form_block_compactness": compactness,
            "urban_form_block_building_density": density,
        },
    )

    assert (
        repo.features_for(
            plan_id=plan_id, indicator_slug="urban_form_block_compactness"
        )
        == compactness
    )
    assert (
        repo.features_for(
            plan_id=plan_id, indicator_slug="urban_form_block_building_density"
        )
        == density
    )


def test_order_within_a_unit_is_preserved(conn):
    """The runner receives a set, but the stored order must not be reshuffled.

    An array that came back reordered would still compute correctly and would
    still break the dedup fingerprint, which is derived from the same content.
    """
    repo = backend_for_connection(conn).run_plan_repo()
    plan_id = _plan_id()
    features = _features(25)

    repo.write(plan_id=plan_id, units={"slug": features})

    assert repo.features_for(plan_id=plan_id, indicator_slug="slug") == features


def test_a_large_unit_survives_toast(conn):
    """86k uuids is the real shape; 5k is enough to force out-of-line storage.

    A uuid[] this size is TOASTed and compressed rather than stored inline, and
    that path has to give back exactly what went in — this is the row that
    replaces a 10 MB gRPC message.
    """
    repo = backend_for_connection(conn).run_plan_repo()
    plan_id = _plan_id()
    features = _features(5000)

    repo.write(plan_id=plan_id, units={"slug": features})

    assert repo.features_for(plan_id=plan_id, indicator_slug="slug") == features


def test_a_missing_plan_raises_rather_than_returning_empty(conn):
    """The defect this prevents is silent.

    Returning ``[]`` would let a unit compute nothing, report success, and leave
    the indicator stale — with no error to attribute it to.
    """
    repo = backend_for_connection(conn).run_plan_repo()

    with pytest.raises(LookupError, match="no run_plan row"):
        repo.features_for(plan_id=_plan_id(), indicator_slug="urban_form_x")


def test_a_known_plan_with_an_unknown_slug_also_raises(conn):
    """A plan that exists but omits this unit is the same defect, not an empty set."""
    repo = backend_for_connection(conn).run_plan_repo()
    plan_id = _plan_id()
    repo.write(plan_id=plan_id, units={"slug_a": _features(1)})

    with pytest.raises(LookupError, match="no run_plan row"):
        repo.features_for(plan_id=plan_id, indicator_slug="slug_b")


def test_an_empty_plan_is_refused(conn):
    """A run with no dirty units is a noop and must never reach the store."""
    repo = backend_for_connection(conn).run_plan_repo()

    with pytest.raises(ValueError, match="empty run plan"):
        repo.write(plan_id=_plan_id(), units={})


def test_rewriting_a_plan_is_rejected_by_the_primary_key(conn):
    """Write-once is enforced by the schema, not by convention.

    If a second write could land, replay could read a different set than the one
    the submitter resolved — the exact drift this table exists to prevent.
    """
    import psycopg2

    repo = backend_for_connection(conn).run_plan_repo()
    plan_id = _plan_id()
    repo.write(plan_id=plan_id, units={"slug": _features(2)})

    with pytest.raises(psycopg2.errors.UniqueViolation):
        repo.write(plan_id=plan_id, units={"slug": _features(2)})


# ── Cleanup ─────────────────────────────────────────────────────────────────


def test_delete_reclaims_only_the_named_plan(conn):
    repo = backend_for_connection(conn).run_plan_repo()
    keep, drop = _plan_id(), _plan_id()
    repo.write(plan_id=keep, units={"slug": _features(2)})
    repo.write(plan_id=drop, units={"slug": _features(2), "other": _features(1)})

    assert repo.delete(plan_id=drop) == 2
    assert repo.features_for(plan_id=keep, indicator_slug="slug")  # untouched
    with pytest.raises(LookupError):
        repo.features_for(plan_id=drop, indicator_slug="slug")


def test_deleting_an_unknown_plan_is_not_an_error(conn):
    """Terminal cleanup runs on paths where the plan may already be gone."""
    repo = backend_for_connection(conn).run_plan_repo()
    assert repo.delete(plan_id=_plan_id()) == 0


_RECLAIM_SQL = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "reclaim_run_plans.sql"
)

_RECLAIM_STATEMENT = "\n".join(
    line
    for line in _RECLAIM_SQL.read_text().splitlines()
    # psql metacommands are the caller's business and psycopg2 cannot run them.
    if not line.startswith("\\")
).replace(":'horizon'", "%(horizon)s")
"""Read and rewritten once at import, not per call — it cannot change mid-run."""


def _reclaim(conn, horizon: str) -> int:
    """Run the **shipped** retention statement, not a copy of it.

    ⚠ This used to re-type the DELETE with a bound parameter. That left the
    actual file — its `:'horizon'` quoting, its `ON_ERROR_STOP`, any later edit
    to its predicate — with no automated coverage at all, while three tests
    reported the retention behaviour as verified.

    psql metacommands are stripped: they are the caller's business, and psycopg2
    cannot execute them.
    """
    with conn.cursor() as cur:
        cur.execute(_RECLAIM_STATEMENT, {"horizon": horizon})
        return cur.rowcount


def _backdate(conn, plan_id: str, interval: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.run_plan "
            "SET created_at = now() - %s::interval WHERE plan_id = %s::uuid",
            (interval, plan_id),
        )


def test_retention_spares_a_plan_inside_the_horizon(conn):
    """The failure this guards is worse than an orphan.

    Reclaiming a plan out from under a run — live, retriable or replayable —
    makes every subsequent unit fail with a missing-plan LookupError, the loud
    error that is supposed to mean "a write was lost".
    """
    repo = backend_for_connection(conn).run_plan_repo()
    fresh = _plan_id()
    repo.write(plan_id=fresh, units={"slug": _features(2)})

    assert _reclaim(conn, "45 days") == 0
    assert repo.features_for(plan_id=fresh, indicator_slug="slug")


def test_retention_spares_a_plan_still_inside_engine_run_retention(conn):
    """The horizon must outlast the engine, not merely outlast a long run.

    Hatchet keeps runs for 720 h by default and `runs.replay()` re-executes from
    the original input — this plan. A 31-day-old plan is still replayable, which
    is exactly the case the old 24 h submit-path sweep destroyed.
    """
    repo = backend_for_connection(conn).run_plan_repo()
    replayable = _plan_id()
    repo.write(plan_id=replayable, units={"slug": _features(2)})
    _backdate(conn, replayable, "31 days")

    assert _reclaim(conn, "45 days") == 0
    assert repo.features_for(plan_id=replayable, indicator_slug="slug")


def test_retention_reclaims_a_plan_past_the_horizon(conn):
    """Past the horizon the run itself is gone from the engine, so nothing reads it."""
    repo = backend_for_connection(conn).run_plan_repo()
    stale = _plan_id()
    repo.write(plan_id=stale, units={"slug": _features(2)})
    _backdate(conn, stale, "46 days")

    assert _reclaim(conn, "45 days") >= 1
    with pytest.raises(LookupError):
        repo.features_for(plan_id=stale, indicator_slug="slug")

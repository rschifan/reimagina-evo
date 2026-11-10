"""`bootstrap-db` must seed feature types before assets, not hope a migration did.

A feature-backed asset resolves its `feature_type_id`, so `seed_assets` raises
when the catalog is empty:

    asset 'mex.state' references unknown feature type 'MEX:state'
    — run `feat seed-types` first

`bootstrap_local_platform` called `seed_assets` under a comment claiming
assets are seeded "after feature types exist", while nothing in it made them
exist. It worked only because `r003_feature_tables` happens to insert the same
fifteen rows as a migration side-effect.

⚠ On a database where that side-effect did not land — created before r003
carried the seed, or cleared since — `bootstrap-db` failed outright, and its
own advice was impossible to follow: `feat seed-types` is the step *after*
`bootstrap-db` in every runbook, so the user was told to run the thing they
could not reach. Observed on a real machine partway through building the
Mexicali pilot.

These tests pin the order rather than the accident.
"""

from __future__ import annotations

import inspect

import pytest

from reimagina.assets.registry import seed_assets
from reimagina.data.feature import FEATURE_TYPE_ROWS, seed_feature_types
from reimagina.db import bootstrap as bootstrap_mod


def _clear_catalog(conn) -> None:
    """Empty the asset graph and the feature-type catalog.

    Asset rows are referenced by `asset_dependency`, so the dependent tables go
    first — a bare `DELETE FROM curated_geo.asset` fails on the foreign key.
    """
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE curated_geo.asset_dependency, "
            "curated_geo.asset_materialization, curated_geo.asset_version, "
            "curated_geo.asset CASCADE"
        )
        cur.execute("DELETE FROM curated_geo.feature_type_catalog")


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return int(cur.fetchone()[0])


def test_seeding_assets_without_feature_types_is_the_failure_being_fixed(conn):
    """The precondition. Without this, the ordering test proves nothing."""
    _clear_catalog(conn)
    assert _count(conn, "curated_geo.feature_type_catalog") == 0

    with pytest.raises(RuntimeError) as exc:
        seed_assets(conn)
    message = str(exc.value)
    assert "unknown feature type" in message
    assert "feat seed-types" in message


def test_the_bootstrap_order_recovers_an_unseeded_catalog(conn):
    """Feature types, then assets — the sequence bootstrap_local_platform runs."""
    _clear_catalog(conn)

    seed_feature_types(conn)
    seed_assets(conn)

    assert _count(conn, "curated_geo.feature_type_catalog") == len(FEATURE_TYPE_ROWS)
    assert _count(conn, "curated_geo.asset") > 0


def test_seeding_feature_types_twice_is_harmless(conn):
    """It runs on every bootstrap, including ones that need nothing."""
    before = _count(conn, "curated_geo.feature_type_catalog")
    seed_feature_types(conn)
    seed_feature_types(conn)
    assert _count(conn, "curated_geo.feature_type_catalog") == max(
        before, len(FEATURE_TYPE_ROWS)
    )


def test_bootstrap_local_platform_seeds_types_before_assets():
    """Read the source, because the bug was an ordering claim in a comment.

    A behavioural test would have to create databases. What went wrong here was
    narrower and cheaper to pin: `seed_assets` was called without
    `seed_feature_types` ever being called, under a comment asserting the
    opposite. So assert on the call order in the function that owns it.
    """
    source = inspect.getsource(bootstrap_mod.bootstrap_local_platform)

    assert "seed_feature_types(conn)" in source, (
        "bootstrap_local_platform must seed feature types itself; relying on "
        "r003's migration side-effect is what broke on a real database"
    )
    assert "seed_assets(conn)" in source

    types_at = source.index("seed_feature_types(conn)")
    assets_at = source.index("seed_assets(conn)")
    assert types_at < assets_at, (
        "feature types must be seeded before assets — a feature-backed asset "
        "resolves its feature_type_id and raises when the catalog is empty"
    )

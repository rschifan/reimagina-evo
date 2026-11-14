"""AOI membership must stay subdivided, or it reverts to ~9 minutes a city.

`compute_overlaps` intersects the AOI polygon against every candidate feature.
Against one whole municipal boundary the GiST index can prune only on its
bounding box — for Municipio de Mexicali that is most of Baja California — so
every feature in the box got a full `ST_Intersection` against all 2,109
vertices, and each measure was recomputed eight times.

Measured on 343,696 real Overture buildings against the real boundary:

    polygon branch   508,006 ms -> 20,249 ms   (25x)
    line branch      457,368 ms ->  7,505 ms   (61x)
    compute_overlaps   536,000 ms ->  40,400 ms  (13x, including the
                                      delete and re-insert of 343,696 rows)

These are structural assertions, not behavioural ones — correctness is covered
by the integration suite. They exist because every element below is easy to
"simplify" away, and the cost of doing so is invisible on a test fixture of
sixteen blocks.
"""

from __future__ import annotations

import inspect

from reimagina.storage.postgres.repositories import (
    AOI_SUBDIVIDE_VERTICES,
    MEMBERSHIP_MIN_OVERLAP_RATIO,
    PostgresAOIRepository,
)


def _sql() -> str:
    return inspect.getsource(PostgresAOIRepository.compute_overlaps)


def test_the_aoi_polygon_is_subdivided():
    assert "ST_Subdivide" in _sql(), (
        "without ST_Subdivide every feature intersects the whole boundary and "
        "the index cannot prune — ~8.5 minutes for one city"
    )


def test_the_subdivision_is_materialized():
    """Inlined, `ST_Subdivide` is re-evaluated per feature — worse than none."""
    assert "MATERIALIZED" in _sql()


def test_wholly_contained_features_skip_the_intersection():
    """`ST_CoveredBy` is the short-circuit that makes the common case free.

    A feature inside a chunk has its intersection equal to itself, so the
    geometry never needs constructing. For a city inside a 9,971 km² municipio
    that is nearly every feature.
    """
    assert "ST_CoveredBy" in _sql()


def test_both_measured_branches_are_subdivided():
    """Polygons and lines both; the point branch needs no measure."""
    sql = _sql()
    assert sql.count("ST_Subdivide") >= 2
    assert sql.count("ST_CoveredBy") >= 2
    assert "'area'" in sql and "'length'" in sql and "'presence'" in sql


def test_each_measure_is_computed_once_per_feature():
    """The old shape wrote the geography cast eight times per row.

    Counted rather than eyeballed: with the measure computed once in a
    `measured` CTE and reused, the per-branch count of `::geography` casts is
    small. The old shape had eight per branch in the SELECT and WHERE alone.
    """
    sql = _sql()
    assert "measured AS (" in sql, "the measure must be computed in its own CTE"
    # 3 per branch: the covered-by short-circuit, the intersection, and the
    # feature's own measure. Two measured branches -> 6.
    assert sql.count("::geography") <= 8, (
        f"{sql.count('::geography')} geography casts — the point of the "
        f"`measured` CTE is that each measure is projected once per feature"
    )


def test_jit_is_disabled_for_these_statements():
    """⚠ Not a micro-optimisation — a crash.

    Subdividing raises the cost estimate past `jit_above_cost`, so Postgres
    tries to JIT-compile. Where the PostGIS build and LLVM disagree, loading
    its bitcode summary is a FATAL that kills the backend:

        FATAL: failed to load summary ".../postgis-3.index.bc":
               Invalid summary version 10. Version should be in [1-9].

    Observed three times on PostgreSQL 16 + PGDG PostGIS 3 while measuring
    this change. Without the SET, the optimisation trades 25x for an outright
    failure on some installs.
    """
    sql = _sql()
    assert "SET LOCAL jit = off" in sql
    # SET LOCAL, not SET: it must revert at commit rather than leak into the
    # caller's session.
    assert "SET jit" not in sql.replace("SET LOCAL jit", "")


def test_the_identifier_rule_survived_the_rewrite():
    """r040: identifiers beat geometry where both exist.

    The rewrite touched the candidate CTEs, which is where this lives.
    """
    sql = _sql()
    assert sql.count("membership_key IS NULL") >= 3, (
        "all three branches must apply the same rule, or they can disagree "
        "about who is a member"
    )
    assert sql.count("f.attributes @> aoi.membership_key") >= 3


def test_the_threshold_and_cap_survived_the_rewrite():
    sql = _sql()
    assert "LEAST(m.overlap_measure, m.feature_measure)" in sql
    assert "LEAST(m.overlap_measure / NULLIF(m.feature_measure, 0), 1.0)" in sql
    assert MEMBERSHIP_MIN_OVERLAP_RATIO == 1e-9


def test_the_subdivision_size_is_a_named_constant():
    assert isinstance(AOI_SUBDIVIDE_VERTICES, int)
    assert 8 <= AOI_SUBDIVIDE_VERTICES <= 256, (
        "too small and the chunk join dominates; too large and the index cannot prune"
    )

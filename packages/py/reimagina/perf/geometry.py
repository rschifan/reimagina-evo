"""Shared geometry probes for deterministic PostGIS benchmark inputs.

A benchmark is only as meaningful as the region it reads, so every probe here
must be **deterministic** (the same input on every run and every machine) and
**bounded** (a viewport whose size is a property of the query, not of whichever
row happened to sort first).
"""

from __future__ import annotations

_PADDING = 0.0001
"""Degrees added around an anchor envelope, and the floor on any half-extent.

Keeps a zero-area geometry — a point feature, or a degenerate polygon — from
producing an empty bbox that matches nothing.
"""


def feature_extent(conn) -> tuple[float, float, float, float]:
    """Return the bounding box of every canonical feature.

    Lives here rather than privately in both callers: `perf/seed.py` and
    `perf/runner.py` each carried a character-identical copy of this query, so
    any change to it had to be made twice and would silently drift the seeded
    AOIs away from the benchmarked viewports.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              ST_XMin(extent::geometry),
              ST_YMin(extent::geometry),
              ST_XMax(extent::geometry),
              ST_YMax(extent::geometry)
            FROM (
              SELECT ST_Extent(geometry) AS extent
              FROM curated_geo.feature
            ) bounds
            """
        )
        row = cur.fetchone()
    if row is None or any(value is None for value in row):
        raise ValueError(
            "No canonical features are loaded. Benchmarks require feature data."
        )
    return tuple(float(value) for value in row)  # type: ignore[return-value]


def bbox_from_extent(
    extent: tuple[float, float, float, float],
    *,
    width_ratio: float,
    height_ratio: float,
    around: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Return a viewport sized as a fraction of ``extent``.

    Centred on the extent by default. Pass ``around`` — an anchor envelope — to
    centre it on real data instead, which is what keeps a "dense" viewport dense:
    a box centred on the extent's midpoint contains nothing at all when the
    features are clustered off-centre.

    ⚠ When an anchor is given the result is **expanded to contain it**. A box
    that excluded its own anchor would be the one thing a caller asking for
    "dense around this feature" can never use, and the ratio is a size request,
    not a promise that the anchor is smaller than it.
    """
    min_x, min_y, max_x, max_y = extent
    half_width = max((max_x - min_x) * width_ratio / 2, _PADDING)
    half_height = max((max_y - min_y) * height_ratio / 2, _PADDING)

    if around is None:
        center_x = min_x + (max_x - min_x) / 2
        center_y = min_y + (max_y - min_y) / 2
    else:
        a_min_x, a_min_y, a_max_x, a_max_y = around
        center_x = a_min_x + (a_max_x - a_min_x) / 2
        center_y = a_min_y + (a_max_y - a_min_y) / 2
        half_width = max(half_width, (a_max_x - a_min_x) / 2 + _PADDING)
        half_height = max(half_height, (a_max_y - a_min_y) / 2 + _PADDING)

    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def bbox_around_feature(conn) -> tuple[float, float, float, float]:
    """Return a small viewport guaranteed to contain one canonical feature.

    ⚠ **The smallest envelope, not the first row.** `ORDER BY id` picks an
    arbitrary feature from a table that holds `MEX:state` and `MEX:municipality`
    polygons alongside blocks and buildings — so on real data the "small"
    viewport could just as easily cover an entire entidad. Ordering by envelope
    area makes the result bounded by construction.

    ⚠ **`id` breaks the tie**, because area alone is not a total order: any two
    equally sized features would leave the choice to the planner and the
    benchmark would stop being reproducible across runs.

    ⚠ **No `type_slug` filter.** Restricting to blocks would look tidier and is
    wrong twice: `tests/performance` seeds `global:building` and no blocks at
    all, so it would raise there; and the partial GiST indexes on blocks and lots
    are only reachable from a query that *itself* filters by type, which the
    benchmark this feeds does not. Exercising those indexes needs its own
    benchmark, not a different anchor.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              ST_XMin(ST_Envelope(geometry)),
              ST_YMin(ST_Envelope(geometry)),
              ST_XMax(ST_Envelope(geometry)),
              ST_YMax(ST_Envelope(geometry))
            FROM curated_geo.feature
            WHERE geometry IS NOT NULL
              AND NOT ST_IsEmpty(geometry)
            ORDER BY ST_Area(ST_Envelope(geometry)), id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None or any(value is None for value in row):
        raise ValueError(
            "No canonical features are loaded. Benchmarks require feature data."
        )
    min_x, min_y, max_x, max_y = (float(value) for value in row)
    return (
        min_x - _PADDING,
        min_y - _PADDING,
        max_x + _PADDING,
        max_y + _PADDING,
    )

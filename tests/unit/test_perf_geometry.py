"""Benchmark inputs must be deterministic and bounded.

A benchmark is only as meaningful as the region it reads. These pin the two
properties that stopped holding: the "small" anchor became an arbitrary feature
(which on real data can be a whole state), and the "dense" viewport became a
byte-identical copy of the small one.
"""

from __future__ import annotations

import pytest

from reimagina.perf.geometry import bbox_around_feature, bbox_from_extent

EXTENT = (-100.0, 19.0, -98.0, 20.0)  # 2° x 1°


class _Cursor:
    def __init__(self, recorder, row):
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, *_):
        self._recorder.append(" ".join(sql.split()))

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.queries: list[str] = []
        self._row = row

    def cursor(self):
        return _Cursor(self.queries, self._row)


# ── The anchor ──────────────────────────────────────────────────────────────


def test_the_anchor_is_the_smallest_valid_envelope_broken_by_id():
    """`ORDER BY id` alone picked an arbitrary row from a mixed-type table.

    `curated_geo.feature` holds `MEX:state` polygons beside blocks and
    buildings, so the "small" viewport could cover an entire entidad. Area makes
    it bounded; `id` makes it reproducible, since area alone is not a total
    order and would leave equally sized features to the planner.
    """
    conn = _Connection((-99.14, 19.43, -99.137, 19.433))
    bbox_around_feature(conn)

    sql = conn.queries[0]
    assert "ORDER BY ST_Area(ST_Envelope(geometry)), id" in sql
    assert "geometry IS NOT NULL" in sql
    assert "NOT ST_IsEmpty(geometry)" in sql
    # Restricting by type would raise on the perf fixture, which seeds
    # global:building and no blocks at all.
    assert "type_slug" not in sql


def test_the_anchor_is_padded_so_a_point_feature_still_matches():
    conn = _Connection((-99.14, 19.43, -99.14, 19.43))  # zero-area
    min_x, min_y, max_x, max_y = bbox_around_feature(conn)
    assert max_x > min_x and max_y > min_y


def test_no_features_is_an_error_not_an_empty_box():
    with pytest.raises(ValueError, match="No canonical features"):
        bbox_around_feature(_Connection(None))


# ── The viewport ────────────────────────────────────────────────────────────


def test_an_unanchored_viewport_is_centred_on_the_extent():
    min_x, min_y, max_x, max_y = bbox_from_extent(
        EXTENT, width_ratio=0.08, height_ratio=0.08
    )
    assert (min_x + max_x) / 2 == pytest.approx(-99.0)
    assert (min_y + max_y) / 2 == pytest.approx(19.5)
    assert max_x - min_x == pytest.approx(2.0 * 0.08)


def test_an_anchored_viewport_is_centred_on_the_anchor_and_keeps_its_size():
    """Why `bbox_dense` is anchored rather than extent-centred.

    A box centred on the extent's midpoint contains nothing when the data is
    clustered off-centre — which is the failure the anchor was introduced to fix
    before it went too far and collapsed dense onto small.
    """
    anchor = (-99.5001, 19.2001, -99.4999, 19.2003)
    min_x, min_y, max_x, max_y = bbox_from_extent(
        EXTENT, width_ratio=0.08, height_ratio=0.08, around=anchor
    )
    assert (min_x + max_x) / 2 == pytest.approx(-99.5)
    assert max_x - min_x == pytest.approx(2.0 * 0.08)  # size still from the ratio
    assert min_x < anchor[0] and max_x > anchor[2]  # and it contains the anchor


def test_an_anchor_larger_than_the_ratio_expands_the_viewport():
    """A box that excluded its own anchor is the one thing a caller cannot use.

    The ratio is a size request, not a promise that the anchor fits inside it.
    """
    anchor = (-99.9, 19.1, -98.1, 19.9)  # far wider than 8% of the extent
    min_x, min_y, max_x, max_y = bbox_from_extent(
        EXTENT, width_ratio=0.08, height_ratio=0.08, around=anchor
    )
    assert min_x < anchor[0] and max_x > anchor[2]
    assert min_y < anchor[1] and max_y > anchor[3]


def test_small_and_dense_are_not_the_same_viewport():
    """The finding: both keys were built from the identical anchor expression."""
    anchor = (-99.14, 19.43, -99.137, 19.433)
    dense = bbox_from_extent(EXTENT, width_ratio=0.08, height_ratio=0.08, around=anchor)
    assert dense != anchor
    assert (dense[2] - dense[0]) > (anchor[2] - anchor[0])

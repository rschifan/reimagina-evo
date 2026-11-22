"""Removing an override stales its dependents' scenario rows too.

Composites read no attribute (their `reads` is empty), so selecting by
`attribute_key in spec.reads` alone left a livability score current after the
rule behind it was removed, and the recompute never rewrites a block whose
input is gone.
"""

from __future__ import annotations

from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    LIVABILITY_BLOCK_SCORE,
)
from reimagina.scenarios.authoring import _supersede_unbacked_results


class _Cursor:
    def __init__(self, sink):
        self.sink, self.rowcount = sink, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, params):
        self.sink.append(params)

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.params: list[dict] = []

    def cursor(self):
        return _Cursor(self.params)


def test_a_removed_input_stales_the_composite_that_consumes_it():
    conn = _Conn()

    _supersede_unbacked_results(conn, "s1", [("block-x", "unit_size")])

    # [0] deletes rows of blocks with no override left, [1] stales these,
    # [2] drops the partition's materialisation records for these slugs.
    params = conn.params[1]
    staled = set(zip(params["slugs"], params["fids"], strict=True))
    assert (EDIFICABILITY_BLOCK_DENSITY.slug, "block-x") in staled
    assert (LIVABILITY_BLOCK_SCORE.slug, "block-x") in staled
    # And freshness may not vouch for either any more.
    _partition, keys = conn.params[2]
    assert {k.split(".", 1)[1] for k in keys} >= {
        EDIFICABILITY_BLOCK_DENSITY.slug,
        LIVABILITY_BLOCK_SCORE.slug,
    }

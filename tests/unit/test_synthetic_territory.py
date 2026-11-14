"""The generated territory's references must be unique at any grid size.

A duplicate reference does not fail; it is absorbed. `load_features` inserts
with ON CONFLICT (source_record_ref) DO NOTHING, so a collision silently
reduces the row count, and the AGEB geometries are built by grouping into a
dict keyed on the same string, so a collision there *merges* two AGEBs into
one with the union of their extents. The second failure is the dangerous one:
it yields a wrong polygon rather than a missing one.

This was a real defect. `f"SYN{row:02d}{col:02d}"` is unambiguous only while
both indices stay under 100 — row 101 column 0 and row 10 column 100 both
render "SYN10100". Every demonstration had used a 4x4 grid, so it went unseen
until a 150x150 run came back with 22,250 blocks of 22,500 and 220 AGEBs of
225.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.synthetic_territory import build  # noqa: E402


# 150 is the size that exposed the original bug; 37 with a 4-block AGEB is a
# grid that divides evenly into neither, so partial AGEBs at two edges are
# covered too. 4 is the default the documentation quotes.
@pytest.mark.parametrize(
    ("blocks_per_side", "ageb_side"), [(4, 2), (10, 3), (37, 4), (150, 10)]
)
def test_every_generated_reference_is_unique(blocks_per_side, ageb_side):
    agebs, blocks, buildings = build(blocks_per_side, ageb_side=ageb_side)
    for layer, frame in (("ageb", agebs), ("block", blocks), ("building", buildings)):
        refs = list(frame["ref"])
        assert len(set(refs)) == len(refs), (
            f"{layer} references collide at blocks_per_side={blocks_per_side}: "
            f"{len(refs) - len(set(refs))} of {len(refs)} would be silently "
            "dropped or merged"
        )


@pytest.mark.parametrize(
    ("blocks_per_side", "ageb_side"), [(4, 2), (10, 3), (37, 4), (150, 10)]
)
def test_the_grid_has_the_shape_it_was_asked_for(blocks_per_side, ageb_side):
    agebs, blocks, _buildings = build(blocks_per_side, ageb_side=ageb_side)
    assert len(blocks) == blocks_per_side**2
    # Ceiling division: a grid that does not divide evenly keeps its partial
    # AGEBs at the edge rather than dropping the blocks in them.
    per_side = -(-blocks_per_side // ageb_side)
    assert len(agebs) == per_side**2


def test_every_block_names_an_ageb_that_exists():
    agebs, blocks, _buildings = build(37, ageb_side=4)
    assert set(blocks["parent_ref"]) <= set(agebs["ref"])
    # And every AGEB is used: an AGEB with no children would be an empty
    # polygon in the rollup, which averages over nothing.
    assert set(agebs["ref"]) == set(blocks["parent_ref"])

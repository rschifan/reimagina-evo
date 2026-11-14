"""Every spatial predicate over an AOI polygon must repair it the same way.

`compute_overlaps` has repaired AOI geometry since r004:

    CASE WHEN ST_IsValid(geometry) THEN geometry
         ELSE ST_CollectionExtract(ST_MakeValid(geometry), 3) END

because INEGI polygons are routinely self-intersecting. Any other code path
that runs a predicate against the same column and *doesn't* repair it is
inconsistent with that decision, and the inconsistency is invisible until the
two disagree.

⚠ `_clip_buildings_to_aoi` was such a path, and it is a DELETE — the one place
where disagreeing with `compute_overlaps` costs data rather than a wrong
answer. Found while investigating a load whose clip removed every row.

This test does not claim invalid geometry caused that: the obvious theory was
tested and failed — a bowtie has ST_Area 0 yet ST_Intersects still returns true
for a point on its surface, so GEOS is more robust than the theory needed. What
it pins is the weaker, true thing: the repair is applied consistently, so the
question cannot come back.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from reimagina.pipeline import overture
from reimagina.storage.postgres import repositories

#: The repair, as r004 spelled it. Matched loosely on both halves rather than
#: as one string, because the SQL is formatted differently in each file.
_VALIDITY_GUARD = "ST_IsValid"
_REPAIR = "ST_MakeValid"
_POLYGONS_ONLY = "ST_CollectionExtract"


def test_the_building_clip_repairs_the_aoi_polygon():
    source = inspect.getsource(overture._clip_buildings_to_aoi)
    assert "DELETE FROM curated_geo.feature" in source, (
        "this test is pinned to the clip being a DELETE; if it stopped being "
        "one, re-read whether the repair still matters"
    )
    for token in (_VALIDITY_GUARD, _REPAIR, _POLYGONS_ONLY):
        assert token in source, (
            f"{token} missing from _clip_buildings_to_aoi; it runs "
            f"ST_Intersects against curated_geo.area_of_interest.geometry and "
            f"must repair it the way compute_overlaps does"
        )


def test_compute_overlaps_is_still_the_reference_implementation():
    """If r004's repair ever disappears from here, this test is measuring nothing."""
    source = Path(repositories.__file__).read_text()
    for token in (_VALIDITY_GUARD, _REPAIR, _POLYGONS_ONLY):
        assert token in source, f"{token} missing from the storage repositories"


def test_the_repair_keeps_only_polygonal_parts():
    """`ST_CollectionExtract(..., 3)` — the 3 matters.

    MakeValid on a self-intersecting polygon can return a collection carrying
    stray lines or points. Keeping those would make a predicate match on
    geometry the AOI does not actually cover.
    """
    source = inspect.getsource(overture._clip_buildings_to_aoi)
    assert "ST_CollectionExtract(ST_MakeValid(geometry), 3)" in source

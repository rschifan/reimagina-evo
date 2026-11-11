"""The synthetic territory must seed one amenity per accessibility category.

**Why this is a contract test and not a nicety.** A category with no amenity in
the territory does not fail anything. ``pgr_drivingDistance`` given a NULL start
array returns zero rows rather than raising, so every block's ``agg_cost`` comes
back NULL, ``bounded_distance`` reports the 1,500 m cutoff, and the indicator
writes a full set of rows that are all identical. ``scripts/acceptance_run.sh``
then passes — the right number of rows, no error, a populated choropleth — while
shipping a layer that measures nothing.

That is not hypothetical. It is what the real Mexicali pilot did for ``park``:
the IMIP amenity layer has no park-like category, so all 19,058 blocks reported
1,500 m and the map drew a uniform surface that reads as a finding about
Mexicali's park access. It went unnoticed because 1,500 m looks like a
measurement in a way 0.0 does not.

So the acceptance run — the one artifact that is meant to be reproducible
anywhere and is cited as the Schedule A criterion — must not be able to
reproduce that failure. Adding a category to ``ACCESSIBILITY_CATEGORIES``
without adding a destination to the generator breaks this test.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reimagina.indicators.catalog import (  # noqa: E402
    ACCESSIBILITY_CATEGORIES,
)
from scripts.synthetic_territory import build_amenities  # noqa: E402


def test_every_accessibility_category_has_a_synthetic_destination():
    amenities = build_amenities(8)
    seeded = set(amenities["category"])
    missing = set(ACCESSIBILITY_CATEGORIES) - seeded
    assert not missing, (
        f"ACCESSIBILITY_CATEGORIES has {sorted(missing)} with no amenity in the "
        "synthetic territory. The acceptance run will report the 1,500 m cutoff "
        "for every block of those indicators and still pass. Add a destination "
        "to build_amenities in scripts/synthetic_territory.py."
    )


def test_the_seeded_values_are_the_bare_category_keys():
    """The generator writes ``category`` straight into ``attributes->>'category'``,
    which the indicator matches with ``= ANY(...)``. So the seeded string has to
    be one the category's tuple actually contains — the bare English key, which
    is what those tuples carry them for."""
    amenities = build_amenities(8)
    for category in amenities["category"]:
        assert category in ACCESSIBILITY_CATEGORIES, (
            f"{category!r} is seeded but is not a category key"
        )
        assert category in ACCESSIBILITY_CATEGORIES[category], (
            f"{category!r} is seeded but ACCESSIBILITY_CATEGORIES[{category!r}] "
            f"does not list it, so the indicator will not match it"
        )


def test_destinations_sit_on_distinct_intersections():
    """Two amenities at one point make two indicators identical, which hides a
    category wired to the wrong destination set."""
    amenities = build_amenities(8)
    points = {(p.x, p.y) for p in amenities.geometry}
    assert len(points) == len(amenities), "two synthetic amenities share a point"


def test_one_amenity_per_category_exactly():
    amenities = build_amenities(8)
    counts = amenities["category"].value_counts().to_dict()
    assert counts == {c: 1 for c in ACCESSIBILITY_CATEGORIES}, counts

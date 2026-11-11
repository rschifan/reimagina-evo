"""A category that matches nothing must say so, because the numbers will not.

`accessibility.py` guards the establishment *layer* and deliberately not the
category, and that decision stands —
`test_no_amenity_of_that_category_reports_the_cutoff` pins it: a park that is
genuinely far away should report the cutoff.

⚠ But the comment defending that choice also describes its failure mode one
level down: "every block reports the cutoff and the result reads as a real,
uniformly bad accessibility surface ... harder to spot because 1500 m looks
like a measurement in a way 0.0 does not."

That is precisely what the first real-data run produced.
`accessibility_block_park_network_m` returned **1500.0 for all 19,058**
Mexicali blocks, because the IMIP amenity file carries no park-like category
at all — its five categories are `Cine`, `Consultorio medico`, `Farmacia`,
`Guarderia`, `Hospital`. The surface was uniform, the run said `"status":
"ok"`, and nothing distinguished "no parks nearby" from "no parks in the
data".

So the warning below changes no value and refuses nothing. It makes the
degenerate case visible, and names the categories that *are* present — the one
fact needed to tell a real absence from a source that labels the same thing
differently.
"""

from __future__ import annotations

import logging

from reimagina.indicators.accessibility import _warn_if_category_absent
from reimagina.indicators.catalog import ACCESSIBILITY_CATEGORIES

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)
from reimagina.data import feature as feat


def _seed_establishments(conn, categories: list[str]) -> None:
    source_id = _register_source(
        conn,
        name="absent_cat_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    type_id = feat.get_type_id(conn, "global", "establishment")
    for index, category in enumerate(categories):
        _insert_feature(
            conn,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"poi-{index}",
            geometry_wkt=f"POINT(-115.48 {32.62 + index / 1000})",
            attributes={"category": category},
        )


def test_a_category_matching_nothing_warns(conn, caplog):
    """The Mexicali shape: a loaded layer with no park-like category."""
    _seed_establishments(conn, ["Cine", "Consultorio medico", "Farmacia"])

    with caplog.at_level(logging.WARNING, logger="reimagina.indicators.accessibility"):
        with conn.cursor() as cur:
            _warn_if_category_absent(
                cur,
                "accessibility_block_park_network_m",
                ACCESSIBILITY_CATEGORIES["park"],
            )

    assert caplog.records, "a category matching nothing must not pass silently"
    message = caplog.records[-1].getMessage()
    assert "accessibility_block_park_network_m" in message
    assert "cutoff" in message
    assert "not a measurement" in message
    # The categories that ARE present — what distinguishes an absence from a
    # naming mismatch.
    assert "Cine" in message and "Farmacia" in message
    # And where to fix it if it is a mismatch.
    assert "ACCESSIBILITY_CATEGORIES" in message


def test_a_category_with_matches_stays_quiet(conn, caplog):
    """Health matches three of Mexicali's five categories, so no warning."""
    _seed_establishments(conn, ["Consultorio medico", "Farmacia", "Hospital"])

    with caplog.at_level(logging.WARNING, logger="reimagina.indicators.accessibility"):
        with conn.cursor() as cur:
            _warn_if_category_absent(
                cur,
                "accessibility_block_health_network_m",
                ACCESSIBILITY_CATEGORIES["health"],
            )

    assert not caplog.records, (
        "a category with destinations must not warn; the warning has to stay "
        "rare enough to be read"
    )


def test_a_thin_match_stays_quiet_too(conn, caplog):
    """⚠ Mexicali's `school` matches only `Guarderia` — 79 nurseries.

    Thin is not absent, and this does not warn about it. Recorded so the
    limitation is not mistaken for a bug: 59.6% of blocks hit the cutoff for
    `school` on real data because the only matching category is nurseries,
    while the mapping also lists `Educacion_primaria`, `Educacion_secundaria`
    and the rest, none of which appear in that file.

    Judging "too few destinations" needs a threshold nobody has justified, so
    the warning stops at zero and the catalog's own description is what tells
    a planner what the number covers.
    """
    _seed_establishments(conn, ["Guarderia"])

    with caplog.at_level(logging.WARNING, logger="reimagina.indicators.accessibility"):
        with conn.cursor() as cur:
            _warn_if_category_absent(
                cur,
                "accessibility_block_school_network_m",
                ACCESSIBILITY_CATEGORIES["school"],
            )

    assert not caplog.records


def test_an_empty_layer_lists_no_categories_rather_than_crashing(conn, caplog):
    """The layer guard fires before this in production, but not in a unit call."""
    with caplog.at_level(logging.WARNING, logger="reimagina.indicators.accessibility"):
        with conn.cursor() as cur:
            _warn_if_category_absent(
                cur, "accessibility_block_park_network_m", ("park",)
            )

    assert caplog.records
    assert "(none)" in caplog.records[-1].getMessage()

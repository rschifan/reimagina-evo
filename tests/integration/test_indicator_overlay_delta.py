"""The delta view of ``serving_geo.indicator_overlay_tile`` (r049).

The map's job here is to paint the same answer ``GET
/scenarios/{id}/comparison`` gives in numbers. If the summary says "6 changed"
and the map colours 7, the user has found a bug before anyone else has — so
these tests do not restate the classification rules in their own words. They
assert the four-way table in ``reimagina.scenarios.comparison`` and, once,
assert the tile and ``compare()`` agree feature by feature over the same
fixture (``test_the_delta_tile_and_the_comparison_endpoint_agree``). That last
test is the one that would catch a rule drifting on one side only.

Two properties of the encoding are load-bearing and worth stating because the
assertions below lean on them:

- ``ST_AsMVT`` omits a NULL property from a feature's property map, so
  ``delta`` being *absent* is how "there is nothing to subtract from" arrives
  at the client. ``.get("delta") is None`` is therefore the assertion, not
  ``== None`` on a present key.
- the property *keys* a layer declares are the columns of the query, not the
  union of what the features carry. A delta column that is NULL for every
  feature of a value-mode tile would still put the string ``delta`` in the
  layer's key dictionary, which is why
  ``test_without_delta_the_tile_serves_values_and_nothing_else`` also checks
  the raw bytes: it pins "no delta column is emitted at all", not merely "no
  feature happens to carry one".

Skipped when ``mapbox_vector_tile`` is absent, the same posture as
``test_indicator_overlay_tile.py``.
"""

from __future__ import annotations

import json

import pytest

from reimagina.data import aoi as aoi_mod
from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
    seed_catalog,
)
from reimagina.scenarios.authoring import create_scenario
from reimagina.scenarios.comparison import CHANGED, NEW, UNCHANGED, compare
from reimagina.storage import backend_for_connection
from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

try:
    import mapbox_vector_tile  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    mapbox_vector_tile = None

pytestmark = pytest.mark.skipif(
    mapbox_vector_tile is None,
    reason="mapbox_vector_tile is not installed; add it to the dev deps to run tile decode tests",
)

_FLOORS = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
#: One of the five indicators that exist only under a scenario, so the ``new``
#: state is its normal state rather than its corner case.
_DENSITY = EDIFICABILITY_BLOCK_DENSITY.slug

#: Eight ~210 m squares in a row at 19.40°N — the same layout
#: ``test_scenario_comparison`` uses, placed so a rectangle can take any prefix
#: of them without clipping the next.
_LAT0, _LAT1 = 19.4000, 19.4020
_BLOCK_SIDE = 0.0020
_BLOCK_PITCH = 0.0040
_BLOCK_LON0 = -99.2000
_BLOCKS = 8

#: The one tile that covers all eight blocks. z=12 is above the function's
#: ``z >= 8`` floor and below its ``z >= 14`` no-simplify branch, so the
#: geometry it returns has been through ``ST_Simplify`` — which a square
#: survives.
_Z, _X, _Y = 12, 919, 1822


def _block_wkt(i: int) -> str:
    lon = _BLOCK_LON0 + i * _BLOCK_PITCH
    return (
        f"POLYGON(({lon} {_LAT0},{lon + _BLOCK_SIDE} {_LAT0},"
        f"{lon + _BLOCK_SIDE} {_LAT1},{lon} {_LAT1},{lon} {_LAT0}))"
    )


def _rect(lon_min: float, lon_max: float) -> str:
    lat_min, lat_max = _LAT0 - 0.001, _LAT1 + 0.001
    return (
        f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},"
        f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min}))"
    )


#: Every block.
_AOI_WIDE = _rect(-99.2010, -99.1690)
#: Blocks 0..2 only — block 3 starts at -99.1880.
_AOI_NARROW = _rect(-99.2010, -99.1890)


class _Territory:
    """A project, eight blocks, two AOIs and a scenario over the wide one."""

    def __init__(self, conn):
        self.conn = conn
        self.project_id = insert_project(conn, "Delta Tile Project")
        type_id = get_type_id(conn, "MEX", "block")
        source_id = register_source(
            conn,
            name="delta_tile_src",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        ingest = start_run(conn, source_id)
        self.blocks = [
            insert_feature(
                conn,
                type_id=type_id,
                source_id=source_id,
                ingestion_run_id=ingest,
                dataset_version="v1",
                source_record_ref=f"delta-blk-{i}",
                geometry_wkt=_block_wkt(i),
            )
            for i in range(_BLOCKS)
        ]
        self.aoi_wide = self._aoi("delta wide", _AOI_WIDE)
        self.aoi_narrow = self._aoi("delta narrow", _AOI_NARROW)
        self.scenario_id = create_scenario(
            conn, project_id=self.project_id, name="Upzoning", aoi_id=self.aoi_wide
        )

    def _aoi(self, name: str, wkt: str) -> str:
        aoi_id = aoi_mod.create_aoi(
            self.conn, project_id=self.project_id, name=name, geometry_wkt=wkt
        )
        aoi_mod.compute_aoi_features(self.conn, aoi_id)
        return aoi_id


def _write(conn, *, slug, values, scenario_id=None):
    """``indicator_result`` rows through the repository's own write path.

    The ``job_run`` carries the same ``scenario_id`` as the rows because
    r016's before-write trigger refuses a scenario result filed under a
    baseline run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id) "
            "VALUES ('test', 'succeeded', 'test', %s) RETURNING id",
            (scenario_id,),
        )
        job_run_id = cur.fetchone()[0]
    backend_for_connection(conn).indicator_result_repo().bulk_write_numeric_results(
        list(values.items()),
        indicator_slug=slug,
        job_run_id=job_run_id,
        scenario_id=scenario_id,
    )


def _write_valueless(conn, *, slug, feature_id, scenario_id=None):
    """One ``current`` row carrying no number.

    ``write_result(value_numeric=None, quality_flag='missing')`` is supported
    and ``'missing'`` is a valid flag, so this row exists by design. It
    supersedes whatever ``current`` row the same (feature, indicator,
    scenario) already had. ``missing_reason`` is not decoration: r016's
    before-write trigger refuses ``'missing'`` without one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id) "
            "VALUES ('test', 'succeeded', 'test', %s) RETURNING id",
            (scenario_id,),
        )
        job_run_id = cur.fetchone()[0]
    backend_for_connection(conn).indicator_result_repo().write_result(
        feature_id=feature_id,
        indicator_slug=slug,
        job_run_id=job_run_id,
        value_numeric=None,
        quality_flag="missing",
        missing_reason="no destination in range",
        scenario_id=scenario_id,
    )


def _tile(conn, **params) -> bytes:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT serving_geo.indicator_overlay_tile(%s, %s, %s, %s::json)",
            (_Z, _X, _Y, json.dumps(params)),
        )
        raw = cur.fetchone()[0]
    assert raw is not None, (
        "the tile function returns bytes, possibly empty, never NULL"
    )
    return bytes(raw)


def _features(tile: bytes) -> list[dict]:
    if not tile:
        return []
    return (
        mapbox_vector_tile.decode(tile).get("indicator_overlay", {}).get("features", [])
    )


class DuplicateFeature(AssertionError):
    """One tile carried the same ``feature_id`` twice.

    A named subclass rather than a bare ``assert`` so that an ``xfail`` can
    name *this* failure and nothing else. An ``xfail`` that accepts any
    exception is a canary that has stopped singing: a broken fixture, a SQL
    error or a rewritten helper would keep the marker green-by-expectation and
    the signal would be silently dead. ``AssertionError`` stays the base class
    so every other caller behaves exactly as before.
    """


def _props(tile: bytes) -> dict[str, dict]:
    """``feature_id`` to properties, refusing a feature drawn twice.

    The refusal is the point of the dict: a double-drawn feature would
    otherwise be silently collapsed by the last writer, and double-drawing is
    the failure the baseline/scenario anti-join exists to prevent.
    """
    features = _features(tile)
    by_id: dict[str, dict] = {}
    for feature in features:
        properties = feature["properties"]
        feature_id = properties["feature_id"]
        if feature_id in by_id:
            raise DuplicateFeature(
                f"{feature_id} appears twice in one tile: "
                f"{by_id[feature_id]} and {properties}"
            )
        by_id[feature_id] = properties
    assert len(by_id) == len(features)
    return by_id


@pytest.fixture
def territory(conn):
    # Plain in-transaction seed, not seed_catalog_committed: that helper leaves
    # autocommit on, so every write here would outlive the per-test rollback.
    seed_catalog(conn)
    return _Territory(conn)


@pytest.fixture
def upzoned(territory, conn):
    """Floors: a baseline on all eight blocks, a scenario that moves two.

    Block 0 rises to 9, block 1 to 5, block 2 is recomputed to the value it
    already had, and blocks 3..7 have no scenario row at all — so all three
    states are present, and the untouched majority outnumbers the changed
    minority 6 to 2. The same fixture ``test_scenario_comparison`` classifies.
    """
    blocks = territory.blocks
    _write(conn, slug=_FLOORS, values=dict.fromkeys(blocks, 3.0))
    _write(
        conn,
        slug=_FLOORS,
        values={blocks[0]: 9.0, blocks[1]: 5.0, blocks[2]: 3.0},
        scenario_id=territory.scenario_id,
    )
    return territory


@pytest.fixture
def densified(territory, conn):
    """Density: no baseline anywhere, two scenario-only rows."""
    _write(
        conn,
        slug=_DENSITY,
        values={territory.blocks[0]: 120.0, territory.blocks[1]: 80.0},
        scenario_id=territory.scenario_id,
    )
    return territory


# ── the value tile is untouched ─────────────────────────────────────────────


def test_without_delta_the_tile_serves_values_and_nothing_else(upzoned, conn):
    """A scenario tile asked for no delta is r043's overlay tile, unchanged.

    Structural rather than byte-identical against the old function: once the
    migrations have run, the pre-r049 definition does not exist to render.
    What is pinned instead is everything a client can observe — which features
    the tile carries, which properties each one carries, and that the strings
    ``delta`` and ``state`` are nowhere in the encoded tile, key dictionary
    included.
    """
    tile = _tile(conn, indicator_slug=_FLOORS, scenario_id=upzoned.scenario_id)
    properties = _props(tile)

    assert set(properties) == set(upzoned.blocks)
    for feature_id, props in properties.items():
        assert set(props) == {
            "feature_id",
            "type_slug",
            "value_numeric",
            "quality_flag",
        }, feature_id

    # The layer's key dictionary, not just the per-feature property maps: an
    # all-NULL delta column would leave the key here while every feature
    # looked clean.
    assert b"delta" not in tile
    assert b"state" not in tile


def test_a_value_tile_draws_each_block_once(upzoned, conn):
    """The anti-join, from the one side a tile can observe it.

    Blocks 0..2 carry both a baseline row and a scenario row. Widening the
    predicate to ``scenario_id = X OR scenario_id IS NULL`` makes both match,
    and ``ST_AsMVT`` emits both — two features with the same ``feature_id``,
    one painted with the scenario's value and one with the baseline's, drawn
    in whichever order the planner returned them.
    """
    properties = _props(
        _tile(conn, indicator_slug=_FLOORS, scenario_id=upzoned.scenario_id)
    )

    assert len(properties) == _BLOCKS
    assert properties[upzoned.blocks[0]]["value_numeric"] == pytest.approx(9.0)
    assert properties[upzoned.blocks[3]]["value_numeric"] == pytest.approx(3.0)


def test_delta_false_is_not_a_delta_request(upzoned, conn):
    """``delta=false`` serves values.

    The parameter is read as a boolean the way Postgres reads one, so only a
    recognised truth word turns the delta view on. Anything else — ``false``,
    a typo, a stray ``0`` — falls back to the value tile, which is the
    behaviour every client had before this migration.
    """
    for raw in ("false", "0", "no", "maybe", ""):
        properties = _props(
            _tile(
                conn,
                indicator_slug=_FLOORS,
                scenario_id=upzoned.scenario_id,
                delta=raw,
            )
        )
        assert set(properties) == set(upzoned.blocks), raw
        assert all("value_numeric" in p for p in properties.values()), raw
        assert all("delta" not in p for p in properties.values()), raw


#: Spellings worth asking about: the obvious ones, Postgres's unique prefixes,
#: whitespace of the kind a hand-assembled URL carries, the false words, and
#: strings Postgres rejects outright — ``o`` among them, since it is ambiguous
#: between ``on`` and ``off``.
_DELTA_SPELLINGS = [
    "true",
    "TRUE",
    "True",
    "tru",
    "tr",
    "t",
    "T",
    " true",
    "true ",
    " true ",
    "\ttrue\n",
    "yes",
    "ye",
    "y",
    "on",
    "ON",
    "1",
    "false",
    "f",
    "no",
    "n",
    "off",
    "0",
    "o",
    "01",
    "truex",
    "maybe",
    "",
    "  ",
]


def test_the_delta_flag_is_read_exactly_as_postgres_reads_a_boolean(upzoned, conn):
    """The vocabulary is pinned against the parser, not against a comment.

    The migration spells the true-words out in SQL rather than calling
    ``pg_input_is_valid(…, 'boolean')``, which would need PostgreSQL 16 on
    every stack this repository runs on. A hand-copied list is exactly the
    thing that drifts, so this test asks *the server* what each spelling means
    and requires the tile to agree — including on the spellings the list is
    easiest to get wrong: the unique prefixes (``tr``, ``tru``, ``ye``) and a
    value with whitespace around it.

    Getting those wrong fails silently in the worst direction: ``?delta=true ``
    serves the value tile to a client that asked for a difference, under a
    property it is not reading.

    ⚠ The *oracle* needs PostgreSQL 16 — the same constraint that kept
    ``pg_input_is_valid`` out of the migration, which is why this is a skip and
    not a smaller test. ``pg_input_is_valid`` is the only way to ask Postgres
    "is this a boolean?" without a cast that raises, and a cast that raises
    aborts the transaction this fixture lives in. On an older server the
    vocabulary is therefore unchecked here, and the explicit list in the
    migration is what keeps the tile working at all: do not "simplify" the
    migration to use ``pg_input_is_valid`` on the strength of this test
    passing, or the function stops installing on the stack this skip exists
    for.
    """
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        version = int(cur.fetchone()[0])
    if version < 160000:
        pytest.skip(
            f"pg_input_is_valid needs PostgreSQL 16; this server is "
            f"{version}. The migration deliberately does not use it either — "
            f"see this test's docstring before changing the SQL."
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw,
                   CASE WHEN pg_input_is_valid(btrim(raw), 'boolean')
                        THEN btrim(raw)::boolean ELSE false END
              FROM unnest(%s::text[]) AS s(raw)
            """,
            (_DELTA_SPELLINGS,),
        )
        postgres_says = dict(cur.fetchall())

    # The harness itself, before it is trusted to judge the tile: both answers
    # must be represented, and the three spellings this test exists for must be
    # the ones Postgres says they are.
    assert postgres_says["tru"] is True
    assert postgres_says["true "] is True
    assert postgres_says["o"] is False, "ambiguous between on and off"
    assert sum(postgres_says.values()) >= 15
    assert sum(1 for v in postgres_says.values() if not v) >= 10

    for raw, is_true in postgres_says.items():
        properties = _props(
            _tile(
                conn,
                indicator_slug=_FLOORS,
                scenario_id=upzoned.scenario_id,
                delta=raw,
            )
        )
        served_delta = all("state" in p for p in properties.values())
        assert properties, raw
        assert served_delta is is_true, (
            f"{raw!r}: Postgres reads this as {is_true}, the tile served "
            f"{'delta' if served_delta else 'values'}"
        )


# ── the delta tile ──────────────────────────────────────────────────────────


def test_delta_emits_the_difference_where_both_values_exist(upzoned, conn):
    properties = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            delta="true",
        )
    )

    changed = properties[upzoned.blocks[0]]
    assert changed["state"] == CHANGED
    assert changed["delta"] == pytest.approx(9.0 - 3.0)
    assert properties[upzoned.blocks[1]]["delta"] == pytest.approx(5.0 - 3.0)
    assert properties[upzoned.blocks[1]]["state"] == CHANGED


def test_the_delta_tile_carries_no_value_property(upzoned, conn):
    """``delta`` and ``state`` replace the value; they do not join it.

    A difference drawn by the sequential value ramp reads as a value, so the
    two views are kept apart at the property level: a client that forgot to
    switch ramps paints nothing rather than painting a lie.

    The byte check is the mirror of the value tile's, and for the same reason:
    a column that is NULL for every feature is invisible per feature but still
    declares its key in the layer's dictionary, which is the whole argument for
    building the two views as a ``CASE`` rather than as a ``UNION ALL`` over
    one shared column list. Asserting it on both tiles is what makes that
    argument checkable from either side.
    """
    tile = _tile(
        conn,
        indicator_slug=_FLOORS,
        scenario_id=upzoned.scenario_id,
        delta="true",
    )
    properties = _props(tile)

    # Every block in this fixture has a baseline, so every feature carries a
    # delta and the property set is exact rather than a subset.
    for feature_id, props in properties.items():
        assert set(props) == {"feature_id", "type_slug", "delta", "state"}, feature_id

    assert b"value_numeric" not in tile
    assert b"quality_flag" not in tile


def test_a_recomputed_block_that_did_not_move_is_unchanged(upzoned, conn):
    """Block 2's scenario row equals its baseline.

    A row's existence is not a change: classifying on presence would call
    this ``changed`` with a delta of zero, which is a contradiction the map
    then paints as impact.
    """
    props = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            delta="true",
        )
    )[upzoned.blocks[2]]

    assert props["state"] == UNCHANGED
    assert props["delta"] == pytest.approx(0.0)


def test_a_block_the_scenario_never_touched_is_unchanged_at_zero(upzoned, conn):
    """Blocks 3..7 have no scenario row at all — the common case.

    Their effective value is the baseline's, by the overlay rule, so they are
    ``unchanged`` with a zero delta rather than absent. Classifying on the raw
    scenario column instead would leave five of eight blocks unpainted, and on
    a real AOI nearly all of them.
    """
    properties = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            delta="true",
        )
    )

    assert set(properties) == set(upzoned.blocks)
    for block in upzoned.blocks[3:]:
        assert properties[block]["state"] == UNCHANGED, block
        assert properties[block]["delta"] == pytest.approx(0.0), block


def test_delta_marks_a_scenario_only_value_new(densified, conn):
    """The edificability shape: a value where the city has none.

    ``delta`` is absent rather than equal to the scenario's own number. A
    value with nothing to compare against is not a change of that size, and a
    diverging ramp centred on zero would otherwise paint "from nothing to 120"
    as the largest improvement on the map.
    """
    properties = _props(
        _tile(
            conn,
            indicator_slug=_DENSITY,
            scenario_id=densified.scenario_id,
            delta="true",
        )
    )

    assert set(properties) == {densified.blocks[0], densified.blocks[1]}
    for block in (densified.blocks[0], densified.blocks[1]):
        assert properties[block]["state"] == NEW, block
        assert properties[block].get("delta") is None, block


def test_a_valueless_scenario_row_does_not_shadow_the_baseline(upzoned, conn):
    """A ``missing``-flagged scenario row leaves the baseline standing.

    The same rule ``scenario_overlay`` and ``comparison._joined`` apply: the
    overlay is a COALESCE over *values*, and COALESCE skips a NULL. So block 0
    reverts to reading ``unchanged`` at its baseline — not ``new``, and not
    absent.
    """
    _write_valueless(
        conn,
        slug=_FLOORS,
        feature_id=upzoned.blocks[0],
        scenario_id=upzoned.scenario_id,
    )

    props = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            delta="true",
        )
    )[upzoned.blocks[0]]

    assert props["state"] == UNCHANGED
    assert props["delta"] == pytest.approx(0.0)


def test_the_value_tile_draws_a_shadowed_block_once(upzoned, conn):
    """The same fixture as the test above, asked of the *value* tile.

    Worth stating why this test exists at all rather than living in a note
    somewhere: the fixture one function up builds exactly the state that
    triggers the defect and then queries only the delta tile, so the suite sat
    beside the bug without touching it. Querying the value tile in that state
    is the whole difference.

    This carried a ``strict=True`` xfail against ``DuplicateFeature`` while
    r043's one-sided anti-join stood. r049's ``_OVERLAY`` now tests for a value
    on the scenario arm too, so the block survives once at its baseline value
    and the marker is gone — the assertions it guarded are unchanged.
    ``_props`` still raises ``DuplicateFeature`` rather than collapsing a
    repeated ``feature_id``, which is what keeps this a test of the defect and
    not of the renderer's draw order.
    """
    _write_valueless(
        conn,
        slug=_FLOORS,
        feature_id=upzoned.blocks[0],
        scenario_id=upzoned.scenario_id,
    )

    properties = _props(
        _tile(conn, indicator_slug=_FLOORS, scenario_id=upzoned.scenario_id)
    )

    assert set(properties) == set(upzoned.blocks)
    assert properties[upzoned.blocks[0]]["value_numeric"] == pytest.approx(3.0), (
        "a valueless scenario row falls through to the baseline, as it does on "
        "all six Python read paths"
    )


def test_the_value_tile_draws_a_valueless_block_with_no_baseline(densified, conn):
    """The other side of that fix, and why the scenario arm is not simply
    ``AND <has a value>``.

    Excluding valueless scenario rows outright would drop a ``'missing'`` row
    for a feature the baseline never computed out of *both* arms, and the
    feature would vanish from the tile — while a baseline tile draws exactly
    that row, carrying ``quality_flag`` and no ``value_numeric``. So the arm
    also admits a valueless row when the baseline has none, and the feature is
    drawn once with no number, which is what ``ST_AsMVT`` does with a NULL
    property.

    ``densified`` has no baseline anywhere, so block 2 — which has neither a
    baseline nor a scenario value until this writes one — is the clean case.
    """
    _write_valueless(
        conn,
        slug=_DENSITY,
        feature_id=densified.blocks[2],
        scenario_id=densified.scenario_id,
    )

    properties = _props(
        _tile(conn, indicator_slug=_DENSITY, scenario_id=densified.scenario_id)
    )

    assert densified.blocks[2] in properties, (
        "a valueless scenario row with no baseline to fall back to is still "
        "the scenario's answer for that feature, and must not vanish"
    )
    assert properties[densified.blocks[2]]["quality_flag"] == "missing"
    assert "value_numeric" not in properties[densified.blocks[2]], (
        "ST_AsMVT omits a NULL property, so absence is how no-value reaches the client"
    )


def test_a_feature_valueless_on_both_sides_is_absent(upzoned, conn):
    """The other half of the ``value_numeric IS NOT NULL`` filter, and the
    load-bearing one.

    Block 5's numeric baseline is superseded by a ``'missing'``-flagged row, so
    it now has no number on either side. Without the *baseline*-side filter it
    reaches the classification as ``(NULL, NULL)`` and comes back ``new`` with
    a null delta — a fifth case the table has no cell for, painted as though
    the scenario had added something where the city has nothing.
    ``comparison._joined`` drops it, so the tile must too.
    """
    _write_valueless(conn, slug=_FLOORS, feature_id=upzoned.blocks[5])

    properties = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            delta="true",
        )
    )

    assert upzoned.blocks[5] not in properties
    assert set(properties) == set(upzoned.blocks) - {upzoned.blocks[5]}
    assert {
        row["feature_id"]
        for row in compare(
            conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS
        ).rows
    } == set(properties), "and the endpoint drops exactly the same feature"


# ── the scoping a delta request must not escape ─────────────────────────────


def test_delta_without_a_scenario_is_an_empty_tile(upzoned, conn):
    """There is nothing to subtract from the baseline but the baseline.

    Serving the baseline's own values under the ``delta`` property would
    paint every block as unchanged, which is a finding ("my scenario did
    nothing") produced by a missing parameter. The function already answers a
    misconfigured caller with an empty tile when ``indicator_slug`` is absent;
    this is the same answer to the same kind of mistake.
    """
    assert _tile(conn, indicator_slug=_FLOORS, delta="true") == b""
    assert _tile(
        conn, indicator_slug=_FLOORS, delta="true", scenario_id="nonsense"
    ) == (b"")
    assert _tile(conn, scenario_id=upzoned.scenario_id, delta="true") == b""


def test_delta_honours_the_aoi_filter(upzoned, conn):
    """``aoi_id`` scopes a delta request exactly as it scopes a value request.

    Blocks 3..7 have values and intersect this tile, so they are excluded
    here because the narrow AOI does not contain them — not for want of a
    candidate geometry. Without the ``EXISTS``, the delta view would be the
    one path on which a feature outside the requested AOI reaches a client.
    """
    narrow = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            aoi_id=upzoned.aoi_narrow,
            delta="true",
        )
    )

    assert set(narrow) == set(upzoned.blocks[:3])

    wide = _props(
        _tile(
            conn,
            indicator_slug=_FLOORS,
            scenario_id=upzoned.scenario_id,
            aoi_id=upzoned.aoi_wide,
            delta="true",
        )
    )
    assert set(wide) == set(upzoned.blocks), "the wide AOI holds every block"


def test_delta_honours_the_feature_type_filter(upzoned, conn):
    """The grain filter still filters in delta mode.

    Every shipped indicator is block-grain and r016's write trigger refuses a
    row at any other grain, so asking for ``ageb`` can only return nothing —
    which is the assertion. It fails if the parameter is dropped from the
    delta branch, because the blocks would come back.
    """
    assert (
        _props(
            _tile(
                conn,
                indicator_slug=_FLOORS,
                scenario_id=upzoned.scenario_id,
                feature_type="ageb",
                delta="true",
            )
        )
        == {}
    )
    assert set(
        _props(
            _tile(
                conn,
                indicator_slug=_FLOORS,
                scenario_id=upzoned.scenario_id,
                feature_type="block",
                delta="true",
            )
        )
    ) == set(upzoned.blocks)


def test_a_delta_tile_below_the_zoom_floor_is_empty(upzoned, conn):
    """The floor is the function's, not the branch's.

    A choropleth at city scale is misleading whichever property it paints, and
    a delta drawn on one pixel per district is no better than a value.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT octet_length(serving_geo.indicator_overlay_tile(7, 28, 56, %s::json))",
            (
                json.dumps(
                    {
                        "indicator_slug": _FLOORS,
                        "scenario_id": upzoned.scenario_id,
                        "delta": "true",
                    }
                ),
            ),
        )
        assert cur.fetchone()[0] == 0


# ── the map and the endpoint say the same thing ─────────────────────────────


def _delta(value) -> float | None:
    """A delta, rounded so the two surfaces are compared and not the IEEE noise.

    The rounding is belt and braces rather than a fix for a known seam:
    ``indicator_result.value_numeric`` is ``double precision`` (r016:178), so
    Postgres's ``-`` and ``=`` and Python's are the same operations on the same
    bits. One qualifier, since "the same operation" is only true of finite
    values: Postgres's float8 ``=`` holds ``NaN = NaN`` true where Python's
    ``float('nan') == float('nan')`` is false, so a NaN value would be
    ``unchanged`` on the tile and ``changed`` in ``compare()``. r016's catalog
    bounds (``valid_min`` / ``valid_max``, enforced by the before-write
    trigger) make a NaN unwritable for every bounded indicator, which is why
    this is a qualifier on the argument and not a defect in it.
    """
    return None if value is None else round(float(value), 9)


def _comparison_states(conn, *, scenario_id, slug, aoi_id):
    """``compare()``'s answer, keyed the way the tile's properties are."""
    result = compare(conn, scenario_id=scenario_id, indicator_slug=slug, aoi_id=aoi_id)
    return {
        row["feature_id"]: (row["state"], _delta(row["delta"])) for row in result.rows
    }


def _tile_states(conn, *, scenario_id, slug, aoi_id):
    properties = _props(
        _tile(
            conn,
            indicator_slug=slug,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            delta="true",
        )
    )
    return {
        feature_id: (props["state"], _delta(props.get("delta")))
        for feature_id, props in properties.items()
    }


@pytest.mark.parametrize("slug", [_FLOORS, _DENSITY])
@pytest.mark.parametrize("aoi", ["wide", "narrow"])
def test_the_delta_tile_and_the_comparison_endpoint_agree(territory, conn, slug, aoi):
    """The headline: one classification, two surfaces.

    Both fixtures are loaded at once so that each parametrisation sees an
    indicator with a baseline *and* one without, and both are asked of both
    surfaces over the same AOI. A rule that drifts on one side — the
    ``COALESCE`` dropped, a valueless row allowed to shadow, equality called a
    change — shows up here as a per-feature disagreement rather than as a
    count that happens to match.

    Both AOIs, because the two surfaces apply the AOI at different levels of
    their queries: ``comparison._joined`` tests it once, outside the full outer
    join, against ``COALESCE(b.feature_id, s.feature_id)``, while the tile
    tests it against ``f.id`` after joining the comparison to ``feature``. The
    narrow AOI is the leg where that difference could show — it excludes five
    of the eight blocks, including one the scenario has a row for.
    """
    blocks = territory.blocks
    _write(conn, slug=_FLOORS, values=dict.fromkeys(blocks, 3.0))
    _write(
        conn,
        slug=_FLOORS,
        values={blocks[0]: 9.0, blocks[1]: 5.0, blocks[2]: 3.0},
        scenario_id=territory.scenario_id,
    )
    _write(
        conn,
        slug=_DENSITY,
        values={blocks[0]: 120.0, blocks[3]: 80.0},
        scenario_id=territory.scenario_id,
    )
    _write_valueless(
        conn, slug=_FLOORS, feature_id=blocks[4], scenario_id=territory.scenario_id
    )

    aoi_id = territory.aoi_wide if aoi == "wide" else territory.aoi_narrow
    expected = _comparison_states(
        conn,
        scenario_id=territory.scenario_id,
        slug=slug,
        aoi_id=aoi_id,
    )
    assert expected, "the fixture must give this indicator something to compare"
    assert (
        _tile_states(
            conn,
            scenario_id=territory.scenario_id,
            slug=slug,
            aoi_id=aoi_id,
        )
        == expected
    )

"""Comparing a scenario against the baseline it departs from.

The comparison is a ``FULL OUTER JOIN`` and not the ``LEFT JOIN`` the
acceptance script prints, because the five indicators a scenario exists to
answer — the edificability family — have no baseline at all, and a left join
anchored on the baseline drops every one of their rows.

The subtle half is the other direction. A scenario stores only what it
changed, so most blocks in an AOI have a baseline row and no scenario row.
Their scenario value is not unknown: it is the baseline, by the same overlay
rule ``storage._utils.scenario_overlay`` applies to every other scenario read.
Classifying on the raw scenario column instead of the resolved one would paint
the whole untouched AOI as having no value, which is why
``test_most_of_the_aoi_is_unchanged_for_an_indicator_with_a_baseline`` exists
alongside the single-block version of the same claim.
"""

from __future__ import annotations

import pytest

from reimagina.data import aoi as aoi_mod
from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import (
    ACCESSIBILITY_NETWORK,
    EDIFICABILITY_BLOCK_DENSITY,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
    seed_catalog,
)
from reimagina.scenarios.authoring import create_scenario
from reimagina.scenarios.comparison import compare
from reimagina.storage import backend_for_connection
from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

_FLOORS = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
_DENSITY = EDIFICABILITY_BLOCK_DENSITY.slug
#: ``accessibility_block_school_network_m`` — metres to the nearest school, so
#: the one shipped family whose ``higher_is_better`` is False.
_SCHOOL = ACCESSIBILITY_NETWORK[0].slug

#: Eight ~210 m squares in a row at 19.40°N, spaced so a rectangle can take any
#: prefix of them without clipping the next.
_LAT0, _LAT1 = 19.4000, 19.4020
_BLOCK_SIDE = 0.0020
_BLOCK_PITCH = 0.0040
_BLOCK_LON0 = -99.2000
_BLOCKS = 8


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
        self.project_id = insert_project(conn, "Comparison Project")
        type_id = get_type_id(conn, "MEX", "block")
        source_id = register_source(
            conn,
            name="compare_src",
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
                source_record_ref=f"cmp-blk-{i}",
                geometry_wkt=_block_wkt(i),
            )
            for i in range(_BLOCKS)
        ]
        self.aoi_wide = self._aoi("wide", _AOI_WIDE)
        self.aoi_narrow = self._aoi("narrow", _AOI_NARROW)
        self.scenario_id = create_scenario(
            conn, project_id=self.project_id, name="Upzoning", aoi_id=self.aoi_wide
        )

    def _aoi(self, name: str, wkt: str) -> str:
        aoi_id = aoi_mod.create_aoi(
            self.conn, project_id=self.project_id, name=name, geometry_wkt=wkt
        )
        aoi_mod.compute_aoi_features(self.conn, aoi_id)
        return aoi_id

    def other_scenario(self, name: str = "Rival") -> str:
        return create_scenario(
            self.conn, project_id=self.project_id, name=name, aoi_id=self.aoi_wide
        )


def _write(conn, *, slug, values, scenario_id=None):
    """``indicator_result`` rows through the repository's own write path.

    ``values`` maps feature id to number. The ``job_run`` carries the same
    ``scenario_id`` as the rows because r016's before-write trigger refuses a
    scenario result filed under a baseline run.
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


def _row_for(result, feature_id: str) -> dict:
    matches = [row for row in result.rows if row["feature_id"] == feature_id]
    assert matches, f"{feature_id} is absent from the comparison"
    assert len(matches) == 1, f"{feature_id} appears {len(matches)} times"
    return matches[0]


def _states(result) -> dict[str, int]:
    """The summary, recounted from the rows it claims to summarise."""
    counts = {"changed": 0, "unchanged": 0, "new": 0}
    for row in result.rows:
        counts[row["state"]] += 1
    return counts


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
    already had, and blocks 3..7 have no scenario row at all. So the three
    states the table distinguishes are all present in one fixture, and the
    untouched majority outnumbers the changed minority 6 to 2.
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
    """Density: no baseline anywhere, two scenario-only rows.

    The shape of every edificability indicator — they exist only under a
    scenario, which is what the full outer join is for.
    """
    _write(
        conn,
        slug=_DENSITY,
        values={territory.blocks[0]: 120.0, territory.blocks[1]: 80.0},
        scenario_id=territory.scenario_id,
    )
    return territory


# ── the three states ────────────────────────────────────────────────────────


def test_a_changed_block_reports_its_delta(upzoned, conn):
    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    row = _row_for(result, upzoned.blocks[0])
    assert row["state"] == "changed"
    assert row["baseline"] == 3.0
    assert row["scenario"] == 9.0
    assert row["delta"] == pytest.approx(row["scenario"] - row["baseline"])


def test_a_recomputed_block_that_did_not_move_is_unchanged(upzoned, conn):
    """Block 2 has a scenario row equal to its baseline.

    A row's existence is not a change. Classifying on presence rather than
    value would call this ``changed`` with a delta of zero — a contradiction
    the summary then reports as impact.
    """
    row = _row_for(
        compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS),
        upzoned.blocks[2],
    )

    assert row["state"] == "unchanged"
    assert row["delta"] == 0.0
    assert row["scenario"] == row["baseline"] == 3.0


def test_a_block_the_scenario_never_touched_is_unchanged_with_a_zero_delta(
    upzoned, conn
):
    """A baseline row and no scenario row.

    Its effective value is the baseline, so the honest answer is "unchanged",
    not NULL and not "new".
    """
    row = _row_for(
        compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS),
        upzoned.blocks[5],
    )

    assert row["state"] == "unchanged"
    assert row["delta"] == 0.0
    assert row["scenario"] == row["baseline"]
    assert row["scenario"] is not None


def test_most_of_the_aoi_is_unchanged_for_an_indicator_with_a_baseline(upzoned, conn):
    """The guard against classifying on the raw scenario column.

    Were that done, the six blocks outside the drawn area would come back with
    a NULL scenario value and could not be ``unchanged`` at all.
    """
    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    assert len(result.rows) == _BLOCKS
    assert result.summary["unchanged"] == 6
    assert result.summary["changed"] == 2
    assert result.summary["unchanged"] > result.summary["changed"]
    assert result.summary["new"] == 0
    assert all(row["scenario"] is not None for row in result.rows)


def test_an_indicator_with_no_baseline_reports_new_not_a_delta(densified, conn):
    """The five indicators the full outer join exists for.

    A left join anchored on the baseline returns nothing here at all.
    """
    result = compare(conn, scenario_id=densified.scenario_id, indicator_slug=_DENSITY)

    assert len(result.rows) == 2
    row = _row_for(result, densified.blocks[0])
    assert row["state"] == "new"
    assert row["baseline"] is None
    assert row["delta"] is None
    assert row["scenario"] == 120.0


# ── the summary ─────────────────────────────────────────────────────────────


def test_the_summary_carries_the_catalog_direction(densified, conn):
    result = compare(conn, scenario_id=densified.scenario_id, indicator_slug=_DENSITY)

    assert result.summary["higher_is_better"] is True


def test_a_lower_is_better_indicator_reports_its_own_direction(territory, conn):
    """Read from the catalog, not assumed.

    Distance to the nearest school is the shipped counter-example: the three
    accessibility specs are the reason ``higher_is_better`` exists at all, and
    a hardcoded ``True`` would invert what the comparison means for them.
    """
    _write(conn, slug=_SCHOOL, values=dict.fromkeys(territory.blocks, 900.0))

    result = compare(conn, scenario_id=territory.scenario_id, indicator_slug=_SCHOOL)

    assert result.summary["higher_is_better"] is False


def test_the_summary_counts_the_states_its_rows_report(upzoned, conn):
    """Counted, not inferred — and counted from the same rows.

    A summary that disagrees with the rows it summarises is the bug worth
    pinning: it is what a caller reads when it does not want to walk 19,058
    blocks itself.
    """
    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    counted = _states(result)
    assert result.summary["changed"] == counted["changed"]
    assert result.summary["unchanged"] == counted["unchanged"]
    assert result.summary["new"] == counted["new"]
    assert counted["changed"] + counted["unchanged"] + counted["new"] == len(
        result.rows
    )


def test_all_three_states_coexist_and_the_summary_counts_each(territory, conn):
    """The only shape in which an inferred count can disagree with the rows.

    A partial baseline — blocks 0..5 but not 6 and 7 — plus a scenario that
    raises block 0, re-states block 2 and computes the two blocks the baseline
    never had. Deriving any one count by subtraction instead of counting it
    gets the wrong answer here and only here.
    """
    blocks = territory.blocks
    _write(conn, slug=_FLOORS, values=dict.fromkeys(blocks[:6], 3.0))
    _write(
        conn,
        slug=_FLOORS,
        values={blocks[0]: 9.0, blocks[2]: 3.0, blocks[6]: 4.0, blocks[7]: 4.0},
        scenario_id=territory.scenario_id,
    )

    result = compare(conn, scenario_id=territory.scenario_id, indicator_slug=_FLOORS)

    counted = _states(result)
    assert counted == {"changed": 1, "unchanged": 5, "new": 2}
    assert result.summary["changed"] == counted["changed"]
    assert result.summary["unchanged"] == counted["unchanged"]
    assert result.summary["new"] == counted["new"]
    assert len(result.rows) == sum(counted.values()) == 8


def test_the_new_count_matches_the_rows_for_an_indicator_with_no_baseline(
    densified, conn
):
    result = compare(conn, scenario_id=densified.scenario_id, indicator_slug=_DENSITY)

    counted = _states(result)
    assert result.summary["new"] == counted["new"] == 2
    assert result.summary["changed"] == counted["changed"] == 0
    assert result.summary["unchanged"] == counted["unchanged"] == 0


def test_mean_delta_averages_over_every_comparable_block(upzoned, conn):
    """Including the zeros.

    Eight blocks with a baseline, deltas of +6, +2 and six zeros, so 1.0. The
    alternative — averaging only the blocks that moved — would report +4 and
    make a scenario that upzoned two blocks of 19,058 look like one that
    upzoned the city.
    """
    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    assert result.summary["mean_delta"] == pytest.approx(1.0)


def test_mean_delta_is_none_when_nothing_is_comparable(densified, conn):
    result = compare(conn, scenario_id=densified.scenario_id, indicator_slug=_DENSITY)

    assert result.summary["mean_delta"] is None


# ── what the comparison must not read ───────────────────────────────────────


def test_a_superseded_scenario_row_is_not_read(upzoned, conn):
    """Only ``current`` rows.

    Stale the scenario's row for block 0 and it must fall back to the
    baseline, not keep reporting the value a later run replaced.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.indicator_result SET status = 'stale' "
            "WHERE scenario_id = %s AND feature_id = %s AND indicator_slug = %s",
            (upzoned.scenario_id, upzoned.blocks[0], _FLOORS),
        )
        assert cur.rowcount == 1

    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    row = _row_for(result, upzoned.blocks[0])
    assert row["state"] == "unchanged"
    assert row["scenario"] == 3.0
    assert result.summary["changed"] == 1


def test_another_scenarios_rows_are_not_read(upzoned, conn):
    """Two what-ifs over one AOI stay separate.

    The rival moves block 7, which the scenario under comparison never
    touched. It must still read as unchanged.
    """
    rival = upzoned.other_scenario()
    _write(
        conn,
        slug=_FLOORS,
        values={upzoned.blocks[7]: 42.0},
        scenario_id=rival,
    )

    result = compare(conn, scenario_id=upzoned.scenario_id, indicator_slug=_FLOORS)

    assert _row_for(result, upzoned.blocks[7])["state"] == "unchanged"
    assert result.summary["changed"] == 2


def test_an_aoi_scopes_the_comparison_to_its_members(upzoned, conn):
    """The narrow AOI holds blocks 0..2 — the two that moved and one that did not."""
    result = compare(
        conn,
        scenario_id=upzoned.scenario_id,
        indicator_slug=_FLOORS,
        aoi_id=upzoned.aoi_narrow,
    )

    assert {row["feature_id"] for row in result.rows} == set(upzoned.blocks[:3])
    assert result.summary["changed"] == 2
    assert result.summary["unchanged"] == 1


# ── refusals ────────────────────────────────────────────────────────────────


def test_an_unknown_scenario_is_refused_rather_than_reported_unchanged(upzoned, conn):
    """A typo must not read as "this what-if changed nothing".

    Same reasoning as ``delete_scenario``'s existence check: four zeros for a
    typo is indistinguishable from four zeros for a real answer.
    """
    with pytest.raises(LookupError):
        compare(
            conn,
            scenario_id="00000000-0000-0000-0000-000000000000",
            indicator_slug=_FLOORS,
        )


def test_an_unknown_indicator_is_refused(upzoned, conn):
    """``higher_is_better`` has no honest value for a slug the catalog lacks."""
    with pytest.raises(LookupError):
        compare(
            conn, scenario_id=upzoned.scenario_id, indicator_slug="not_an_indicator"
        )

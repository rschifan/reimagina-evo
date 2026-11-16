"""The indicator picker, asked under a scenario.

``GET /indicators?aoi_id=`` exists because the catalog says what an indicator
*is*, not whether anyone has computed it: offering a registered-but-uncomputed
indicator paints an empty map, which reads as a broken indicator rather than an
absent one. The scenario surface turns that filter into a lie in the other
direction. The five edificability indicators have **no baseline rows at all** —
they are computed only inside a scenario — so a picker that counts baseline rows
only hides exactly the indicators a scenario exists to answer, and the map that
a scenario was authored to show cannot be reached from the UI that authored it.

The probe is widened to ``(ir.scenario_id = %s OR ir.scenario_id IS NULL)``,
and that widening is **not** the ``scenario_overlay`` anti-join every other
scenario read uses. The distinction is whether the predicate's query emits
rows:

- the five anti-join sites (``get_current``,
  ``list_current_for_indicator``, ``list_current_with_geometry``,
  ``value_stats``, ``fetch_aoi_children``) and ``indicator_overlay_tile``
  all return or aggregate ``indicator_result`` rows. There, a bare ``OR
  scenario_id IS NULL`` matches the scenario row *and* the baseline row for a
  feature the scenario recomputed — the feature is counted twice, which is the
  bug ``storage._utils.scenario_overlay`` was written to close
- this is an ``EXISTS`` probe in a ``WHERE`` clause over
  ``indicator_catalog``. It emits no rows; it answers "is there at least one".
  Two matching rows and one matching row are the same answer, so
  double-counting is not merely unlikely here, it is unrepresentable — and the
  anti-join would buy that nothing for a correlated ``NOT EXISTS`` per catalog
  row

``test_an_indicator_is_offered_exactly_once_under_a_scenario`` is the assertion
that keeps that claim honest: it gives one feature both a baseline row and a
scenario row for the same indicator, which is precisely the state a join would
duplicate.

The hard half is the other direction: the baseline picker must not move. Any
indicator gained or lost from ``?aoi_id=`` alone is a regression, so
``test_the_baseline_picker_is_unchanged`` asserts the whole set by equality
rather than membership.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from reimagina.data import aoi as aoi_mod
from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import IndicatorSpec, seed_catalog
from reimagina.scenarios.authoring import create_scenario
from reimagina.storage import backend_for_connection
from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

#: Two ~210 m squares side by side at 19.41°N, and an AOI rectangle around both.
_LAT0, _LAT1 = 19.4100, 19.4120
_BLOCK_SIDE = 0.0020
_BLOCK_PITCH = 0.0040
_BLOCK_LON0 = -99.3000


def _block_wkt(i: int) -> str:
    lon = _BLOCK_LON0 + i * _BLOCK_PITCH
    return (
        f"POLYGON(({lon} {_LAT0},{lon + _BLOCK_SIDE} {_LAT0},"
        f"{lon + _BLOCK_SIDE} {_LAT1},{lon} {_LAT1},{lon} {_LAT0}))"
    )


_AOI_WKT = (
    f"POLYGON((-99.3010 {_LAT0 - 0.001},-99.2930 {_LAT0 - 0.001},"
    f"-99.2930 {_LAT1 + 0.001},-99.3010 {_LAT1 + 0.001},"
    f"-99.3010 {_LAT0 - 0.001}))"
)

#: Slugs are per-test unique so a seeded catalog row can never be mistaken for
#: one a shipped indicator left behind.
_KINDS = ("baseline", "scenario_only", "both", "rival_only", "uncomputed")


def _spec(slug: str) -> IndicatorSpec:
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind="direct",
        model_family="urban_form",
        value_type="numeric",
        unit="ratio",
        aggregation_method="mean",
        applies_to=("block",),
    )


def _write(conn, *, slug: str, values: dict[str, float], scenario_id=None) -> None:
    """``indicator_result`` rows through the repository's own write path.

    The ``job_run`` carries the same ``scenario_id`` as the rows because r016's
    before-write trigger refuses a scenario result filed under a baseline run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id) "
            "VALUES ('test', 'succeeded', 'test', %s) RETURNING id",
            (scenario_id,),
        )
        job_run_id = str(cur.fetchone()[0])
    backend_for_connection(conn).indicator_result_repo().bulk_write_numeric_results(
        list(values.items()),
        indicator_slug=slug,
        job_run_id=job_run_id,
        scenario_id=scenario_id,
    )


class _Territory:
    """Two blocks in one AOI, two scenarios over it, five catalog rows.

    One indicator per state the picker has to tell apart:

    ======================  ===============================================
    ``baseline``            a baseline row, no scenario rows
    ``scenario_only``       a row under ``scenario`` only — the
                            edificability case, invisible to the baseline
                            picker by design
    ``both``                a baseline row *and* a ``scenario`` row on the
                            **same** feature — the double-count state
    ``rival_only``          a row under ``rival`` only
    ``uncomputed``          catalog row, no results anywhere
    ======================  ===============================================
    """

    def __init__(self, conn):
        self.conn = conn
        self.slugs = {kind: f"pick_{kind}_{uuid4().hex[:8]}" for kind in _KINDS}
        seed_catalog(conn, tuple(_spec(slug) for slug in self.slugs.values()))

        self.project_id = insert_project(conn, f"Picker Project {uuid4().hex[:6]}")
        type_id = get_type_id(conn, "MEX", "block")
        source_id = register_source(
            conn,
            name=f"picker_src_{uuid4().hex[:8]}",
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
                source_record_ref=f"pick-blk-{i}-{uuid4().hex[:6]}",
                geometry_wkt=_block_wkt(i),
            )
            for i in range(2)
        ]
        self.aoi_id = aoi_mod.create_aoi(
            conn, project_id=self.project_id, name="picker AOI", geometry_wkt=_AOI_WKT
        )
        aoi_mod.compute_aoi_features(conn, self.aoi_id)
        self.scenario_id = create_scenario(
            conn, project_id=self.project_id, name="Upzoning", aoi_id=self.aoi_id
        )
        self.rival_id = create_scenario(
            conn, project_id=self.project_id, name="Rival", aoi_id=self.aoi_id
        )

        _write(conn, slug=self.slugs["baseline"], values={self.blocks[0]: 1.0})
        _write(
            conn,
            slug=self.slugs["scenario_only"],
            values={self.blocks[0]: 2.0},
            scenario_id=self.scenario_id,
        )
        _write(conn, slug=self.slugs["both"], values={self.blocks[0]: 3.0})
        _write(
            conn,
            slug=self.slugs["both"],
            values={self.blocks[0]: 4.0},
            scenario_id=self.scenario_id,
        )
        _write(
            conn,
            slug=self.slugs["rival_only"],
            values={self.blocks[0]: 5.0},
            scenario_id=self.rival_id,
        )

    def slug(self, kind: str) -> str:
        return self.slugs[kind]

    def mine(self, slugs) -> set[str]:
        """Only this territory's slugs, so a shipped catalog row cannot leak in."""
        return {s for s in slugs if s in set(self.slugs.values())}


@pytest.fixture
def territory(conn):
    return _Territory(conn)


@pytest.fixture
def listing(conn):
    repo = backend_for_connection(conn).indicator_catalog_repo()

    def _listing(**kwargs) -> list[str]:
        return [row["slug"] for row in repo.list_indicators(**kwargs)]

    return _listing


@pytest.fixture
def client(conn):
    """The live app, pinned to the test's own in-flight transaction."""
    app = create_app(initialise_pool=False)

    def _override_conn():
        yield conn

    app.dependency_overrides[get_db_conn] = _override_conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_an_indicator_with_only_scenario_rows_is_offered_under_that_scenario(
    territory, listing
):
    """The edificability case: computed only inside a what-if.

    Absent from the baseline picker on purpose — a value that exists only
    inside a scenario is not a reason to offer the indicator on the baseline
    map — and present the moment that scenario is the one being asked about.
    """
    slug = territory.slug("scenario_only")
    assert slug not in listing(with_values_for_aoi=territory.aoi_id)
    assert slug in listing(
        with_values_for_aoi=territory.aoi_id, scenario_id=territory.scenario_id
    )


def test_the_baseline_picker_is_unchanged(territory, listing):
    """Asked with no scenario, the answer is exactly what it was before.

    By set equality, not membership: an indicator *gained* here (a scenario's
    rows leaking into the baseline offer) and one *lost* here are both the
    regression this pins, and membership catches neither.
    """
    assert territory.mine(listing(with_values_for_aoi=territory.aoi_id)) == {
        territory.slug("baseline"),
        territory.slug("both"),
    }


def test_a_baseline_indicator_is_still_offered_under_a_scenario(territory, listing):
    """A scenario stores only what it changed, so the baseline is still the map.

    Dropping the ``OR ir.scenario_id IS NULL`` half narrows the picker to the
    handful of indicators the scenario recomputed, and every other indicator
    — which reads through to the baseline value under ``scenario_overlay``,
    and renders perfectly — disappears from the offer the moment a scenario is
    picked.
    """
    offered = listing(
        with_values_for_aoi=territory.aoi_id, scenario_id=territory.scenario_id
    )
    assert territory.slug("baseline") in offered
    assert territory.mine(offered) == {
        territory.slug("baseline"),
        territory.slug("both"),
        territory.slug("scenario_only"),
    }


def test_an_indicator_is_offered_exactly_once_under_a_scenario(territory, listing):
    """``both`` has a baseline row and a scenario row on the same feature.

    That is the state a join over ``indicator_result`` would duplicate, and the
    reason the widened predicate is safe *here* and nowhere that emits rows:
    the probe is an ``EXISTS``, so one matching row and two are the same
    answer. Asserted on the whole listing rather than one slug, because a
    picker that offers any indicator twice renders a duplicated dropdown.
    """
    offered = listing(
        with_values_for_aoi=territory.aoi_id, scenario_id=territory.scenario_id
    )
    assert offered.count(territory.slug("both")) == 1
    assert len(offered) == len(set(offered))


def test_another_scenarios_indicator_is_not_offered(territory, listing):
    """``scenario_id`` scopes to *one* scenario, not to scenarios in general.

    Two what-ifs over the same AOI are the normal case — that is what makes
    them comparable — and a picker that offers a rival's recomputed indicator
    paints the baseline while naming the scenario.
    """
    offered = listing(
        with_values_for_aoi=territory.aoi_id, scenario_id=territory.scenario_id
    )
    assert territory.slug("rival_only") not in offered
    assert territory.slug("scenario_only") in offered


def test_an_uncomputed_indicator_is_offered_under_no_scenario(territory, listing):
    """The filter's original reason survives the widening."""
    slug = territory.slug("uncomputed")
    assert slug not in listing(with_values_for_aoi=territory.aoi_id)
    assert slug not in listing(
        with_values_for_aoi=territory.aoi_id, scenario_id=territory.scenario_id
    )
    assert slug in listing(), "the bare catalog listing must not filter"


def test_an_unknown_scenario_offers_exactly_the_baseline_set(territory, listing):
    """A well-formed id naming no scenario is answered, not refused.

    This is a *filter*, not the resource being addressed, and the existing
    ``aoi_id`` half of this same route already answers an AOI with nothing
    computed in it with ``[]`` rather than a 404. More to the point, the
    answer is true: ``scenario_overlay`` resolves every read under an unknown
    scenario to the baseline row, so the baseline indicators are exactly the
    ones that would render. Refusing would also cost an existence query on
    every picker load.
    """
    assert territory.mine(
        listing(with_values_for_aoi=territory.aoi_id, scenario_id=str(uuid4()))
    ) == territory.mine(listing(with_values_for_aoi=territory.aoi_id))


def test_a_scenario_without_an_aoi_does_not_narrow_the_catalog(territory, listing):
    """``scenario_id`` shifts which rows count; ``aoi_id`` is what narrows.

    Alone it filters nothing, exactly as before — the full catalog is a real
    answer to "what indicators exist", and a scenario does not change that.
    """
    assert listing(scenario_id=territory.scenario_id) == listing()


def test_the_route_passes_the_scenario_through(territory, client):
    """End to end, with the ``UUID``-typed query parameter in the way."""
    aoi, scenario = territory.aoi_id, territory.scenario_id
    baseline = {r["slug"] for r in client.get(f"/indicators?aoi_id={aoi}").json()}
    scoped = {
        r["slug"]
        for r in client.get(f"/indicators?aoi_id={aoi}&scenario_id={scenario}").json()
    }
    assert territory.slug("scenario_only") not in baseline
    assert territory.slug("scenario_only") in scoped
    assert territory.slug("baseline") in scoped


def test_the_route_refuses_a_malformed_scenario_id(territory, client):
    """422 at the boundary, and the transaction still usable afterwards.

    Typed ``UUID`` rather than ``str`` so FastAPI answers before a statement
    runs. Sent as a bare string it would reach a ``uuid`` cast, come back as
    ``psycopg2.DataError``, land on ``errors.database_error_handler`` as a 500,
    and abort the request's transaction on the way out — which the follow-up
    request here is what proves did not happen.
    """
    aoi = territory.aoi_id
    assert (
        client.get(f"/indicators?aoi_id={aoi}&scenario_id=not-a-uuid").status_code
        == 422
    )
    assert client.get(f"/indicators?aoi_id={aoi}").status_code == 200

"""The scenario HTTP surface against a real database.

``tests/contract/test_scenarios_api.py`` pins which connection each handler
runs on and which status each refusal becomes; nothing there executes a
statement. This file executes them. The failure it exists to catch is the one
a monkeypatched test cannot see: a handler that answers 200 having written
nothing, because the write went through the API's pooled connection and the
pool rolls back on the success path.

So the round trips here read back through a *separate* connection. A response
body proves nothing about persistence — only another transaction does.

The app is built with ``initialise_pool=False``, which is load-bearing twice
over: these routes must not need the pool, and a handler that took
``Depends(get_db_conn)`` would raise ``PoolNotInitialised`` instead of
silently rolling back.

Setup is committed, because the routes open their own connections and would
not see an open transaction's rows. Every row is therefore removed statement
by statement with ``discard``, the shape Tasks 5–8 settled on: cleanup as one
transaction stops at its first failure and leaves committed rows in a
database the whole session shares.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from apps.api import deps
from apps.api.app import create_app
from reimagina.assets.keys import partition_key
from reimagina.assets.registry import seed_assets
from reimagina.data import aoi as aoi_mod
from reimagina.scenarios.authoring import create_scenario, get_scenario
from tests.integration._helpers import (
    discard,
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

_TYPE_SLUG = "block_scenarios_api"
_SLUG = "edificability_block_density"

# Two ~210 m squares at 19.40°N, each ~46,500 m². At six storeys and 60 m² per
# dwelling that is 4,650 housing units — comfortably inside the catalog's
# 200,000 bound, so the ceiling check passes for a reason this file can state.
_LAT0, _LAT1 = 19.4000, 19.4020
_LON0 = (-99.2000, -99.1960)
_SIDE = 0.0020


def _block_wkt(i: int) -> str:
    lon = _LON0[i]
    return (
        f"POLYGON(({lon} {_LAT0},{lon + _SIDE} {_LAT0},"
        f"{lon + _SIDE} {_LAT1},{lon} {_LAT1},{lon} {_LAT0}))"
    )


def _rect(lon_min: float, lon_max: float) -> str:
    lat_min, lat_max = _LAT0 - 0.001, _LAT1 + 0.001
    return (
        f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},"
        f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min}))"
    )


#: Covers the two small blocks: the area a proposal is drawn over.
_AREA = _rect(-99.2010, -99.1930)

#: One ~2.3 km² block beside them, inside the AOI and outside ``_AREA``. Big
#: enough that the housing-unit bound bites at a density the precheck allows,
#: which is the only way to reach the per-block ceiling check over HTTP.
_BIG = (-99.1900, -99.1700, _LAT0, _LAT0 + 0.0100)
_BIG_WKT = (
    f"POLYGON(({_BIG[0]} {_BIG[2]},{_BIG[1]} {_BIG[2]},"
    f"{_BIG[1]} {_BIG[3]},{_BIG[0]} {_BIG[3]},{_BIG[0]} {_BIG[2]}))"
)

#: The AOI: everything above, with room around it.
_AOI_WKT = (
    f"POLYGON((-99.2010 {_LAT0 - 0.001},-99.1600 {_LAT0 - 0.001},"
    f"-99.1600 {_LAT0 + 0.012},-99.2010 {_LAT0 + 0.012},-99.2010 {_LAT0 - 0.001}))"
)


def _as_geojson(lon_min: float, lon_max: float, lat_min: float, lat_max: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon_min, lat_min],
                [lon_max, lat_min],
                [lon_max, lat_max],
                [lon_min, lat_max],
                [lon_min, lat_min],
            ]
        ],
    }


_AREA_GEOJSON = _as_geojson(-99.2010, -99.1930, _LAT0 - 0.001, _LAT1 + 0.001)
_BIG_BLOCK_GEOJSON = _as_geojson(-99.1910, -99.1690, _LAT0 - 0.001, _LAT0 + 0.011)

PROPOSAL = {
    "area": _AREA_GEOJSON,
    "levels": 6,
    "unit_size": 60,
    "people_per_household": 3.6,
    "type_slug": _TYPE_SLUG,
}


class _Territory:
    """A committed project, AOI, feature type and two blocks."""

    def __init__(self, conn, migrated_db: str) -> None:
        self.conn = conn
        self.dsn = migrated_db
        self.source_name = f"scenarios-api-{uuid4()}"
        self.project_id = insert_project(conn, f"scenarios-api-{uuid4()}")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.feature_type_catalog
                  (context, type_slug, label, geometry_family, role)
                VALUES ('MEX', %s, 'Block', 'polygon', 'analysis_unit')
                ON CONFLICT (context, type_slug) DO NOTHING
                RETURNING id
                """,
                (_TYPE_SLUG,),
            )
            inserted = cur.fetchone()
            self.type_inserted = inserted is not None
            if inserted is None:
                cur.execute(
                    "SELECT id FROM curated_geo.feature_type_catalog "
                    "WHERE context = 'MEX' AND type_slug = %s",
                    (_TYPE_SLUG,),
                )
                inserted = cur.fetchone()
            self.type_id = str(inserted[0])
        source_id = register_source(
            conn,
            name=self.source_name,
            storage_type="vector",
            integration_role="canonical_entity",
        )
        run_id = start_run(conn, source_id)
        self.blocks = [
            insert_feature(
                conn,
                type_id=self.type_id,
                source_id=source_id,
                ingestion_run_id=run_id,
                dataset_version="v1",
                source_record_ref=f"api-blk-{i}",
                geometry_wkt=_block_wkt(i),
                attributes={},
            )
            for i in (0, 1)
        ]
        self.big_block = insert_feature(
            conn,
            type_id=self.type_id,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref="api-blk-big",
            geometry_wkt=_BIG_WKT,
            attributes={},
        )
        self.blocks.append(self.big_block)
        self.aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=self.project_id,
            name="scenarios api aoi",
            geometry_wkt=_AOI_WKT,
        )
        aoi_mod.compute_aoi_features(conn, self.aoi_id)


@pytest.fixture
def territory(migrated_db: str) -> Iterator[_Territory]:
    conn = psycopg2.connect(migrated_db)
    created: _Territory | None = None
    try:
        # ``publish_scenario_version`` writes a version per override asset, so
        # those assets have to exist before a materialisation runs.
        seed_assets(conn)
        created = _Territory(conn, migrated_db)
        conn.commit()
        yield created
    finally:
        conn.rollback()
        if created is not None:
            scenario_ids = _scenario_ids(conn, created.project_id)
            for scenario_id in scenario_ids:
                discard(
                    conn,
                    "DELETE FROM curated_geo.scenario_override WHERE scenario_id = %s",
                    scenario_id,
                )
                discard(
                    conn,
                    "DELETE FROM curated_geo.indicator_result WHERE scenario_id = %s",
                    scenario_id,
                )
                # Neither hangs off the scenario: both are keyed by the
                # partition string, which no foreign key can follow.
                discard(
                    conn,
                    "DELETE FROM curated_geo.asset_version WHERE partition_key = %s",
                    partition_key({"scenario": scenario_id}),
                )
                discard(
                    conn,
                    "DELETE FROM curated_geo.asset_materialization "
                    "WHERE partition_key = %s",
                    partition_key({"scenario": scenario_id}),
                )
            # Cascades scenario, scenario_edit, area_of_interest, aoi_feature.
            discard(
                conn, "DELETE FROM app_core.project WHERE id = %s", created.project_id
            )
            discard(
                conn,
                "DELETE FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
                created.blocks,
            )
            discard(
                conn,
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                created.source_name,
            )
            discard(
                conn,
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                created.source_name,
            )
            if created.type_inserted:
                discard(
                    conn,
                    "DELETE FROM curated_geo.feature_type_catalog WHERE type_slug = %s",
                    _TYPE_SLUG,
                )
        conn.close()


def _scenario_ids(conn, project_id: str) -> list[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM app_core.scenario WHERE project_id = %s",
                (project_id,),
            )
            return [row[0] for row in cur.fetchall()]
    except psycopg2.Error:
        conn.rollback()
        return []


@pytest.fixture
def client(migrated_db: str, monkeypatch) -> Iterator[TestClient]:
    """The app with no pool, pointed at the migrated test database.

    ``deps.api_dsn()`` resolves from the environment on every call, which is
    the documented knob; the scenario routes call it per request.
    """
    deps.shutdown_pool()
    monkeypatch.setenv("REIMAGINA_APP_DSN", migrated_db)
    with TestClient(create_app(initialise_pool=False)) as c:
        yield c


@pytest.fixture
def orchestrator(monkeypatch):
    """A fake ops-plane: the enqueue is covered by ``POST /jobs``' own tests.

    A real submit would need DBOS and ``DATABASE_URL_DIRECT``, and what this
    file is about is the materialisation that precedes it.
    """
    submitted: list[dict] = []

    class _Handle:
        run_id = "run-123"
        status = None
        deduplicated = False

    class _Fake:
        def submit_run(self, conn, **kwargs):
            submitted.append(kwargs)
            return _Handle()

    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator", lambda mode=None: _Fake()
    )
    return submitted


def _reader(dsn: str):
    """A connection of its own, so persistence is read outside the write."""
    return psycopg2.connect(dsn)


# ── create, read, list ───────────────────────────────────────────────────


def test_creating_a_scenario_persists_it(client, territory, migrated_db):
    created = client.post(
        "/scenarios",
        json={
            "project_id": territory.project_id,
            "aoi_id": territory.aoi_id,
            "name": "Centralidad norte",
            "description": "six storeys",
        },
    )

    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    assert created.json()["name"] == "Centralidad norte"
    assert created.json()["aoi_id"] == territory.aoi_id
    assert created.json()["edits"] == []
    assert created.json()["override_count"] == 0

    assert client.get(f"/scenarios/{scenario_id}").status_code == 200

    # The claim the response cannot make: another transaction can see it.
    reader = _reader(migrated_db)
    try:
        assert get_scenario(reader, scenario_id)["name"] == "Centralidad norte"
    finally:
        reader.close()


def test_a_write_through_the_pool_would_have_persisted_nothing(
    migrated_db, territory, monkeypatch
):
    """⚠ The negative control for the whole transaction rule.

    This is what ``Depends(get_db_conn)`` does to a write: the dependency
    rolls back on the *success* path, so the handler returns 200 and the row
    is not there. Driving the dependency by hand is the only way to show it,
    because through HTTP the response is indistinguishable from a success.
    """
    monkeypatch.setenv("REIMAGINA_APP_DSN", migrated_db)
    deps.shutdown_pool()
    deps.initialise_pool()
    try:
        pooled = deps.get_db_conn()
        conn = next(pooled)
        scenario_id = create_scenario(
            conn, project_id=territory.project_id, name="Through the pool"
        )
        with pytest.raises(StopIteration):
            next(pooled)  # the dependency's teardown: rollback, then putconn
    finally:
        deps.shutdown_pool()

    reader = _reader(migrated_db)
    try:
        assert get_scenario(reader, scenario_id) is None
    finally:
        reader.close()


def test_a_duplicate_name_is_409_not_500(client, territory):
    body = {"project_id": territory.project_id, "name": "Twice"}

    assert client.post("/scenarios", json=body).status_code == 201
    assert client.post("/scenarios", json=body).status_code == 409


def test_a_duplicate_name_leaves_the_next_create_working(client, territory):
    """The savepoint in ``create_scenario``, from the HTTP end.

    A failed statement aborts the whole Postgres transaction; without the
    savepoint the refusal would poison the connection the handler is holding.
    """
    body = {"project_id": territory.project_id, "name": "Twice"}
    client.post("/scenarios", json=body)
    client.post("/scenarios", json=body)

    assert client.post("/scenarios", json={**body, "name": "Thrice"}).status_code == 201


def test_listing_narrows_by_project_and_by_aoi(client, territory):
    client.post(
        "/scenarios",
        json={
            "project_id": territory.project_id,
            "aoi_id": territory.aoi_id,
            "name": "Narrowing",
        },
    )

    mine = client.get(f"/scenarios?project_id={territory.project_id}").json()
    assert [s["name"] for s in mine] == ["Narrowing"]
    assert client.get(f"/scenarios?project_id={uuid4()}").json() == []
    assert len(client.get(f"/scenarios?aoi_id={territory.aoi_id}").json()) == 1
    assert client.get(f"/scenarios?aoi_id={uuid4()}").json() == []


def test_an_unknown_scenario_is_404(client, territory):
    assert client.get(f"/scenarios/{uuid4()}").status_code == 404


def test_a_project_that_does_not_exist_is_404_naming_its_field(client, territory):
    """``ForeignKeyViolation``, mapped — and the field read from the constraint.

    Left raw this was a 500 for a client's typo. The real constraint name is
    what makes this an integration test: the mapping reads
    ``diag.constraint_name`` rather than the driver's message, which is
    internal and never echoed.
    """
    response = client.post(
        "/scenarios", json={"project_id": str(uuid4()), "name": "Orphan"}
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["field"] == "project_id"


def test_an_aoi_that_does_not_exist_is_404_naming_its_field(client, territory):
    response = client.post(
        "/scenarios",
        json={
            "project_id": territory.project_id,
            "aoi_id": str(uuid4()),
            "name": "Orphan AOI",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["field"] == "aoi_id"


def test_a_malformed_id_really_would_have_reached_postgres(territory, migrated_db):
    """The negative control for the 404-on-a-malformed-path-id rule.

    The boundary check in the router only matters if the alternative is real:
    handed a non-uuid, the data layer sends it to a ``uuid`` column and
    Postgres raises — which ``errors.database_error_handler`` maps to 500,
    and which also aborts the request's transaction on the way out.
    """
    reader = _reader(migrated_db)
    try:
        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            get_scenario(reader, "not-a-uuid")
    finally:
        reader.rollback()
        reader.close()


# ── edits and recompute ─────────────────────────────────────────────────


def _scenario(client, territory, name: str = "Centralidad norte") -> str:
    created = client.post(
        "/scenarios",
        json={
            "project_id": territory.project_id,
            "aoi_id": territory.aoi_id,
            "name": name,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_one_proposal_writes_four_edits_that_recompute_materialises(
    client, territory, orchestrator, migrated_db
):
    """The whole authoring path, with the override rows read back separately.

    Four edits over two blocks is eight override rows — and ``floors`` and
    ``num_levels`` are both there, which is the asymmetry
    ``proposal.edits_for`` exists to keep: the planned-floors indicator reads
    one and the edificability family reads the other.
    """
    scenario_id = _scenario(client, territory)

    written = client.post(f"/scenarios/{scenario_id}/edits", json=PROPOSAL)
    assert written.status_code == 201, written.text
    assert [e["attribute_key"] for e in written.json()["edits"]] == [
        "floors",
        "num_levels",
        "unit_size",
        "people_per_household",
    ]

    recomputed = client.post(f"/scenarios/{scenario_id}/recompute")

    assert recomputed.status_code == 202, recomputed.text
    body = recomputed.json()
    assert body["matched_per_edit"] and set(body["matched_per_edit"].values()) == {2}
    assert body["overrides_written"] == 8
    assert body["run_id"] == "run-123"
    assert orchestrator[0]["scenario_id"] == scenario_id
    assert orchestrator[0]["aoi_id"] == territory.aoi_id

    reader = _reader(migrated_db)
    try:
        with reader.cursor() as cur:
            cur.execute(
                "SELECT attribute_key, count(*) FROM curated_geo.scenario_override "
                "WHERE scenario_id = %s GROUP BY attribute_key ORDER BY attribute_key",
                (scenario_id,),
            )
            assert cur.fetchall() == [
                ("floors", 2),
                ("num_levels", 2),
                ("people_per_household", 2),
                ("unit_size", 2),
            ]
        shown = client.get(f"/scenarios/{scenario_id}").json()
        assert shown["override_count"] == 8
        assert shown["materialized_at"] is not None
        assert len(shown["edits"]) == 4
    finally:
        reader.close()


def test_a_proposal_the_density_bound_rules_out_never_reaches_the_database(
    client, territory
):
    """Thirteen storeys at 60 m² is 2,166 units/ha against a 2,000 bound.

    Refused at ``POST /edits``, before a block is chosen, because the density
    bound is the one that does not depend on block area.
    """
    scenario_id = _scenario(client, territory)

    written = client.post(
        f"/scenarios/{scenario_id}/edits", json={**PROPOSAL, "levels": 13}
    )

    assert written.status_code == 422, written.text
    assert written.json()["detail"]["allowed_levels"] == 12
    assert client.get(f"/scenarios/{scenario_id}").json()["edits"] == []


def test_a_breach_only_a_block_can_show_refuses_the_whole_materialisation(
    client, territory, orchestrator, migrated_db
):
    """⚠ The bound the form cannot check, reached through HTTP.

    Twelve storeys at 60 m² is exactly the density bound, so the precheck
    passes it — and must, since density is all it can see with no block
    chosen. The housing-unit bound scales with block area, and over the
    ~2.3 km² block it is 467,000 against a 200,000 ceiling. Nothing is
    written: a refusal here is total, so the scenario keeps whatever it was
    materialised to last time.
    """
    scenario_id = _scenario(client, territory)
    drawn = {
        **PROPOSAL,
        "area": _BIG_BLOCK_GEOJSON,
        "levels": 12,
        "unit_size": 60,
    }
    assert client.post(f"/scenarios/{scenario_id}/edits", json=drawn).status_code == 201

    response = client.post(f"/scenarios/{scenario_id}/recompute")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["breach_count"] >= 1
    assert {b["feature_id"] for b in detail["breaches"]} == {territory.big_block}
    assert "edificability_block_housing_units" in {
        b["indicator_slug"] for b in detail["breaches"]
    }
    assert client.get(f"/scenarios/{scenario_id}").json()["override_count"] == 0
    assert client.get(f"/scenarios/{scenario_id}").json()["materialized_at"] is None


def test_an_edit_can_be_removed_and_only_from_its_own_scenario(
    client, territory, migrated_db
):
    scenario_id = _scenario(client, territory)
    other_id = _scenario(client, territory, name="Other")
    edits = client.post(f"/scenarios/{scenario_id}/edits", json=PROPOSAL).json()[
        "edits"
    ]
    edit_id = edits[0]["id"]

    # Through the wrong scenario: refused, and the edit survives.
    assert (
        client.delete(f"/scenarios/{other_id}/edits?edit_id={edit_id}").status_code
        == 404
    )
    assert len(client.get(f"/scenarios/{scenario_id}").json()["edits"]) == 4

    assert (
        client.delete(f"/scenarios/{scenario_id}/edits?edit_id={edit_id}").status_code
        == 204
    )
    remaining = client.get(f"/scenarios/{scenario_id}").json()["edits"]
    assert [e["attribute_key"] for e in remaining] == [
        "num_levels",
        "unit_size",
        "people_per_household",
    ]
    assert (
        client.delete(f"/scenarios/{scenario_id}/edits?edit_id={edit_id}").status_code
        == 404
    )


def test_recompute_without_an_aoi_is_409(client, territory, migrated_db):
    """``aoi_id`` is ``ON DELETE SET NULL``: the AOI can leave on its own."""
    scenario_id = _scenario(client, territory)
    client.post(f"/scenarios/{scenario_id}/edits", json=PROPOSAL)
    writer = _reader(migrated_db)
    try:
        with writer.cursor() as cur:
            cur.execute(
                "UPDATE app_core.scenario SET aoi_id = NULL WHERE id = %s",
                (scenario_id,),
            )
        writer.commit()
    finally:
        writer.close()

    response = client.post(f"/scenarios/{scenario_id}/recompute")

    assert response.status_code == 409, response.text
    assert "area of interest" in response.json()["detail"]


# ── delete ──────────────────────────────────────────────────────────────


def test_deleting_a_scenario_reports_what_it_removed_and_then_404s(
    client, territory, orchestrator, migrated_db
):
    """And a second DELETE is a 404, not a 204.

    The data layer raises ``ScenarioNotFound`` for an id that matches nothing
    rather than "reporting four zeros for a typo" — its own words — and this
    response carries those four counts. Answering a typo with a body of zeros
    would be the confident "nothing happened" that the comparison module
    refuses for an unknown AOI, and a 204 would say the same thing with no
    body at all.
    """
    scenario_id = _scenario(client, territory)
    client.post(f"/scenarios/{scenario_id}/edits", json=PROPOSAL)
    client.post(f"/scenarios/{scenario_id}/recompute")

    deleted = client.delete(f"/scenarios/{scenario_id}")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["overrides"] == 8
    assert deleted.json()["versions_retired"] == 4
    assert client.get(f"/scenarios/{scenario_id}").status_code == 404
    assert client.delete(f"/scenarios/{scenario_id}").status_code == 404

    reader = _reader(migrated_db)
    try:
        with reader.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM curated_geo.scenario_override "
                "WHERE scenario_id = %s",
                (scenario_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        reader.close()


def test_deleting_an_id_that_never_existed_is_404(client, territory):
    assert client.delete(f"/scenarios/{uuid4()}").status_code == 404


# ── comparison ──────────────────────────────────────────────────────────


def test_the_comparison_runs_and_reports_an_honest_nothing(client, territory):
    """No results on either side yet: zero rows, zero counts, no total.

    The failure this catches is a read surface that is right in shape and
    wrong in SQL — a column the view does not carry, a cast Postgres will not
    make. Those pass every monkeypatched test and fail the first request.
    """
    scenario_id = _scenario(client, territory)

    body = client.get(
        f"/scenarios/{scenario_id}/comparison?indicator_slug={_SLUG}"
        f"&aoi_id={territory.aoi_id}"
    )

    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["rows"] == []
    assert payload["row_count"] == 0
    assert payload["summary"]["changed"] == 0
    assert payload["summary"]["mean_delta"] is None
    assert payload["summary"]["higher_is_better"] is True
    assert "total" not in body.text


def test_the_comparison_refuses_an_unknown_indicator_and_an_unknown_aoi(
    client, territory
):
    scenario_id = _scenario(client, territory)
    base = f"/scenarios/{scenario_id}/comparison"

    assert client.get(f"{base}?indicator_slug=not_an_indicator").status_code == 404
    assert (
        client.get(f"{base}?indicator_slug={_SLUG}&aoi_id={uuid4()}").status_code == 404
    )
    assert (
        client.get(
            f"/scenarios/{uuid4()}/comparison?indicator_slug={_SLUG}"
        ).status_code
        == 404
    )

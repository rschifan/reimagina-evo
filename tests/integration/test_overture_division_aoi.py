"""An AOI built from an Overture division polygon, and what makes it usable.

Every other AOI constructor in this repository is INEGI-derived and declares
a ``membership_key`` — ``{"CVE_ENT": …, "CVE_MUN": …}`` — because r040 made
membership identifier-based where identifiers exist: a block belongs to an
alcaldía because INEGI says so, not because its polygon overlaps.

An Overture boundary carries no INEGI code, so it must pass
``membership_key=None``. These tests pin that, and pin the consequence: with
no key, membership falls back to geometry in all three branches of the
membership SQL. The clause's own comment names the case — *"one that carries
none of them (Overture buildings, OSM establishments) is decided spatially as
before"* — and this is the test that the comment is true.

⚠ Why it matters more than it looks: ``get_tile`` scopes every request by AOI
through an ``EXISTS`` against ``curated_geo.aoi_feature``. A feature in no AOI
is correctly typed, correctly zoomed, visible in ``serving_geo`` — and returns
``204 No Content`` on every tile, with no error anywhere in the path. If
``membership_key`` were wrongly set here, 387,273 loaded buildings would be
undrawable and nothing would say so.
"""

from __future__ import annotations


from reimagina.data import aoi as aoi_mod
from reimagina.data import feature as feat
from reimagina.pipeline.overture_divisions import (
    OVERTURE_LICENSE,
    DivisionCandidate,
    DivisionMatch,
)
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    insert_project as _insert_project,
    register_source as _register_source,
    start_run as _start_run,
)


def _square(x0: float, y0: float, side: float = 0.01) -> str:
    x1, y1 = x0 + side, y0 + side
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


#: A stand-in for the real `Municipio de Mexicali` polygon, in the right part
#: of the world. The real one is a 2,109-point MultiPolygon; what these tests
#: exercise is the AOI wiring, not PostGIS's polygon handling.
_MEXICALI_ISH = _square(-115.60, 32.55, side=0.20)


def _division_match() -> DivisionMatch:
    return DivisionMatch(
        candidate=DivisionCandidate(
            overture_id="443051fd-cece-4d31-bab5-39871bee6b6c",
            subtype="county",
            division_class="land",
            country="MX",
            region="MX-BCN",
            name="Municipio de Mexicali",
        ),
        geometry_wkt=_MEXICALI_ISH,
        release="2026-10-21.0",
    )


def _aoi_from_match(conn, match: DivisionMatch, *, project: str) -> str:
    """The same call `apps.cli.overture.aoi_from_overture_division` makes.

    Duplicated rather than imported because the CLI helper opens its own
    connection from a DSN, and this suite's `conn` fixture owns the
    transaction the assertions read from.
    """
    return aoi_mod.create_aoi(
        conn,
        project_id=_insert_project(conn, name=project),
        name="Mexicali (Overture municipal boundary)",
        geometry_wkt=match.geometry_wkt,
        description=(
            f"AOI derived from Overture divisions/division_area "
            f"id={match.overture_id} ({match.name}) in release "
            f"{match.release}. Licence: {OVERTURE_LICENSE}."
        ),
        membership_key=None,
    )


def test_an_overture_aoi_declares_no_membership_key(conn):
    """The one field that differs from `aoi_from_alcaldia`, and must."""
    aoi_id = _aoi_from_match(conn, _division_match(), project="ovt_key_proj")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT membership_key FROM curated_geo.area_of_interest WHERE id = %s",
            (aoi_id,),
        )
        (membership_key,) = cur.fetchone()

    assert membership_key is None, (
        "an Overture boundary carries no INEGI code, so declaring one would "
        "exclude every feature that lacks it — which is every feature this "
        "AOI will ever hold"
    )


def test_membership_is_decided_spatially_when_there_is_no_key(conn):
    """Inside becomes a member, outside does not — with no identifiers anywhere.

    This is the path r040 left open for "Overture buildings, OSM
    establishments". Both features below carry empty attributes, exactly as
    an Overture building does.
    """
    source_id = _register_source(
        conn,
        name="ovt_spatial_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    building_type = feat.get_type_id(conn, "global", "building")

    def _building(ref: str, wkt: str) -> str:
        return _insert_feature(
            conn,
            type_id=building_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="2026-10-21.0",
            source_record_ref=ref,
            geometry_wkt=wkt,
        )

    # Inside the AOI square; and well north of it, where Calexico would be.
    inside = _building("ovt-inside", _square(-115.50, 32.62, side=0.002))
    outside = _building("ovt-outside", _square(-115.50, 32.90, side=0.002))

    aoi_id = _aoi_from_match(conn, _division_match(), project="ovt_spatial_proj")
    backend_for_connection(conn).aoi_repo().compute_overlaps(aoi_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT feature_id::text FROM curated_geo.aoi_feature WHERE aoi_id = %s",
            (aoi_id,),
        )
        members = {row[0] for row in cur.fetchall()}

    assert inside in members, (
        "a building inside the Overture boundary must be a member; without "
        "this, get_tile returns 204 for every tile of the city"
    )
    assert outside not in members, (
        "a building outside the Mexican boundary must not be a member — this "
        "is what keeps US-side buildings out of a Mexicali AOI"
    )


def test_a_feature_carrying_inegi_codes_is_still_placed_spatially(conn):
    """No key on the AOI means the feature's own codes are not consulted.

    Worth pinning because the inverse is the r040 rule, and it would be easy
    to assume identifiers always win. They win only where the *AOI* declares
    them. A block with a Baja California CVE_ENT lands in this AOI because
    its geometry does, not because 02 matches anything.
    """
    source_id = _register_source(
        conn,
        name="ovt_codes_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    fid = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="ovt-coded-block",
        geometry_wkt=_square(-115.50, 32.62, side=0.002),
        attributes={"CVE_ENT": "02", "CVE_MUN": "002"},
    )

    aoi_id = _aoi_from_match(conn, _division_match(), project="ovt_codes_proj")
    backend_for_connection(conn).aoi_repo().compute_overlaps(aoi_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT feature_id::text FROM curated_geo.aoi_feature WHERE aoi_id = %s",
            (aoi_id,),
        )
        members = {row[0] for row in cur.fetchall()}

    assert fid in members


def test_the_aoi_records_where_its_boundary_came_from(conn):
    """Provenance in the description, because the AOI has no source_id column.

    A boundary that cannot be traced to a release and an upstream id is a
    polygon of unknown origin in a delivery whose argument is that its
    provenance is checkable.
    """
    match = _division_match()
    aoi_id = _aoi_from_match(conn, match, project="ovt_prov_proj")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT description FROM curated_geo.area_of_interest WHERE id = %s",
            (aoi_id,),
        )
        (description,) = cur.fetchone()

    assert match.overture_id in description
    assert match.release in description
    assert "Municipio de Mexicali" in description
    assert OVERTURE_LICENSE in description
    assert "divisions/division_area" in description

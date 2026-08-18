"""Border contact must not make a feature an AOI member.

Two polygons sharing only a boundary line intersect to a sliver whose area is
nonzero in floating point but physically meaningless. `aoi_feature`'s
`overlap_ratio > 0` CHECK does not reject that, so on real CDMX data Coyoacan
and Xochimilco became members of Tlalpan at ratios of 6.2e-12 and 8.1e-12 and
their whole polygons rendered on an AOI-scoped map.
"""

from __future__ import annotations

from reimagina.data import feature as feat
from reimagina.storage import backend_for_connection
from reimagina.storage.postgres.repositories import MEMBERSHIP_MIN_OVERLAP_RATIO

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    insert_project as _insert_project,
    register_source as _register_source,
    start_run as _start_run,
)
from reimagina.data import aoi as aoi_mod


def _square(x0: float, y0: float, side: float = 0.01) -> str:
    x1, y1 = x0 + side, y0 + side
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


def test_a_shared_border_is_not_membership(conn):
    source_id = _register_source(
        conn,
        name="eps_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    def _block(ref: str, wkt: str) -> str:
        return _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=wkt,
        )

    # `inside` sits within the AOI; `abutting` shares only the x=-99.13 edge.
    inside = _block("eps-inside", _square(-99.139, 19.430, side=0.002))
    abutting = _block("eps-abutting", _square(-99.130, 19.430))

    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=_insert_project(conn, name="eps_proj"),
        name="eps_aoi",
        geometry_wkt=_square(-99.140, 19.430),
    )
    backend_for_connection(conn).aoi_repo().compute_overlaps(aoi_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT feature_id::text, overlap_ratio FROM curated_geo.aoi_feature "
            "WHERE aoi_id = %s",
            (aoi_id,),
        )
        members = dict(cur.fetchall())

    assert inside in members, "a feature inside the AOI must be a member"
    assert abutting not in members, (
        "a feature sharing only a border must not be a member; got ratio "
        f"{members.get(abutting)!r}"
    )
    # Guards against the threshold being raised into real data: everything that
    # survives must clear the epsilon by a wide margin, not sit just above it.
    assert min(members.values()) > MEMBERSHIP_MIN_OVERLAP_RATIO * 1000

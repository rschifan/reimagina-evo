"""One map credit per product, for sources that declare one.

INEGI's terms ask for "Fuente: INEGI, <producto>, <fecha>" per product. One
credit per (provider, licence) folded the Marco Geoestadístico, the census and
DENUE into a single generic INEGI line, and the census — observations only, no
features — was not credited at all.
"""

from __future__ import annotations

from uuid import uuid4

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data.attribution import attributions

INEGI_LICENCE = "Libre uso — Términos de Uso de la Información del INEGI"


def _source(conn, *, provider="INEGI", license=INEGI_LICENCE, credit=None):
    source_id = cat.register_source(
        conn,
        name=f"credit-test-{uuid4()}",
        provider=provider,
        storage_type="vector",
        integration_role="enrichment",
        storage_uri="file:///tmp/fake",
        version="v1",
        license=license,
        credit=credit,
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="seed",
        code_version="test",
    )
    return source_id, run_id


def _feature(conn, source_id, run_id) -> str:
    type_id = feat.get_type_id(conn, "global", "establishment")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (type_id, source_id, source_record_ref, ingestion_run_id,
               dataset_version, geometry)
            VALUES (%s, %s, %s, %s, 'v1', ST_SetSRID(ST_MakePoint(-115.4, 32.6), 4326))
            RETURNING id
            """,
            (type_id, source_id, str(uuid4()), run_id),
        )
        return str(cur.fetchone()[0])


def _observation(conn, feature_id, source_id, run_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.observation
              (feature_id, attribute_key, value_type, value_numeric,
               source_id, ingestion_run_id)
            VALUES (%s, 'credit_test', 'numeric', 1, %s, %s)
            """,
            (feature_id, source_id, run_id),
        )


def _texts(conn) -> list[str]:
    return [a.text for a in attributions(conn)]


def test_each_inegi_product_gets_its_own_credit_line(conn) -> None:
    marco = "Fuente: INEGI, Marco Geoestadístico 2024 (test)"
    denue = "Fuente: INEGI, DENUE, 05/2025 (test)"
    for credit in (marco, denue):
        _feature(conn, *_source(conn, credit=credit))
    texts = _texts(conn)
    assert marco in texts and denue in texts


def test_identical_credits_render_once(conn) -> None:
    """The Marco Geoestadístico is four sources, one per layer."""
    credit = f"Fuente: INEGI, Marco (test {uuid4()})"
    for _ in range(2):
        _feature(conn, *_source(conn, credit=credit))
    assert _texts(conn).count(credit) == 1


def test_a_product_with_only_observations_is_credited(conn) -> None:
    """The census lands as observations on INEGI blocks, never as features."""
    feature_id = _feature(conn, *_source(conn, provider=f"blocks-{uuid4()}"))
    credit = f"Fuente: INEGI, Censo (test {uuid4()})"
    source_id, run_id = _source(conn, credit=credit)
    _observation(conn, feature_id, source_id, run_id)
    assert credit in _texts(conn)


def test_an_observation_source_without_a_credit_is_not_credited(conn) -> None:
    """Rollups and synthetic seeds write observations under their own source
    rows; there is nobody third-party to credit for those."""
    feature_id = _feature(conn, *_source(conn, provider=f"blocks-{uuid4()}"))
    provider = f"reimagina-rollup-{uuid4()}"
    source_id, run_id = _source(conn, provider=provider, license=None)
    _observation(conn, feature_id, source_id, run_id)
    assert provider not in _texts(conn)


def test_a_source_without_a_credit_keeps_the_provider_fallback(conn) -> None:
    provider = f"Overture-{uuid4()}"
    _feature(conn, *_source(conn, provider=provider, license="ODbL-1.0"))
    assert f"{provider} (ODbL-1.0)" in _texts(conn)

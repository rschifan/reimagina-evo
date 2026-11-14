"""Places loaded by the ingest must be found by the accessibility query.

**The gap this closes.** `tests/unit/test_overture_places.py` covers the
loader's pure logic and `tests/unit/test_amenity_categories.py` covers the
vocabulary, but neither touches a database, so between them they prove nothing
about the only thing that matters: that a row written by
`load_places_subset` is *visible* to the SQL in
`reimagina.indicators.accessibility`.

That join is by convention, not by constraint. The indicator selects
`type_slug = 'establishment'` and reads `attributes->>'category'`; the loader
passes `attributes_cols=["category", ...]` to `load_vector_source` and trusts
it to land there. Nothing in the schema enforces the correspondence. If the
category were written under another key — or the feature type resolved
elsewhere — the ingest would report rows written, the indicator would find no
destinations, and every block would report the 1,500 m cutoff. A green ingest
and a flat choropleth: the exact failure this whole module exists to remove,
reintroduced one layer down and just as silent.

So this loads through the real function and then asks the real question.
"""

from __future__ import annotations

from uuid import uuid4

import geopandas as gpd
import shapely

from reimagina.data import feature as feat
from reimagina.indicators.catalog import ACCESSIBILITY_CATEGORIES
from reimagina.pipeline.overture_places import load_places_subset

from tests.integration._helpers import register_source as _register_source

# One of each, drawn from what the real Mexicali extract actually returned.
_PLACES = [
    ("Parque Vicente Guerrero", "park"),
    ("Plaza del Mariachi", "public_plaza"),
    ("Gimnasio Olimpico", "gym"),
    ("Cinepolis Mexicali", "movie_theater"),
    ("Biblioteca Publica Central", "library"),
]

# The amenity selection from `reimagina.indicators.accessibility._AMENITIES_CTE`,
# reproduced verbatim. Copied rather than imported because the point is to fail
# if the *loader* drifts from it; importing the constant would make both sides
# move together and assert nothing.
_AMENITY_QUERY = """
    SELECT count(*)
    FROM curated_geo.feature f
    WHERE f.type_slug = 'establishment'
      AND f.attributes->>'category' = ANY(%(categories)s)
      AND f.source_id = %(source_id)s::uuid
"""


def _silver_parquet(tmp_path):
    gdf = gpd.GeoDataFrame(
        {
            "id": [f"overture-{i}" for i in range(len(_PLACES))],
            "name": [name for name, _ in _PLACES],
            "category": [category for _, category in _PLACES],
            "confidence": [0.9] * len(_PLACES),
        },
        geometry=[
            shapely.Point(-115.48 + i / 1000, 32.62) for i in range(len(_PLACES))
        ],
        crs="EPSG:4326",
    )
    path = tmp_path / "places_silver.parquet"
    gdf.to_parquet(path)
    return path


def _load(conn, tmp_path):
    # Fresh source and run id per test. `register_source` is idempotent on the
    # name, so a shared one returns the same row and the second test collides on
    # ingestion_run's (source_id, orchestrator_run_id) unique key — a failure
    # about the fixture, not the code.
    source_id = _register_source(
        conn,
        name=f"overture_places_test_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="enrichment",
    )
    feat.seed_feature_types(conn)
    result = load_places_subset(
        conn,
        source_id=source_id,
        silver_path=_silver_parquet(tmp_path),
        orchestrator_run_id=str(uuid4()),
        dataset_version="2026-10-21.0",
    )
    return source_id, result


def test_loaded_places_are_visible_to_the_accessibility_query(conn, tmp_path):
    source_id, _ = _load(conn, tmp_path)

    with conn.cursor() as cur:
        cur.execute(
            _AMENITY_QUERY,
            {
                "categories": list(ACCESSIBILITY_CATEGORIES["park"]),
                "source_id": source_id,
            },
        )
        parks = cur.fetchone()[0]
        cur.execute(
            _AMENITY_QUERY,
            {
                "categories": list(ACCESSIBILITY_CATEGORIES["culture"]),
                "source_id": source_id,
            },
        )
        culture = cur.fetchone()[0]

    # park, public_plaza, gym — the three park-family rows above.
    assert parks == 3, (
        "the accessibility query found no park destinations among rows the "
        "places loader reported writing — the loader and the indicator disagree "
        "about where the category lives"
    )
    # movie_theater, library.
    assert culture == 2


def test_the_category_lands_in_attributes_not_only_in_the_name(conn, tmp_path):
    """`name` and `category` are both written, and they are not the same column.

    The loader sets `name_col="name"` and puts the category in `attributes`.
    An earlier amenity loader used `category` for both, so a reader could
    reasonably assume the indicator can match on either. It cannot.
    """
    source_id, _ = _load(conn, tmp_path)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.name, f.attributes->>'category'
            FROM curated_geo.feature f
            WHERE f.source_id = %s::uuid AND f.name = 'Cinepolis Mexicali'
            """,
            (source_id,),
        )
        name, category = cur.fetchone()

    assert name == "Cinepolis Mexicali"
    assert category == "movie_theater"


def test_places_land_as_the_shared_establishment_type(conn, tmp_path):
    """Same feature type as the IMIP amenities, which is what lets the two
    layers feed one indicator without a catalog change."""
    source_id, result = _load(conn, tmp_path)
    assert result["records_written"] == len(_PLACES)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT f.type_slug
            FROM curated_geo.feature f WHERE f.source_id = %s::uuid
            """,
            (source_id,),
        )
        assert [r[0] for r in cur.fetchall()] == ["establishment"]

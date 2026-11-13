"""`data_source_catalog.storage_uri` records where a CDMX layer came from.

It was registered as a `pending://` placeholder and then overwritten with the
local bronze path once the archive landed — so the catalog recorded where our
copy sat and never where the data was published. The bronze copy needs no
column: it lives at `data/bronze/{source_id}/` by construction.
"""

from __future__ import annotations

from reimagina.pipeline import cdmx
from reimagina.pipeline.cdmx import CDMX_BLOCKS, ensure_layer_source


def test_a_new_layer_records_where_it_is_published(monkeypatch):
    registered: dict[str, object] = {}

    monkeypatch.setattr(cdmx.cat, "get_source", lambda conn, **kw: None)

    def _register(conn, **kwargs):
        registered.update(kwargs)
        return "source-1"

    monkeypatch.setattr(cdmx.cat, "register_source", _register)

    assert ensure_layer_source(object(), CDMX_BLOCKS) == "source-1"
    assert registered["storage_uri"] == CDMX_BLOCKS.download_url
    assert "pending://" not in str(registered["storage_uri"])


def test_a_row_registered_under_the_old_convention_moves_onto_lineage(monkeypatch):
    """Re-ingest heals a row that holds a local bronze path.

    Returning early for an existing row used to be harmless, because the
    landing step overwrote the URI each run anyway. With that overwrite gone,
    an early return would have frozen every pre-existing row on its bronze
    path indefinitely — which is the state a real database is already in.
    """
    old = "file://data/bronze/source-1/manzanas.shp"
    updates: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cdmx.cat,
        "get_source",
        lambda conn, **kw: {"id": "source-1", "storage_uri": old},
    )
    monkeypatch.setattr(
        cdmx.cat,
        "update_source_storage_uri",
        lambda conn, source_id, uri: updates.append((source_id, uri)),
    )

    assert ensure_layer_source(object(), CDMX_BLOCKS) == "source-1"
    assert updates == [("source-1", CDMX_BLOCKS.download_url)]


def test_a_row_already_on_lineage_is_left_alone(monkeypatch):
    """No write when nothing would change — re-ingest stays a read."""
    updates: list[object] = []

    monkeypatch.setattr(
        cdmx.cat,
        "get_source",
        lambda conn, **kw: {"id": "source-1", "storage_uri": CDMX_BLOCKS.download_url},
    )
    monkeypatch.setattr(
        cdmx.cat, "update_source_storage_uri", lambda *a: updates.append(a)
    )

    ensure_layer_source(object(), CDMX_BLOCKS)
    assert updates == []

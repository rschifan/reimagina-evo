"""Clipping away 100% of a load is a bbox/AOI mismatch, and must not pass.

`_clip_buildings_to_aoi` exists because a bbox extract is a *superset* of the
AOI — it deletes the overhang after load so `aoi_feature` never surfaces
bbox-only orphans. If it deletes everything, the bbox and the AOI are
disjoint: the extract is of somewhere else.

⚠ The old behaviour reported that as a successful clip and carried on.
`scripts/mexicali_run.sh` omitted `--bbox`, so `overture extract` used its own
default — MEXICO_CITY_BBOX, 1,600 km from Mexicali. Several million Mexico City
buildings were extracted (a 722 MB parquet), loaded, and clipped to nothing.
No error. The run continued four more steps and failed as:

    urban_form_block_building_density needs the global:building feature
    layer, which is empty

— a message about blocks, pointing nowhere near the cause.
"""

from __future__ import annotations

import pytest

from reimagina.pipeline.overture import ExtractAoiMismatch, _clip_buildings_to_aoi


class _FakeCursor:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class _FakeConn:
    def __init__(self, rowcount: int):
        self._cur = _FakeCursor(rowcount)

    def cursor(self):
        return self._cur


AOI = "0e5f1a3c-0000-4000-8000-000000000001"


def test_clipping_everything_raises():
    conn = _FakeConn(rowcount=5_000_000)
    with pytest.raises(ExtractAoiMismatch) as exc:
        _clip_buildings_to_aoi(
            conn, ingestion_run_id="run", aoi_id=AOI, loaded=5_000_000
        )
    message = str(exc.value)
    assert "does not overlap" in message
    # The message has to name the actual cause, because the symptom appears
    # four steps away and mentions blocks.
    assert "--bbox" in message
    assert "MEXICO_CITY_BBOX" in message
    assert AOI in message


def test_clipping_most_of_it_is_normal_and_does_not_raise():
    """The real Mexicali run clips 43,577 of 387,273 — a bbox crossing the border."""
    conn = _FakeConn(rowcount=43_577)
    result = _clip_buildings_to_aoi(
        conn, ingestion_run_id="run", aoi_id=AOI, loaded=387_273
    )
    assert result["clipped_outside_aoi"] == 43_577
    assert result["clipped_fraction"] == pytest.approx(0.1125, abs=1e-3)


def test_clipping_nothing_is_normal():
    conn = _FakeConn(rowcount=0)
    result = _clip_buildings_to_aoi(conn, ingestion_run_id="run", aoi_id=AOI, loaded=10)
    assert result["clipped_outside_aoi"] == 0
    assert result["clipped_fraction"] == 0.0


def test_the_guard_is_inert_when_the_load_count_is_unknown():
    """`loaded=None` must not turn every clip into a failure.

    The parameter is optional so a caller that does not track its own row
    count still works; the guard simply cannot fire for it.
    """
    conn = _FakeConn(rowcount=99)
    result = _clip_buildings_to_aoi(conn, ingestion_run_id="run", aoi_id=AOI)
    assert result["clipped_outside_aoi"] == 99
    assert result["clipped_fraction"] is None


def test_a_clip_larger_than_the_load_also_raises():
    """>= rather than ==, because the delete is scoped by ingestion_run_id.

    It should never exceed the load, but if it ever does, that is more wrong
    than equal — not less.
    """
    conn = _FakeConn(rowcount=11)
    with pytest.raises(ExtractAoiMismatch):
        _clip_buildings_to_aoi(conn, ingestion_run_id="run", aoi_id=AOI, loaded=10)

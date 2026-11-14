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
    """Returns ``rowcount`` for the DELETE and ``remaining`` for the count."""

    def __init__(self, rowcount: int, remaining: int = 0):
        self.rowcount = rowcount
        self._remaining = remaining
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self._remaining,)


class _FakeConn:
    def __init__(self, rowcount: int, remaining: int = 0):
        self._cur = _FakeCursor(rowcount, remaining)

    def cursor(self):
        return self._cur


AOI = "0e5f1a3c-0000-4000-8000-000000000001"


SRC = "11111111-0000-4000-8000-000000000002"


def test_leaving_the_aoi_empty_raises():
    """A Mexico City extract loaded under a Mexicali AOI: nothing remains."""
    conn = _FakeConn(rowcount=5_000_000, remaining=0)
    with pytest.raises(ExtractAoiMismatch) as exc:
        _clip_buildings_to_aoi(
            conn,
            ingestion_run_id="run",
            aoi_id=AOI,
            loaded=5_000_000,
            source_id=SRC,
        )
    message = str(exc.value)
    assert "none" in message and "left inside" in message
    # The message has to name the actual cause, because the symptom appears
    # four steps away and mentions blocks.
    assert "--bbox" in message
    assert "MEXICO_CITY_BBOX" in message
    assert AOI in message


def test_a_re_run_that_clips_everything_it_loaded_does_not_raise():
    """⚠ The false positive the first version of this guard had, and fired.

    A second `overture load` of the same file upserts: rows that survived the
    first clip are skipped ON CONFLICT, and rows the first clip *deleted* are
    re-inserted. So run two loads exactly the rows that get clipped again, and
    clipping 100 % of them is correct.

    Measured on the delivery machine: 387,273 loaded, 43,566 re-inserted on the
    second pass, 343,707 already present and left inside the AOI. The old
    guard raised on 43,566 of 43,566 and aborted a load that was doing the
    right thing.
    """
    conn = _FakeConn(rowcount=43_566, remaining=343_707)
    result = _clip_buildings_to_aoi(
        conn, ingestion_run_id="run2", aoi_id=AOI, loaded=43_566, source_id=SRC
    )
    assert result["clipped_outside_aoi"] == 43_566
    assert result["clipped_fraction"] == 1.0, "100% of this run, legitimately"
    assert result["remaining_in_aoi"] == 343_707


def test_the_remaining_count_is_scoped_to_the_source_not_the_run():
    """Counting this run alone is what made the old guard wrong.

    A re-run's survivors carry the *earlier* run's ingestion_run_id, so a
    per-run count sees zero and raises.
    """
    conn = _FakeConn(rowcount=1, remaining=5)
    _clip_buildings_to_aoi(
        conn, ingestion_run_id="run", aoi_id=AOI, loaded=1, source_id=SRC
    )
    count_sql = [sql for sql, _ in conn._cur.executed if "count(*)" in sql]
    assert count_sql, "the guard must count what remains"
    assert "f.source_id = %s" in count_sql[0]
    assert "ingestion_run_id" not in count_sql[0]


def test_clipping_most_of_it_is_normal_and_does_not_raise():
    """The real Mexicali run clips 43,577 of 387,273 — a bbox crossing the border."""
    conn = _FakeConn(rowcount=43_577, remaining=343_696)
    result = _clip_buildings_to_aoi(
        conn, ingestion_run_id="run", aoi_id=AOI, loaded=387_273, source_id=SRC
    )
    assert result["clipped_outside_aoi"] == 43_577
    assert result["clipped_fraction"] == pytest.approx(0.1125, abs=1e-3)


def test_clipping_nothing_is_normal():
    conn = _FakeConn(rowcount=0, remaining=10)
    result = _clip_buildings_to_aoi(
        conn, ingestion_run_id="run", aoi_id=AOI, loaded=10, source_id=SRC
    )
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


def test_the_guard_is_inert_without_a_source_id():
    """`source_id=None` cannot be judged, so it must not raise.

    The guard needs the source to count what survives; a caller that does not
    supply one gets the clip without the check rather than a false alarm.
    """
    conn = _FakeConn(rowcount=7, remaining=0)
    result = _clip_buildings_to_aoi(conn, ingestion_run_id="run", aoi_id=AOI, loaded=7)
    assert result["clipped_outside_aoi"] == 7
    assert result["remaining_in_aoi"] is None

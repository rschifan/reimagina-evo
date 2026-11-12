"""The scenario sub-app: what it writes, what it refuses, and what it reads.

Every test here runs without a database. The write paths are exercised by
monkeypatching the `authoring` functions the CLI delegates to, which is the
shape the rest of `tests/unit/test_cli_entrypoint.py` already uses; the read
paths are exercised against a recording fake connection, because *which
relation they read* is the contract (AGENTS.md rule 4) and a delegation test
cannot see it.
"""

from __future__ import annotations

import json
import math

import pytest

import apps.cli.scenario as scenario_cli
from apps.cli.app import main
from apps.cli.scenario import _edits_for
from reimagina.scenarios.authoring import DuplicateScenarioName, MaterializeResult
from reimagina.scenarios.ceilings import DENSITY, max_levels_for

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
AOI_ID = "22222222-2222-2222-2222-222222222222"
SCENARIO_ID = "33333333-3333-3333-3333-333333333333"
TYPE_ID = "44444444-4444-4444-4444-444444444444"

PLANNED_FLOORS = "urban_form_block_planned_floors"

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}


# ── doubles ──────────────────────────────────────────────────────────────


class _FakeCursor:
    """Records every statement and replays scripted answers in order."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self._conn.statements.append((" ".join(sql.split()), params))
        columns, rows = self._conn.answers.pop(0) if self._conn.answers else ((), [])
        self.description = [(name,) for name in columns]
        self._rows = list(rows)

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Enough of a psycopg2 connection for the CLI's own statements."""

    def __init__(self, answers: list[tuple[tuple[str, ...], list[tuple]]]) -> None:
        self.answers = list(answers)
        self.statements: list[tuple[str, object]] = []
        self.events: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.events.append("commit")

    def close(self) -> None:
        self.events.append("close")


@pytest.fixture
def fake_conn(monkeypatch):
    """Install a fake connection and hand it (and its script) to the test."""

    def _install(answers=()):
        conn = _FakeConn(list(answers))
        monkeypatch.setattr(scenario_cli, "connect", lambda dsn: conn)
        return conn

    return _install


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _value(edits: list[dict], key: str) -> float:
    (edit,) = [e for e in edits if e["attribute_key"] == key]
    return edit["value_numeric"]


def _floors_valid_max() -> float:
    """`valid_max` for the indicator that reads the `floors` attribute."""
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    (spec,) = [s for s in SHIPPED_INDICATORS if s.slug == PLANNED_FLOORS]
    assert spec.valid_max is not None
    return float(spec.valid_max)


def _geojson_file(tmp_path, payload=None):
    path = tmp_path / "area.geojson"
    path.write_text(json.dumps(payload if payload is not None else POLYGON))
    return str(path)


# ── the four edits ───────────────────────────────────────────────────────


def test_levels_writes_both_floors_and_num_levels():
    edits = _edits_for(levels=6, unit_size=60, people_per_household=3.6)

    assert {e["attribute_key"] for e in edits} == {
        "floors",
        "num_levels",
        "unit_size",
        "people_per_household",
    }
    assert _value(edits, "floors") == _value(edits, "num_levels") == 6


def test_every_edit_key_is_authorable():
    """A key the authoring layer refuses is a four-way write that half-lands."""
    from reimagina.assets.registry import authorable_attributes

    keys = {e["attribute_key"] for e in _edits_for(6, 60, 3.6)}
    assert keys == set(authorable_attributes())


def test_set_attribute_writes_one_edit_per_attribute(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: _FakeConn([]))
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn, ctx, slug: TYPE_ID)
    monkeypatch.setattr(
        scenario_cli,
        "add_edit",
        lambda conn, **kw: (calls.append(kw), f"edit-{len(calls)}")[1],
    )

    code = main(
        [
            "scenario",
            "set-attribute",
            "--scenario-id",
            SCENARIO_ID,
            "--geojson",
            _geojson_file(tmp_path),
            "--levels",
            "6",
            "--unit-size",
            "60",
            "--people-per-household",
            "3.6",
        ]
    )

    assert code == 0
    assert [c["attribute_key"] for c in calls] == [
        "floors",
        "num_levels",
        "unit_size",
        "people_per_household",
    ]
    assert [c["value_numeric"] for c in calls] == [6.0, 6.0, 60.0, 3.6]
    # One drawn area, four rules over it — not four areas.
    assert {c["geometry_wkt"] for c in calls} == {"POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"}
    assert {c["type_id"] for c in calls} == {TYPE_ID}
    assert len(_out(capsys)["edits"]) == 4


def test_set_attribute_writes_all_four_edits_in_one_transaction(monkeypatch, tmp_path):
    """One proposal, one commit — not a commit per edit, and not none.

    Four edits of a proposal that half-lands is the failure `_edits_for`
    exists to prevent, and a commit between them is how it would happen
    anyway: `add_edit` raising on the third would leave `floors` and
    `num_levels` written and `unit_size` not, which is a block the
    edificability family skips while planned floors moves. The round trip in
    `tests/integration/test_scenario_cli.py` notices a *missing* commit; only
    a count notices an extra one.
    """
    conn = _FakeConn([])
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: conn)
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn_, ctx, slug: TYPE_ID)
    calls: list[dict] = []
    monkeypatch.setattr(
        scenario_cli, "add_edit", lambda conn_, **kw: (calls.append(kw), "e")[1]
    )

    assert _set_attribute(tmp_path, levels=6) == 0

    assert len(calls) == 4
    assert conn.events.count("commit") == 1
    assert conn.events[-1] == "close"


def test_a_geojson_path_that_is_not_there_is_a_message_not_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """The likeliest mistake on this command: a mistyped `--geojson`.

    `FileNotFoundError` is an `OSError`, not a `ValueError`, so before
    `OSError` joined `_SCENARIO_USER_ERRORS` this printed a traceback —
    against this sub-app's own rule that refusals are messages.
    """
    monkeypatch.setattr(
        scenario_cli, "connect", lambda dsn: pytest.fail("nothing may be opened")
    )
    missing = str(tmp_path / "typo.geojson")

    code = main(
        [
            "scenario",
            "set-attribute",
            "--scenario-id",
            SCENARIO_ID,
            "--geojson",
            missing,
            "--levels",
            "6",
            "--unit-size",
            "60",
            "--people-per-household",
            "3.6",
        ]
    )

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert missing in err
    assert "Traceback" not in err


def test_set_attribute_reads_the_area_from_a_feature_collection(
    monkeypatch, tmp_path, capsys
):
    """A drawing tool exports a FeatureCollection, not a bare geometry."""
    calls: list[dict] = []
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: _FakeConn([]))
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn, ctx, slug: TYPE_ID)
    monkeypatch.setattr(
        scenario_cli, "add_edit", lambda conn, **kw: (calls.append(kw), "e")[1]
    )
    path = _geojson_file(
        tmp_path,
        {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": POLYGON}],
        },
    )

    code = main(
        [
            "scenario",
            "set-attribute",
            "--scenario-id",
            SCENARIO_ID,
            "--geojson",
            path,
            "--levels",
            "6",
            "--unit-size",
            "60",
            "--people-per-household",
            "3.6",
        ]
    )

    assert code == 0
    assert calls[0]["geometry_wkt"].startswith("POLYGON ((0 0")


def test_a_geojson_with_no_polygon_is_refused_before_any_write(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: _FakeConn([]))
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn, ctx, slug: TYPE_ID)
    monkeypatch.setattr(
        scenario_cli,
        "add_edit",
        lambda conn, **kw: pytest.fail("add_edit must not run"),
    )
    path = _geojson_file(tmp_path, {"type": "Point", "coordinates": [0.0, 0.0]})

    code = main(
        [
            "scenario",
            "set-attribute",
            "--scenario-id",
            SCENARIO_ID,
            "--geojson",
            path,
            "--levels",
            "6",
            "--unit-size",
            "60",
            "--people-per-household",
            "3.6",
        ]
    )

    assert code == 2
    assert "polygon" in capsys.readouterr().err.lower()


# ── the authoring-time ceiling precheck ──────────────────────────────────


def _set_attribute(tmp_path, *, levels, unit_size=60, people_per_household=3.6):
    return main(
        [
            "scenario",
            "set-attribute",
            "--scenario-id",
            SCENARIO_ID,
            "--geojson",
            _geojson_file(tmp_path),
            "--levels",
            str(levels),
            "--unit-size",
            str(unit_size),
            "--people-per-household",
            str(people_per_household),
        ]
    )


def test_set_attribute_prechecks_the_density_ceiling(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        scenario_cli,
        "connect",
        lambda dsn: pytest.fail("the refusal must precede the connection"),
    )
    monkeypatch.setattr(
        scenario_cli,
        "add_edit",
        lambda conn, **kw: pytest.fail("nothing may be written"),
    )

    code = _set_attribute(tmp_path, levels=13, unit_size=60)

    assert code != 0
    assert DENSITY in capsys.readouterr().err


@pytest.mark.parametrize("unit_size", [30, 60, 120])
def test_the_precheck_boundary_is_the_catalog_bound(monkeypatch, tmp_path, unit_size):
    """The refusal sits exactly at `max_levels_for`, at every unit size.

    Derived in the test from the same catalog the code reads, so lowering
    `valid_max` moves both ends of this assertion together.

    ⚠ Three unit sizes, not one. The bound is `valid_max × unit_size / 10000`,
    so a *single* size cannot tell a derived boundary from a constant: at
    60 m² the honest answer and a hardcoded 12 agree exactly, and the
    hardcode passes. 30 m² allows 6 storeys and 120 m² allows 24 — a constant
    accepts a breach at the first and refuses a legal proposal at the second.
    """
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: _FakeConn([]))
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn, ctx, slug: TYPE_ID)
    monkeypatch.setattr(scenario_cli, "add_edit", lambda conn, **kw: "e")
    allowed = math.floor(max_levels_for(unit_size))

    assert _set_attribute(tmp_path, levels=allowed, unit_size=unit_size) == 0
    assert _set_attribute(tmp_path, levels=allowed + 1, unit_size=unit_size) == 2


@pytest.mark.parametrize("levels", ["0", "-3", "nan"])
def test_a_degenerate_value_is_refused(monkeypatch, tmp_path, capsys, levels):
    """`check_ceilings` skips these, and `value_numeric` is a bare double.

    Nothing downstream would stop them: the kernels return 0 for a
    non-positive input and NaN propagates into the override row.
    """
    monkeypatch.setattr(
        scenario_cli, "connect", lambda dsn: pytest.fail("nothing may be written")
    )

    code = _set_attribute(tmp_path, levels=levels)

    assert code == 2
    assert "--levels" in capsys.readouterr().err


@pytest.mark.xfail(
    strict=True,
    reason="ceilings has no floors bound; delete this when it grows one",
)
def test_the_precheck_covers_floors(monkeypatch, tmp_path):
    """⚠ A known, deferred gap, written as the behaviour we want.

    `check_ceilings` bounds density, housing units and population; it does
    *not* bound `floors`, although `urban_form_block_planned_floors` declares
    a `valid_max`. Past that many storeys, at a unit size large enough that
    the density ratio stays legal, a proposal passes every bound there is and
    r016's trigger raises mid-compute instead.

    `xfail(strict=True)` rather than an `== 0` assertion of the status quo.
    The status quo form fails as a bare `assert 2 == 0` when the gap closes,
    with the "delete me" instruction buried in this docstring, and it also
    fails for precheck bugs that have nothing to do with `floors` — a canary
    that dies of several unrelated causes tells you nothing. This way,
    closing the gap is an XPASS that prints the reason above.

    Both inputs are derived from the catalog. Hardcoding `levels=250,
    unit_size=2000` assumed the density bound stays above 1250: lower it and
    the density precheck starts refusing this proposal, so the test would
    pass for the wrong reason.
    """
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: _FakeConn([]))
    monkeypatch.setattr(scenario_cli, "get_type_id", lambda conn, ctx, slug: TYPE_ID)
    monkeypatch.setattr(scenario_cli, "add_edit", lambda conn, **kw: "e")

    levels = math.floor(_floors_valid_max()) + 1
    # `max_levels_for` is linear in unit size, so this is the unit size at
    # which the density bound allows exactly `levels` storeys; the margin
    # puts the proposal clearly inside it, so only `floors` is in question.
    unit_size = 1.1 * levels / max_levels_for(1.0)
    assert max_levels_for(unit_size) > levels

    assert _set_attribute(tmp_path, levels=levels, unit_size=unit_size) == 2


# ── list / show read the explore contract ────────────────────────────────


def test_list_reads_explore_scenario_v(fake_conn, capsys):
    conn = fake_conn(
        [(("id", "name", "edit_count"), [(SCENARIO_ID, "Centralidad norte", 4)])]
    )

    code = main(["scenario", "list"])

    assert code == 0
    assert len(conn.statements) == 1
    assert "explore.scenario_v" in conn.statements[0][0]
    payload = _out(capsys)
    assert payload["scenarios"] == [
        {"id": SCENARIO_ID, "name": "Centralidad norte", "edit_count": 4}
    ]


def test_list_narrows_by_project_and_aoi(fake_conn):
    conn = fake_conn([(("id",), [])])

    main(["scenario", "list", "--project-id", PROJECT_ID, "--aoi-id", AOI_ID])

    _sql, params = conn.statements[0]
    assert params == {"project_id": PROJECT_ID, "aoi_id": AOI_ID}


def test_show_reads_explore_scenario_v(fake_conn, capsys):
    conn = fake_conn(
        [
            (
                ("id", "edit_count", "override_count", "materialized_at"),
                [(SCENARIO_ID, 4, 12, None)],
            )
        ]
    )

    code = main(["scenario", "show", "--scenario-id", SCENARIO_ID])

    assert code == 0
    assert "explore.scenario_v" in conn.statements[0][0]
    payload = _out(capsys)
    assert payload["scenario"]["override_count"] == 12
    assert payload["scenario"]["materialized_at"] is None


def test_show_refuses_an_unknown_scenario(fake_conn, capsys):
    fake_conn([(("id",), [])])

    code = main(["scenario", "show", "--scenario-id", SCENARIO_ID])

    assert code == 2
    assert SCENARIO_ID in capsys.readouterr().err


# ── create ───────────────────────────────────────────────────────────────


def test_create_derives_the_project_from_the_aoi(monkeypatch, fake_conn, capsys):
    conn = fake_conn([(("project_id",), [(PROJECT_ID,)])])
    seen: dict = {}
    monkeypatch.setattr(
        scenario_cli,
        "create_scenario",
        lambda c, **kw: (seen.update(kw), SCENARIO_ID)[1],
    )

    code = main(
        ["scenario", "create", "--name", "Centralidad norte", "--aoi-id", AOI_ID]
    )

    assert code == 0
    assert seen["project_id"] == PROJECT_ID
    assert seen["aoi_id"] == AOI_ID
    assert "area_of_interest" in conn.statements[0][0]
    assert conn.events.count("commit") == 1
    assert _out(capsys)["scenario_id"] == SCENARIO_ID


def test_create_without_a_project_or_an_aoi_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(
        scenario_cli,
        "connect",
        lambda dsn: pytest.fail("nothing to connect for"),
    )

    code = main(["scenario", "create", "--name", "Nowhere"])

    assert code == 2
    assert "--project-id" in capsys.readouterr().err


def test_create_reports_a_duplicate_name_rather_than_a_traceback(
    monkeypatch, fake_conn, capsys
):
    fake_conn([])

    def _raise(conn, **kw):
        raise DuplicateScenarioName("scenario 'N' already exists in this project")

    monkeypatch.setattr(scenario_cli, "create_scenario", _raise)

    code = main(["scenario", "create", "--name", "N", "--project-id", PROJECT_ID])

    assert code == 2
    assert "already exists" in capsys.readouterr().err


# ── recompute: materialise, commit, then evaluate ────────────────────────


@pytest.fixture
def recompute_spy(monkeypatch):
    """Record the order of materialisation, commit and evaluation."""
    order: list[str] = []
    conn = _FakeConn([(("aoi_id",), [(AOI_ID,)])])
    conn.commit = lambda: order.append("commit")  # type: ignore[method-assign]
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: conn)

    def _materialize(c, *, scenario_id):
        order.append("materialize")
        return MaterializeResult(
            matched_per_edit={"edit-1": 4},
            overrides_written=16,
            overrides_removed=0,
            results_superseded=0,
        )

    def _evaluate(**kwargs):
        order.append("evaluate")
        return {"status": "succeeded", **kwargs}

    monkeypatch.setattr(scenario_cli, "materialize_scenario", _materialize)
    monkeypatch.setattr(scenario_cli, "evaluate_scenario_cli", _evaluate)
    return order, conn


def test_recompute_materialises_before_it_evaluates(recompute_spy, capsys):
    order, _conn = recompute_spy

    code = main(["scenario", "recompute", "--scenario-id", SCENARIO_ID, "--sync"])

    assert code == 0
    assert order == ["materialize", "commit", "evaluate"]
    payload = _out(capsys)
    assert payload["materialized"]["overrides_written"] == 16
    assert payload["materialized"]["matched_per_edit"] == {"edit-1": 4}
    assert payload["evaluation"]["status"] == "succeeded"


def test_recompute_evaluates_the_scenario_in_its_own_aoi(recompute_spy, capsys):
    _order, conn = recompute_spy

    main(["scenario", "recompute", "--scenario-id", SCENARIO_ID, "--sync"])

    # The AOI comes from the explore view, not from a flag the caller forgot.
    assert "explore.scenario_v" in conn.statements[0][0]
    evaluation = _out(capsys)["evaluation"]
    assert evaluation["scenario_id"] == SCENARIO_ID
    assert evaluation["aoi_id"] == AOI_ID
    assert evaluation["mode"] == "sync"
    # Empty: the dirty set comes from the versions materialisation just
    # published, which also covers the attributes it retired.
    assert evaluation["changed_attributes"] == []
    assert evaluation["edited_features"] == []


def test_recompute_does_not_print_a_second_unmasked_dsn(recompute_spy, capsys):
    """⚠ Found by running the command, not by reading it.

    `_print` masked the password of `data["dsn"]` and did not walk the
    structure, so the DSN `evaluate_scenario_cli` echoes inside its own
    payload was printed in full — `postgresql://user:password@host/db` on
    stdout and in whatever captures it. `_print` now walks the payload
    (`test_print_masks_a_dsn_nested_inside_a_list`), and the nested key is
    still dropped: the masked one at the top level says the same thing.
    """
    main(
        [
            "scenario",
            "recompute",
            "--scenario-id",
            SCENARIO_ID,
            "--sync",
            "--dsn",
            "postgresql://reimagina:hunter2@127.0.0.1:5432/reimagina_app",
        ]
    )

    captured = capsys.readouterr().out
    assert "hunter2" not in captured
    assert "dsn" not in json.loads(captured)["evaluation"]


def test_recompute_without_materialising_is_not_an_option(monkeypatch, capsys):
    """If materialisation raises, nothing is evaluated and nothing commits."""
    from reimagina.scenarios.authoring import ScenarioHasNoAOI

    conn = _FakeConn([(("aoi_id",), [(None,)])])
    monkeypatch.setattr(scenario_cli, "connect", lambda dsn: conn)

    def _raise(c, *, scenario_id):
        raise ScenarioHasNoAOI(f"scenario {scenario_id} has no area of interest")

    monkeypatch.setattr(scenario_cli, "materialize_scenario", _raise)
    monkeypatch.setattr(
        scenario_cli,
        "evaluate_scenario_cli",
        lambda **kw: pytest.fail("evaluation must not run"),
    )

    code = main(["scenario", "recompute", "--scenario-id", SCENARIO_ID, "--sync"])

    assert code == 2
    assert "no area of interest" in capsys.readouterr().err
    assert "commit" not in conn.events


# ── compare ──────────────────────────────────────────────────────────────


def _comparison(rows):
    from reimagina.scenarios.comparison import ComparisonResult

    return ComparisonResult(
        rows=rows,
        summary={
            "changed": len(rows),
            "unchanged": 0,
            "new": 0,
            "mean_delta": 1.0,
            "higher_is_better": True,
        },
    )


def test_compare_prints_the_summary_and_the_rows(monkeypatch, fake_conn, capsys):
    fake_conn([])
    seen: dict = {}
    rows = [{"feature_id": f"f{i}", "delta": 1.0, "state": "changed"} for i in range(3)]
    monkeypatch.setattr(
        scenario_cli,
        "compare",
        lambda conn, **kw: (seen.update(kw), _comparison(rows))[1],
    )

    code = main(
        [
            "scenario",
            "compare",
            "--scenario-id",
            SCENARIO_ID,
            "--slug",
            "edificability_block_density",
            "--aoi-id",
            AOI_ID,
        ]
    )

    assert code == 0
    assert seen == {
        "scenario_id": SCENARIO_ID,
        "indicator_slug": "edificability_block_density",
        "aoi_id": AOI_ID,
    }
    payload = _out(capsys)
    assert payload["summary"]["changed"] == 3
    assert payload["row_count"] == 3
    assert payload["rows"] == rows


def test_compare_truncates_the_rows_but_never_the_count(monkeypatch, fake_conn, capsys):
    """19,058 blocks is the real case; the count must survive the truncation."""
    fake_conn([])
    rows = [{"feature_id": f"f{i}"} for i in range(50)]
    monkeypatch.setattr(scenario_cli, "compare", lambda conn, **kw: _comparison(rows))

    main(
        [
            "scenario",
            "compare",
            "--scenario-id",
            SCENARIO_ID,
            "--slug",
            "edificability_block_density",
            "--limit",
            "5",
        ]
    )

    payload = _out(capsys)
    assert payload["row_count"] == 50
    assert len(payload["rows"]) == 5
    assert payload["rows_truncated"] is True


def test_compare_refuses_an_unknown_indicator(monkeypatch, fake_conn, capsys):
    fake_conn([])

    def _raise(conn, **kw):
        raise LookupError("no indicator in the catalog with slug 'nope'")

    monkeypatch.setattr(scenario_cli, "compare", _raise)

    code = main(["scenario", "compare", "--scenario-id", SCENARIO_ID, "--slug", "nope"])

    assert code == 2
    assert "nope" in capsys.readouterr().err

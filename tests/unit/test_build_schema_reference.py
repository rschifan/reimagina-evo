"""The Work 3 reference generator's pure parts.

The document is byte-stable only if the date is an input and nothing about the
host leaks in; and it is legible only if JSON Schema fragments come out as
short types rather than raw JSON.
"""

from __future__ import annotations

from datetime import date

import scripts.build_schema_reference as ref


def test_the_issue_date_is_an_input():
    assert ref.parse_date("20 November 2026") == date(2026, 11, 20)
    assert ref.parse_date("2026-11-20") == date(2026, 11, 20)


def test_the_deployed_stack_is_read_from_the_compose_file():
    stack = ref.deployed_stack(ref.COMPOSE.read_text())
    assert stack.startswith("PostgreSQL 16, PostGIS 3.4 and pgRouting 3.6")


def test_schema_fragments_render_as_short_types():
    nullable_uuid = {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}
    assert ref.schema_type(nullable_uuid) == "string (uuid) or null"
    assert (
        ref.schema_type({"type": "array", "items": {"$ref": "#/components/schemas/X"}})
        == "list[X]"
    )
    assert (
        ref.schema_type({"type": "object", "additionalProperties": {"type": "integer"}})
        == "dict[string, integer]"
    )
    assert ref.schema_type({"enum": ["a", "b"]}) == "one of `a`, `b`"
    assert ref.schema_type({}) == "any"


def test_a_stale_database_is_rejected_before_generating_a_reference(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    monkeypatch.setattr(ref.psycopg2, "connect", MagicMock())
    monkeypatch.setattr(
        ref,
        "_rows",
        lambda cur, sql, *args: [("r050",)] if "alembic_version" in sql else [],
    )

    with pytest.raises(SystemExit, match="r050.*r052"):
        ref.database_section("unused-test-dsn")


def test_issue_metadata_preserves_the_original_draft_without_claiming_acceptance(
    monkeypatch, tmp_path
):
    output = tmp_path / "reference.md"
    monkeypatch.setattr(ref, "ROOT", tmp_path)
    monkeypatch.setattr(ref, "OUTPUT", output)
    monkeypatch.setattr(ref, "database_section", lambda dsn: [])
    monkeypatch.setattr(ref, "api_section", lambda: [])
    assert ref.main(["--dsn", "unused-test-dsn", "--date", "20 November 2026"]) == 0
    document = output.read_text()
    assert "2026-11-13" in document
    assert "**Status** | Updated for client review" in document
    assert "Client acceptance is recorded separately" in document
    assert "Issued with the v1.0.0 delivery" not in document

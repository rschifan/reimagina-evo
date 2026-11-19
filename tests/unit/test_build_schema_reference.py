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

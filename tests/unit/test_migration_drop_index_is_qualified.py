"""Every migration's DROP INDEX names the index's schema.

An unqualified name resolves through ``search_path`` (``"$user", public``),
where none of these indexes live, and ``IF EXISTS`` turns the miss into a
silent no-op: the downgrade reports success and leaves the index behind.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "infra/db/migrations/versions"
_DROP_INDEX = re.compile(
    r"DROP INDEX\s+(?:CONCURRENTLY\s+)?(?:IF EXISTS\s+)?([\w.]+)", re.I
)


def test_drop_index_is_schema_qualified():
    unqualified = [
        f"{path.name}: {name}"
        for path in sorted(VERSIONS.glob("*.py"))
        for name in _DROP_INDEX.findall(path.read_text())
        if "." not in name
    ]
    assert unqualified == []

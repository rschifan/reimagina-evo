"""The Article 4.1 inventory must describe the lockfiles at this commit.

Nothing else fails when a dependency bump lands without a regenerated
`docs/THIRD_PARTY_LICENSES.md`, and that is how the document came to list
versions its own `package.json` ranges no longer allowed.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import third_party_licences as tpl  # noqa: E402


def test_the_licence_inventory_matches_the_lockfiles() -> None:
    assert tpl.main(["--check"]) == 0, (
        "docs/THIRD_PARTY_LICENSES.md is stale: run "
        "`uv run python scripts/third_party_licences.py` and commit the result"
    )

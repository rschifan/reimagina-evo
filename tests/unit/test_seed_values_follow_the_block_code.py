"""Synthetic storeys follow the block's INEGI code, not its database id.

``feature.id`` is ``gen_random_uuid()``, so a value hashed from it differed on
every install: two runs of the same bundle seeded different storeys and
reported different edificability numbers. The CVEGEO (``source_record_ref``)
is the same everywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from seed_demo_attributes import constant_payloads, fit_payloads  # noqa: E402

REFS = [f"02002000100{n:02d}0{n:02d}" for n in range(40)]


def _two_installs():
    """Same blocks, fresh random ids, opposite insertion order."""
    a = {str(uuid4()): ref for ref in REFS}
    b = {str(uuid4()): ref for ref in reversed(REFS)}
    return a, b


def test_constant_seed_is_identical_across_installs():
    a, b = _two_installs()

    def by_ref(ids):
        return {
            ids[fid]: json.loads(payload)
            for payload, fid in constant_payloads(list(ids.items()))
        }

    assert by_ref(a) == by_ref(b)
    assert len({p["num_levels"] for p in by_ref(a).values()}) > 1


def test_fitted_seed_is_identical_across_installs():
    a, b = _two_installs()

    def by_ref(ids):
        # Real inputs vary by block; the fit must not depend on row order.
        blocks = [
            (fid, 1000.0 + n, 300.0 + 7 * n, 10.0 + n % 3, 30.0 + n, None)
            for n, (fid, ref) in enumerate(sorted(ids.items(), key=lambda i: i[1]))
        ]
        if list(ids.values())[0] != REFS[0]:
            blocks.reverse()
        return {
            ids[p["feature_id"]]: {k: v for k, v in p.items() if k != "feature_id"}
            for p in fit_payloads(blocks)
        }

    assert by_ref(a) == by_ref(b)

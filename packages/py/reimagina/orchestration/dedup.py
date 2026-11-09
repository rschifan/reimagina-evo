"""Run-level deduplication — one identity for one piece of outstanding work.

``submit_run`` minted a fresh uuid per call, so a double-clicked button, a
retried POST, or an impatient operator started a second run recomputing
exactly what the first was already computing. Both wrote to the same
``indicator_result`` rows, one after the other, and the second was pure waste.

DBOS 3 deduplicates natively: an enqueue carrying a ``deduplication_id``
collides with any workflow already holding that id on the same queue, and
``duplication_policy="return-existing"`` returns a handle to it instead of
starting a second. The id is cleared on every terminal transition, so the key
is held **only while the work is outstanding**. This module computes the id;
the policy choice is the adapter's.

⚠ **An identical request is not the same thing as identical work, and
deduping on the request alone would be a correctness bug.** A scenario edit
does not change the request — the caller still names the same attribute, the
same features, the same scenario. It changes the *values those features
resolve to*. Dedup on the request shape, and an operator who edits an
override mid-run and resubmits is handed back the run computing the values
they just replaced, with nothing to tell them so.

So the id folds in the content fingerprint of everything the planned units
read: ``serving_geo.asset_expected_version_v`` gives each indicator the hash
of its whole upstream closure, which includes
``scenario.override.<attribute>`` for that scenario. Editing an override
republishes that asset, the closure hash moves, the id changes, and the
resubmission is a new run — which is the answer the operator wanted. Measured
rather than assumed: editing one override value moves the indicator's expected
version, and restoring it moves it back, because the version is content
addressed rather than a counter.

**It fails open.** A planned unit with no expected version — an asset graph
not yet backfilled, an indicator registered without one — means input changes
cannot be detected, so no id is produced and nothing is deduplicated. A
redundant run costs CPU; a wrongly suppressed one costs an answer the operator
asked for and did not get.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from reimagina.assets.keys import indicator_asset_key, partition_key

#: Bumped when the composition below changes. Without it, a scheme change
#: would let an old in-flight run capture a new submission whose id happened
#: to collide — the ids are hashes, and two schemes share a namespace.
SCHEME = "v1"


def _expected_versions(conn: Any, keys: Sequence[tuple[str, str]]) -> dict | None:
    """Closure fingerprints for ``(asset_key, partition_key)`` pairs.

    Returns None if any pair is missing, which is the fail-open signal: a unit
    whose inputs cannot be fingerprinted must not be deduplicated against.
    """
    if not keys:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_key, partition_key, expected_version
            FROM serving_geo.asset_expected_version_v
            WHERE (asset_key, partition_key) IN %s
            """,
            (tuple(keys),),
        )
        found = {(key, part): version for key, part, version in cur.fetchall()}
    if len(found) != len(set(keys)):
        return None
    return found


def deduplication_id(
    conn: Any,
    *,
    unit_slugs: Iterable[str],
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    scenario_id: str | None,
    aoi_id: str | None,
    code_version: str,
    indicators: Sequence[str] | None = None,
) -> str | None:
    """A stable id for this piece of work, or None when it cannot be one.

    ``edited_features`` is hashed rather than embedded: a city-scale edit
    carries tens of thousands of uuids, and the id has to stay a short string
    the queue can index.
    """
    slugs = sorted(set(unit_slugs))
    if not slugs:
        return None

    partition = partition_key({"scenario": scenario_id})
    versions = _expected_versions(
        conn, [(indicator_asset_key(slug), partition) for slug in slugs]
    )
    if versions is None:
        return None

    features = sorted(set(str(f) for f in edited_features))
    payload = {
        "scheme": SCHEME,
        "changed_attributes": sorted(set(changed_attributes)),
        # Count alongside the digest so two different sets cannot look alike in
        # a log line; the digest alone is what makes them differ.
        "features": [
            len(features),
            hashlib.sha256("\n".join(features).encode()).hexdigest(),
        ],
        "scenario_id": scenario_id,
        "aoi_id": aoi_id,
        "code_version": code_version,
        "indicators": sorted(set(indicators)) if indicators else None,
        "units": [
            [slug, versions[(indicator_asset_key(slug), partition)]] for slug in slugs
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{SCHEME}:{hashlib.sha256(canonical.encode()).hexdigest()}"

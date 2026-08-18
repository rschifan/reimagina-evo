"""Partition keys — one canonicalisation, shared by both implementations.

An asset is materialised per *partition*: a scenario, an AOI, a feature-type
pair, an attribute prefix, a zoom tier. The partition is a set of named
dimensions, and the key is their canonical rendering.

This module exists so there is exactly one answer to "what is the key for this
partition". ``serving_geo.asset_expected_version_v`` and
``reimagina.assets.version`` both hash the key as an opaque string, so if the
two ever disagreed about how a partition renders they would disagree about
every version derived from it — silently, because both would still be
internally consistent.

The baseline partition is the empty string, matching the column default in
``curated_geo.asset_version`` and ``asset_materialization``.
"""

from __future__ import annotations

from collections.abc import Mapping

BASELINE = ""


def partition_key(dimensions: Mapping[str, str | None] | None = None) -> str:
    """Render a partition as its canonical key.

    Dimensions are sorted by name and rendered ``name=value``, joined by ``/``.
    A dimension whose value is None is dropped rather than rendered empty, so
    ``{"scenario": None}`` is the baseline and not a distinct partition that
    merely looks like it.

    >>> partition_key()
    ''
    >>> partition_key({"scenario": None})
    ''
    >>> partition_key({"scenario": "s1"})
    'scenario=s1'
    >>> partition_key({"target": "ageb", "source": "block"})
    'source=block/target=ageb'
    """
    if not dimensions:
        return BASELINE
    present = {k: v for k, v in dimensions.items() if v is not None}
    if not present:
        return BASELINE
    for name, value in present.items():
        # A separator inside a value would make two different partitions render
        # to the same key, which silently merges their versions.
        if "/" in name or "=" in name or "/" in str(value) or "=" in str(value):
            raise ValueError(
                f"partition dimension {name!r}={value!r} may not contain '/' or '='"
            )
    return "/".join(f"{name}={present[name]}" for name in sorted(present))

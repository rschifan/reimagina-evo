"""Hamilton driver factory for the indicator dataflow.

Builds one process-wide driver over ``reimagina.indicators.dataflow`` with
optional data-version caching (component 11) and OpenLineage lineage
(component 12a), both off by default so unit tests and CI stay deterministic
and need no lineage backend:

- ``REIMAGINA_HAMILTON_CACHE=1`` enables ``.with_cache()`` (store under the
  data dir) — a second run with identical inputs + code skips the compute.
- ``OPENLINEAGE_URL=<url>`` attaches the OpenLineage adapter; unset = no-op.
"""

from __future__ import annotations

import os
from functools import lru_cache

from hamilton import driver

from reimagina.indicators import dataflow


def _cache_enabled() -> bool:
    return os.environ.get("REIMAGINA_HAMILTON_CACHE", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _lineage_adapter():
    """Return an OpenLineage adapter when ``OPENLINEAGE_URL`` is set, else None."""
    url = os.environ.get("OPENLINEAGE_URL")
    if not url:
        return None
    from hamilton.plugins.h_openlineage import OpenLineageAdapter
    from openlineage.client import OpenLineageClient

    return OpenLineageAdapter(
        client=OpenLineageClient(url=url),
        namespace=os.environ.get("OPENLINEAGE_NAMESPACE", "reimagina"),
        job_name="indicator_dataflow",
    )


@lru_cache(maxsize=1)
def build_indicator_driver() -> driver.Driver:
    """The shared indicator dataflow driver (cached per process)."""
    builder = driver.Builder().with_modules(dataflow)
    if _cache_enabled():
        from reimagina.paths import data_dir

        builder = builder.with_cache(path=str(data_dir() / "hamilton_cache"))
    adapter = _lineage_adapter()
    if adapter is not None:
        builder = builder.with_adapters(adapter)
    return builder.build()

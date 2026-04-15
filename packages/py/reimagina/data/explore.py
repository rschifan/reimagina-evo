"""Read-only query helpers for the engineer-facing `explore` schema."""
from __future__ import annotations

from reimagina.storage import backend_for_connection


def get_layers(conn) -> list[dict]:
    """List the curated exploration catalog of important layers and views."""
    return backend_for_connection(conn).explore_reader().list_layers()


def get_sources(conn) -> list[dict]:
    """List registered sources joined with ingestion runs for provenance inspection."""
    return backend_for_connection(conn).explore_reader().list_sources()


def get_feature_stats(conn) -> list[dict]:
    """Return counts of canonical features by type, role, source, and dataset version."""
    return backend_for_connection(conn).explore_reader().list_feature_stats()


def get_serving_stats(conn) -> list[dict]:
    """Return high-level counts over the serving layer read contract."""
    return backend_for_connection(conn).explore_reader().list_serving_stats()

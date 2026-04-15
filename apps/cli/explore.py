"""CLI helpers for the engineer-facing exploration schema."""
from __future__ import annotations

from typing import Any

from reimagina.data import explore
from reimagina.db import connect


def list_layers(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "layers": explore.get_layers(conn)}


def list_sources(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "sources": explore.get_sources(conn)}


def list_feature_stats(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "feature_stats": explore.get_feature_stats(conn)}


def list_serving_stats(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "serving_stats": explore.get_serving_stats(conn)}

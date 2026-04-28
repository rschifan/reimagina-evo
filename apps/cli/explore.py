"""CLI helpers for the engineer-facing exploration schema."""

from __future__ import annotations

from typing import Any

from reimagina.db import connect
from reimagina.storage import backend_for_connection


def list_layers(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {
            "dsn": dsn,
            "layers": backend_for_connection(conn).explore_reader().list_layers(),
        }


def list_sources(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {
            "dsn": dsn,
            "sources": backend_for_connection(conn).explore_reader().list_sources(),
        }


def list_feature_stats(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {
            "dsn": dsn,
            "feature_stats": backend_for_connection(conn)
            .explore_reader()
            .list_feature_stats(),
        }


def list_serving_stats(*, dsn: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {
            "dsn": dsn,
            "serving_stats": backend_for_connection(conn)
            .explore_reader()
            .list_serving_stats(),
        }

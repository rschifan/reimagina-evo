"""Minimal API scaffold.

This stays framework-neutral until the concrete HTTP stack is chosen.
"""
from __future__ import annotations


def create_app() -> dict[str, str]:
    return {
        "app": "api",
        "namespace": "reimagina",
        "status": "scaffold",
    }


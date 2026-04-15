"""Minimal worker scaffold."""
from __future__ import annotations


def create_worker() -> dict[str, str]:
    return {
        "app": "worker",
        "namespace": "reimagina",
        "status": "scaffold",
    }


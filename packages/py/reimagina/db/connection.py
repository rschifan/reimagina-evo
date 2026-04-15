"""Database connection management via the swappable storage manager."""
from __future__ import annotations

from dotenv import load_dotenv

from reimagina.storage import connect as storage_connect
from reimagina.storage import default_dsn

load_dotenv()


def get_dsn(dsn: str | None = None) -> str:
    return dsn or default_dsn()


def connect(dsn: str | None = None):
    """Return a backend-specific connection. Caller owns the lifecycle."""
    return storage_connect(get_dsn(dsn))

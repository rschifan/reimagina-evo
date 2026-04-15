"""Bronze zone: land raw files into data/bronze/ unchanged."""
from __future__ import annotations
import os
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
BRONZE_DIR = Path(os.environ.get("REIMAGINA_DATA_DIR", str(_REPO_ROOT / "data"))) / "bronze"


def land_file(source_path: str | Path, source_id: str) -> Path:
    """Copy a raw file into data/bronze/<source_id>/ unchanged.

    Bronze files are immutable after landing — never modified.
    Returns the destination path.
    """
    src = Path(source_path)
    dest = BRONZE_DIR / source_id / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def bronze_uri(source_id: str, filename: str) -> str:
    """Return a file:// URI for a bronze asset (used as storage_uri in data_source_catalog)."""
    return f"file://data/bronze/{source_id}/{filename}"

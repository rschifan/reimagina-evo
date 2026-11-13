"""Bronze zone: land raw files into data/bronze/ unchanged."""

from __future__ import annotations
import shutil
from pathlib import Path

from reimagina.paths import data_dir

BRONZE_DIR = data_dir() / "bronze"


def land_file(source_path: str | Path, source_id: str) -> Path:
    """Copy a raw file into data/bronze/<source_id>/ unchanged.

    Bronze files are immutable after landing — never modified.
    Returns the destination path.

    ⚠ The bronze copy is addressed by ``source_id``, and that is why no column
    records it: a ``data_source_catalog`` row's own id finds its bronze files.
    That catalog's ``storage_uri`` is lineage — where the data came from — and
    is no longer overwritten with this path, which is what ``bronze_uri`` used
    to exist for.
    """
    src = Path(source_path)
    dest = BRONZE_DIR / source_id / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest

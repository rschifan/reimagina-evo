"""Bronze zone: land raw files into data/bronze/ unchanged."""

from __future__ import annotations

import filecmp
import hashlib
import shutil
from pathlib import Path

from reimagina.paths import data_dir

BRONZE_DIR = data_dir() / "bronze"


def land_file(
    source_path: str | Path, source_id: str, *, beside: Path | None = None
) -> Path:
    """Copy a raw file into data/bronze/<source_id>/ unchanged.

    Bronze files are immutable after landing — never modified.
    Returns the destination path.

    Re-landing identical bytes reuses the existing copy. Different bytes under
    a name already landed (places extracted for a second bbox at the same
    release) go to ``<source_id>/<sha256[:12]>/`` instead of overwriting the
    first, whose rows are still loaded and still need their provenance.
    ``beside`` lands a sidecar (``.dbf`` next to ``.shp``) in the main file's
    folder, because a shapefile family only opens from one directory.

    ⚠ The bronze copy is addressed by ``source_id``, and that is why no column
    records it: a ``data_source_catalog`` row's own id finds its bronze files.
    That catalog's ``storage_uri`` is lineage — where the data came from — and
    is no longer overwritten with this path, which is what ``bronze_uri`` used
    to exist for.
    """
    src = Path(source_path)
    folder = beside.parent if beside is not None else BRONZE_DIR / source_id
    dest = folder / src.name
    if dest.exists() and not filecmp.cmp(src, dest, shallow=False):
        if beside is not None:
            raise FileExistsError(f"refusing to overwrite immutable bronze file {dest}")
        dest = folder / _digest(src) / src.name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return dest


def _digest(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()[:12]

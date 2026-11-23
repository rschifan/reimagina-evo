"""Bronze zone: land raw files into data/bronze/ unchanged."""

from __future__ import annotations

import filecmp
import hashlib
import shutil
from collections.abc import Iterable
from pathlib import Path

from reimagina.paths import data_dir

BRONZE_DIR = data_dir() / "bronze"


def land_file(
    source_path: str | Path, source_id: str, *, sidecars: Iterable[Path] = ()
) -> Path:
    """Copy a raw file into data/bronze/<source_id>/ unchanged.

    Bronze files are immutable after landing — never modified.
    Returns the destination path.

    Re-landing identical bytes reuses the existing copy. Different bytes under
    a name already landed (places extracted for a second bbox at the same
    release) go to ``<source_id>/<sha256[:12]>/`` instead of overwriting the
    first, whose rows are still loaded and still need their provenance.
    ``sidecars`` (``.dbf`` next to ``.shp``) land with the main file as one
    family, in one folder, because a shapefile family only opens from one
    directory: if any member differs from what is landed, the whole family
    goes to a folder named by the digest of all its members. A re-exported
    ``.dbf`` under an identical ``.shp`` is a new version, not a conflict.

    ⚠ The bronze copy is addressed by ``source_id``, and that is why no column
    records it: a ``data_source_catalog`` row's own id finds its bronze files.
    That catalog's ``storage_uri`` is lineage — where the data came from — and
    is no longer overwritten with this path, which is what ``bronze_uri`` used
    to exist for.
    """
    family = [Path(source_path), *sorted(Path(s) for s in sidecars)]
    folder = BRONZE_DIR / source_id
    if any(
        (folder / m.name).exists()
        and not filecmp.cmp(m, folder / m.name, shallow=False)
        for m in family
    ):
        folder = folder / _digest(family)
    folder.mkdir(parents=True, exist_ok=True)
    for member in family:
        if not (folder / member.name).exists():
            shutil.copy2(member, folder / member.name)
    return folder / family[0].name


def _digest(paths: list[Path]) -> str:
    """sha256 over the members' bytes in order; one file = its own sha256."""
    h = hashlib.sha256()
    for path in paths:
        with path.open("rb") as fh:
            hashlib.file_digest(fh, lambda: h)  # feeds the one running hash
    return h.hexdigest()[:12]

"""Path-dependency for the in-process titiler ``/cog`` router.

Titiler's default ``DatasetPathParams`` accepts any ``url`` query param
and hands it straight to rasterio. Mounted in-process in our API that
means a client request like ``/cog/info?url=file:///etc/passwd`` would
open an arbitrary host file through GDAL — a local-file-read
vulnerability. This module replaces the default with a validator that
only accepts ``file://`` URLs pointing inside ``host_data_root()``.

The validator is deliberately strict: no HTTP/S, no empty scheme,
no symlink escapes (via ``Path.resolve()`` + ``relative_to`` check),
no missing files. Operators who need remote COG access later can
relax this on a per-deployment basis, but the default is closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlparse

from fastapi import HTTPException, Query

from reimagina.tiles.defaults import host_data_root


def validated_cog_path(
    url: Annotated[
        str,
        Query(
            description=(
                "Absolute file:// URL to the COG to render. Must resolve "
                "inside the data root (REIMAGINA_DATA_DIR). "
                "Non-file schemes and paths outside the data root are "
                "rejected with 403."
            ),
        ),
    ],
) -> str:
    """Resolve an incoming ``url`` query parameter to a safe host path.

    Rejects with HTTP 400/403/404 if the URL violates any of:
      - scheme is not ``file``
      - authority (``//host``) is non-empty (we only accept
        ``file:///abs/path``)
      - resolved path is outside ``host_data_root()``
      - resolved path does not exist on disk
    """
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise HTTPException(
            status_code=400,
            detail=(
                "Only file:// URLs are accepted by this endpoint "
                f"(got scheme {parsed.scheme!r})."
            ),
        )
    if parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=(
                "file:// URL must have an empty authority "
                f"(form: file:///absolute/path). Got netloc {parsed.netloc!r}."
            ),
        )
    # urllib leaves percent-encoded segments in ``path`` — decode before
    # resolving so ``%2e%2e`` can't sneak a traversal past the regex in
    # ``parse_internal_uri``. ``resolve()`` follows symlinks, so one pointing
    # outside the data root is caught by the ``relative_to`` check below.
    # Containment is decided BEFORE existence: a path outside the root gets
    # the same 403 whether or not it exists, so this endpoint cannot be used
    # to probe the host filesystem. No answer echoes a host path. A residual
    # race remains — the file can be swapped between this return and
    # titiler's open — but exploiting it needs write access under the data
    # root, at which point the attacker already controls the served artifact.
    raw_path = unquote(parsed.path)
    if not raw_path:
        raise HTTPException(status_code=400, detail="URL has no path component.")
    outside = HTTPException(
        status_code=403,
        detail="URL resolves outside the data root (REIMAGINA_DATA_DIR).",
    )
    try:
        resolved = Path(raw_path).resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise outside from exc
    try:
        resolved.relative_to(host_data_root().resolve())
    except ValueError as exc:
        raise outside from exc
    try:
        is_file = resolved.is_file()
        exists = is_file or resolved.exists()
    except OSError:
        is_file = exists = False
    if not exists:
        raise HTTPException(
            status_code=404, detail="COG not found under the data root."
        )
    # Only accept regular files — directories, FIFOs, devices, or sockets
    # under the data root must not be opened by GDAL.
    if not is_file:
        raise HTTPException(status_code=403, detail="Not a regular file.")
    # rasterio accepts both plain paths and file:// URIs — return the
    # resolved path so downstream callers see a canonical form.
    return str(resolved)

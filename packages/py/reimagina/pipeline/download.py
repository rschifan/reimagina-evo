"""Stream an HTTP/FTP response to disk, only ever leaving a complete file."""

from __future__ import annotations

import shutil
from pathlib import Path


class IncompleteDownloadError(OSError):
    """The body was shorter than the Content-Length the server announced."""


def save_response(response, dest: Path) -> int:
    """Write ``response`` to ``dest`` atomically and return the byte count.

    ``HTTPResponse.read()`` does not raise when the server drops the
    connection mid-body — it returns what arrived — so a cut-off download
    ends "normally". Comparing against Content-Length (when sent) is the only
    way to tell, and the ``.partial`` + rename keeps a cache that trusts any
    existing file from ever seeing a truncated one.
    """
    partial = dest.with_name(dest.name + ".partial")
    try:
        with open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
        written = partial.stat().st_size
        expected = (getattr(response, "headers", None) or {}).get("Content-Length")
        if expected is not None and written != int(expected):
            raise IncompleteDownloadError(
                f"download cut short: {written} of {expected} bytes"
            )
        partial.replace(dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return written

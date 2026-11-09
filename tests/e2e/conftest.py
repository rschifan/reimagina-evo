"""Fixtures for the browser suite.

Opt-in and explicit about why it skipped. A browser suite that silently does
nothing is worse than no browser suite: it reports a pass for a page it never
opened, which is exactly the shape of the gap this suite exists to close.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

API = os.environ.get("REIMAGINA_E2E_API", "http://127.0.0.1:8001")
WEB = os.environ.get("REIMAGINA_E2E_WEB", "http://127.0.0.1:5173")


def _reachable(url: str) -> bool:
    """Whether *something* is serving at ``url``.

    Two mistakes are baked out of this, both made while writing it and both
    producing the same symptom — a suite that skips with "not reachable"
    while the server is plainly up:

    ⚠ **An HTTP error still means a server answered.** `urlopen` raises
    `HTTPError` on any 4xx or 5xx, so a `200 <= status < 500` check never
    sees them; the probe hit the API's root, which has no route, got a 404
    and concluded nothing was listening. Reachability is "did anything
    reply", not "did it reply well".

    ⚠ **`urlopen` honours `HTTP_PROXY`**, and a sandboxed or corporate proxy
    will not route to localhost. The empty `ProxyHandler` is what keeps the
    probe looking where the servers actually are.
    """
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3):
            return True
    except urllib.error.HTTPError:
        return True  # a server replied; its status is not this probe's business
    except Exception:  # noqa: BLE001 — anything else means nothing is listening
        return False


@pytest.fixture(scope="session")
def servers() -> dict[str, str]:
    if os.environ.get("REIMAGINA_E2E") != "1":
        pytest.skip(
            "browser suite not enabled (needs an API, a web dev server and a "
            "browser; set REIMAGINA_E2E=1)"
        )
    if not shutil.which("node"):
        pytest.skip("node is not on PATH, so Playwright cannot be driven")
    missing = [
        name for name, url in (("API", API), ("web", WEB)) if not _reachable(url)
    ]
    if missing:
        pytest.skip(
            f"{' and '.join(missing)} not reachable. Start them with:\n"
            "    REIMAGINA_APP_DSN=... python -m apps.api\n"
            "    cd apps/web && npm run dev"
        )
    return {"api": API, "web": WEB}


@pytest.fixture(scope="session")
def probe(servers, tmp_path_factory):
    """Run the Playwright driver and return its JSON report.

    One browser launch for the whole session, because launching Chromium is
    the expensive part and every assertion reads the same report. The driver
    is JavaScript because Playwright's Python package is not a dependency of
    this project and must not become one — the licence inventory is derived
    from the installed tree, so a test-only dependency would change what the
    delivery claims to ship.
    """
    import json
    from pathlib import Path

    driver = Path(__file__).parent / "_browser_probe.js"
    out = tmp_path_factory.mktemp("e2e") / "report.json"
    result = subprocess.run(
        ["node", str(driver)],
        env={**os.environ, "E2E_WEB": servers["web"], "E2E_OUT": str(out)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0 or not out.is_file():
        pytest.fail(
            "the browser driver did not produce a report:\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return json.loads(out.read_text())

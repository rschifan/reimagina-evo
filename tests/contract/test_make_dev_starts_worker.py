from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_make_dev_starts_and_reaps_a_worker() -> None:
    """`make dev` without a worker leaves every UI-submitted run `queued` forever.

    The API only enqueues (DBOS client mode); a worker is the only thing that
    dequeues. Starting it is not enough on its own — the EXIT trap must reap it
    too, or a dead API leaves an orphan worker draining runs nobody watches.
    """
    makefile = (ROOT / "Makefile").read_text()
    recipe = re.search(r"^dev:.*?\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe, "no `dev:` recipe in the Makefile"
    body = recipe.group(1)
    assert "-m apps.worker & worker=$$!" in body
    trap = re.search(r"trap '([^']*)' EXIT", body)
    assert trap and "$$worker" in trap.group(1)

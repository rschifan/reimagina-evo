"""The scenario editor, driven end to end in a real browser.

Draw on the baseline city and Apply (which creates the scenario), change a
rule's numbers, drag it, add a later overlapping rule, delete both. Each step
asserts what the map *paints* at a point — the stale-tile, clipping and
box-that-closes-itself defects were all invisible to the API and the table.

⚠ This test writes to the database behind the API it is pointed at. It runs
only with ``REIMAGINA_E2E_WRITES=1``, and it is meant for a scratch stack
(``tests/e2e/README.md`` has the recipe); it deletes the scenario it creates.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def flow(servers, tmp_path_factory) -> dict:
    if os.environ.get("REIMAGINA_E2E_WRITES") != "1":
        pytest.skip(
            "the scenario flow writes scenarios; point it at a scratch stack and "
            "set REIMAGINA_E2E_WRITES=1"
        )
    out = tmp_path_factory.mktemp("flow") / "report.json"
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "_scenario_flow.js")],
        env={
            **os.environ,
            "E2E_WEB": servers["web"],
            "E2E_API": servers["api"],
            "E2E_OUT": str(out),
        },
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0 or not out.is_file():
        pytest.fail(
            "the flow driver did not produce a report:\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return json.loads(out.read_text())


STEPS = [
    "apply on the baseline creates the scenario and shows the change",
    "saving new numbers repaints the block with the new value",
    "dragging a rule moves it in place",
    "the later rule wins in the overlap",
    "an earlier rule says which rule overrides it",
    "deleting every rule returns the scenario to the baseline",
]


@pytest.mark.parametrize("name", STEPS)
def test_step(flow, name):
    steps = {step["name"]: step for step in flow["steps"]}
    crashed = steps.get("the flow ran to the end")
    assert name in steps, f"never reached: {crashed['detail'] if crashed else 'unknown'}"
    assert steps[name]["ok"], json.dumps(steps[name]["detail"], indent=2)


def test_the_page_logged_no_errors(flow):
    assert flow["errors"] == []

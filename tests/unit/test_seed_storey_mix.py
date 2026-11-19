"""The fitted fallback draws a block's storeys from the storeys actually seen.

Deterministic per block, like ``levels_for``: re-running reproduces the same
dataset, so two people see the same map and a comparison means something.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from seed_demo_attributes import storeys_from_mix  # noqa: E402


def test_a_single_storey_mix_only_ever_draws_that_storey():
    assert {storeys_from_mix(str(uuid4()), {3: 7}) for _ in range(200)} == {3}


def test_the_draw_is_stable_for_a_block():
    fid = str(uuid4())
    mix = {1: 1049, 2: 39, 3: 23, 4: 3, 5: 2, 6: 1}
    assert len({storeys_from_mix(fid, mix) for _ in range(20)}) == 1


def test_the_draws_follow_the_mix():
    # Mexicali's tagged buildings: 94% one storey. Over 5,000 blocks the share
    # drawn lands near that, and nothing outside the mix is ever drawn.
    mix = {1: 1049, 2: 39, 3: 23, 4: 3, 5: 2, 6: 1}
    drawn = Counter(storeys_from_mix(str(uuid4()), mix) for _ in range(5000))
    assert set(drawn) <= set(mix)
    assert 0.92 < drawn[1] / 5000 < 0.96

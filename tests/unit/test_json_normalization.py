from __future__ import annotations

import math

from reimagina.storage._utils import normalize_json_value


def test_json_normalization_converts_nan_to_none_recursively():
    payload = {
        "subtype": math.nan,
        "nested": [1, math.nan, {"height": math.nan}],
    }

    assert normalize_json_value(payload) == {
        "subtype": None,
        "nested": [1, None, {"height": None}],
    }

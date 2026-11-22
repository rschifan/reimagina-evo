"""`POST /jobs` and `/jobs/preview` refuse a malformed id at the door (422).

Before, a typo reached a uuid column (500) or, on the explicit-indicators
path, was persisted into a durable run that could only fail later.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.routers.jobs import SubmitRunBody

GOOD = "4f0c1a8e-2b7d-4c1e-9f3a-0d6b5e7c8a91"


@pytest.mark.parametrize("field", ["scenario_id", "aoi_id"])
def test_a_malformed_id_is_refused(field):
    with pytest.raises(ValidationError):
        SubmitRunBody(**{field: "abc"})


@pytest.mark.parametrize("field", ["scenario_id", "aoi_id"])
def test_a_valid_id_passes_on_as_its_canonical_string(field):
    body = SubmitRunBody(**{field: GOOD.upper()})
    assert getattr(body, field) == GOOD


def test_ids_stay_optional():
    body = SubmitRunBody()
    assert body.scenario_id is None and body.aoi_id is None


def test_a_malformed_edited_feature_is_refused():
    # Compared as uuid in `runners._covers_overrides`; a typo has to stop here.
    with pytest.raises(ValidationError):
        SubmitRunBody(edited_features=[GOOD, "abc"])


def test_edited_features_pass_on_as_canonical_strings():
    body = SubmitRunBody(edited_features=[GOOD.upper()])
    assert body.edited_features == [GOOD]

"""The idempotency window must actually fire, and nothing else asserts that.

`RunInput.dedup_key` feeds `TTLBasedIdempotencyConfig(key_expression=
"input.dedup_key")`, so two submissions of the same work inside the TTL collapse
to one run. That behaviour has never been tested. Both conftests work *around*
the window by giving every submission a distinct `code_version`; the only tests
named `idempotent` cover source registration and are unrelated.

The gap matters because the fingerprint is about to be rewritten. When the
feature ids move out of `RunInput` and into `jobs_runtime.run_plan`, the payload
gains a freshly-minted `plan_id` — and folding that into the key would make
every fingerprint unique, silently disabling deduplication with a green suite.
These tests are written before that change so the change is guarded rather than
merely careful.

Deliberately at the submit boundary rather than on `_dedup_key` directly: the
regression would be introduced at the call site, by passing the wrong thing into
a function that is itself correct.
"""

from __future__ import annotations

import uuid

import pytest

from reimagina.orchestration.impact import DirtyUnit
from reimagina.orchestration.plan import RunPlan

SCENARIO = "11111111-1111-4111-8111-111111111111"
FEATURES = ("feature-a", "feature-b", "feature-c")


class _Ref:
    workflow_run_id = "22222222-2222-4222-8222-222222222222"


def _submit(monkeypatch, *, units, code_version="test", scenario_id=SCENARIO):
    """Run one submission with the engine faked out; return the RunInput sent."""
    from reimagina.orchestration import hatchet_app, hatchet_orchestrator

    captured: list = []

    class _Workflow:
        def run(self, payload, **_kwargs):
            captured.append(payload)
            return _Ref()

    monkeypatch.setattr(
        hatchet_orchestrator,
        "plan_run",
        lambda *a, **k: RunPlan(
            units=[DirtyUnit(slug, frozenset(features)) for slug, features in units],
            params={
                "code_version": code_version,
                "scenario_id": scenario_id,
                "aoi_id": None,
            },
        ),
    )
    monkeypatch.setattr(
        hatchet_orchestrator, "assert_same_domain_database", lambda *a, **k: None
    )
    # ⚠ A *different* plan_id per submission, deliberately. That is what the real
    # thing does, and it is precisely the condition under which folding plan_id
    # into the fingerprint would break deduplication. Returning a constant here
    # would make these tests pass against the very regression they exist to catch.
    monkeypatch.setattr(
        hatchet_orchestrator, "_persist_plan", lambda *a, **k: str(uuid.uuid4())
    )
    monkeypatch.setattr(hatchet_app, "get_run_workflow", lambda: _Workflow())
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")

    hatchet_orchestrator.HatchetOrchestrator(registry=()).submit_run(
        object(),
        changed_attributes={"geometry"},
        edited_features=set(FEATURES),
        code_version=code_version,
        scenario_id=scenario_id,
    )
    return captured[0]


def test_identical_work_submitted_twice_shares_a_dedup_key(monkeypatch):
    """The property the whole idempotency window rests on.

    If this ever fails, two identical submissions land as two runs and the
    stale-then-insert write path lets them fight over the same rows.
    """
    units = [("urban_form_block_compactness", FEATURES)]
    first = _submit(monkeypatch, units=units)
    second = _submit(monkeypatch, units=units)

    assert first.dedup_key == second.dedup_key
    assert first.dedup_key != ""


def test_a_different_feature_scope_is_different_work(monkeypatch):
    """Two edits to one scenario inside the TTL must both run.

    The key covers the per-unit feature scope, not just the scenario — otherwise
    a second, different edit would be swallowed as a duplicate of the first.
    """
    a = _submit(monkeypatch, units=[("urban_form_block_compactness", ("feature-a",))])
    b = _submit(monkeypatch, units=[("urban_form_block_compactness", ("feature-b",))])

    assert a.dedup_key != b.dedup_key


def test_a_different_dirty_set_is_different_work(monkeypatch):
    a = _submit(monkeypatch, units=[("urban_form_block_compactness", FEATURES)])
    b = _submit(
        monkeypatch,
        units=[
            ("urban_form_block_compactness", FEATURES),
            ("urban_form_block_building_density", FEATURES),
        ],
    )

    assert a.dedup_key != b.dedup_key


def test_a_different_code_version_is_different_work(monkeypatch):
    a = _submit(monkeypatch, units=[("slug", FEATURES)], code_version="v1")
    b = _submit(monkeypatch, units=[("slug", FEATURES)], code_version="v2")

    assert a.dedup_key != b.dedup_key


def test_feature_order_does_not_change_the_fingerprint(monkeypatch):
    """Two processes deriving the same dirty set must agree on the key.

    `DirtyUnit.features` is a frozenset, whose iteration order is not stable
    across processes; the caller sorts before hashing. Without that, the same
    edit fingerprints differently depending on who submitted it and the window
    never fires in production.
    """
    a = _submit(monkeypatch, units=[("slug", ("f-a", "f-b", "f-c"))])
    b = _submit(monkeypatch, units=[("slug", ("f-c", "f-a", "f-b"))])

    assert a.dedup_key == b.dedup_key


@pytest.mark.parametrize("field", ["dirty", "scenario_id", "code_version", "plan_id"])
def test_the_payload_still_carries_what_the_key_hashes(monkeypatch, field):
    """The key's inputs must survive on the payload — plus the handle itself.

    ``features`` is deliberately absent from this list now: the ids moved into
    ``jobs_runtime.run_plan`` and the payload carries only ``plan_id``. The
    fingerprint still covers the feature scope, computed from the ids while they
    are in memory at submit — which is what the equality tests above prove.
    """
    payload = _submit(monkeypatch, units=[("slug", FEATURES)])
    assert getattr(payload, field) not in (None, "", [], {})


def test_the_payload_no_longer_carries_the_feature_ids(monkeypatch):
    """The ceiling fix, asserted at the boundary that used to break.

    A full-city run put 258,675 uuids here — 10 MB against a 4 MiB gRPC limit —
    and could not be submitted at all. The handle replaces them.
    """
    payload = _submit(monkeypatch, units=[("slug", FEATURES)])

    # The field is gone from the model, not merely left empty — an unused second
    # path to the feature set is a path someone eventually populates.
    assert not hasattr(payload, "features")
    assert payload.plan_id
    # A couple of hundred bytes, and flat in the size of the city.
    assert len(payload.model_dump_json()) < 1024


def test_an_unscoped_run_serialises_on_its_own_work_not_a_shared_bucket(monkeypatch):
    """The fallback is the fingerprint, not the constant `"baseline"`.

    With no scenario and no AOI there is no named scope to protect, and the
    constant put every such run into one `max_runs=1` bucket — so unrelated
    recomputes queued behind each other for no reason, and under the earlier
    `CANCEL_NEWEST` strategy cancelled each other outright.

    Two unscoped runs genuinely conflict only when they touch the same features,
    which is exactly the set `dedup_key` hashes. Reusing it introduces no new
    concept and gives distinct work distinct buckets.
    """
    payload = _submit(monkeypatch, units=[("slug", FEATURES)], scenario_id=None)

    assert payload.scenario_id is None
    assert payload.aoi_id is None
    assert payload.concurrency_key != "baseline"
    assert payload.concurrency_key == payload.dedup_key


def test_unrelated_unscoped_runs_land_in_different_buckets(monkeypatch):
    """The regression the constant caused, asserted directly."""
    one = _submit(monkeypatch, units=[("slug", FEATURES)], scenario_id=None)
    other = _submit(monkeypatch, units=[("other_slug", FEATURES)], scenario_id=None)

    assert one.concurrency_key != other.concurrency_key


def test_a_scoped_run_still_serialises_on_its_scope(monkeypatch):
    """The fallback must not displace the scope when there is one.

    A scenario names the rows two runs would fight over; two *different* edits
    to one scenario must still serialise, and they have different fingerprints.
    """
    one = _submit(monkeypatch, units=[("slug", FEATURES)], scenario_id=SCENARIO)
    other = _submit(monkeypatch, units=[("other_slug", FEATURES)], scenario_id=SCENARIO)

    assert one.concurrency_key == SCENARIO
    assert other.concurrency_key == SCENARIO
    assert one.dedup_key != other.dedup_key

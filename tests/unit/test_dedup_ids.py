"""What the deduplication id must and must not collapse.

Every assertion here is one of two shapes, and the asymmetry between them is
the whole design. Two submissions that would do *identical* work must get the
same id, or the dedup does nothing. Two submissions that would do *different*
work must get different ids, or the dedup suppresses a run someone asked for
and the answer they get back is stale.

The second kind is the dangerous one, and it has a case that is easy to miss:
a scenario edit does not change the request at all. Same attribute, same
features, same scenario id. Only the values change. An id built from the
request shape alone would collapse those two submissions and hand the
operator the run computing the numbers they had just replaced.
"""

from __future__ import annotations

from reimagina.assets.keys import indicator_asset_key, partition_key
from reimagina.orchestration.dedup import deduplication_id


class _Cursor:
    def __init__(self, versions, missing):
        self._versions = versions
        self._missing = missing
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        keys = params[0] if params else ()
        self._rows = [
            (key, part, self._versions.get((key, part), "v-default"))
            for key, part in keys
            if (key, part) not in self._missing
        ]

    def fetchall(self):
        return self._rows


class _Conn:
    """Answers the asset-version lookup from a dict, and can withhold a key."""

    def __init__(self, versions=None, missing=()):
        self._versions = versions or {}
        self._missing = set(missing)

    def cursor(self):
        return _Cursor(self._versions, self._missing)


def _id(conn=None, **overrides):
    request = {
        "unit_slugs": ["a", "b"],
        "changed_attributes": ["floors"],
        "edited_features": ["f1", "f2"],
        "scenario_id": None,
        "aoi_id": None,
        "code_version": "abc123",
        "indicators": None,
    }
    request.update(overrides)
    return deduplication_id(conn or _Conn(), **request)


# ── identical work collapses ────────────────────────────────────────────────


def test_the_same_request_gives_the_same_id():
    assert _id() == _id()


def test_input_order_does_not_matter():
    """A set arriving in a different order is the same set.

    `changed_attributes` and `edited_features` are sets at the port, but they
    reach here as iterables and their iteration order is not stable across
    processes — so without normalisation two identical submissions from two
    API workers would get different ids and neither would deduplicate.
    """
    assert _id(edited_features=["f2", "f1"], unit_slugs=["b", "a"]) == _id()


def test_duplicate_entries_do_not_change_the_id():
    assert _id(edited_features=["f1", "f1", "f2"]) == _id()


# ── different work must not collapse ────────────────────────────────────────


def test_a_changed_input_value_changes_the_id():
    """The case the request shape cannot see.

    Editing a scenario override republishes `scenario.override.<attribute>`,
    which moves the closure fingerprint of every indicator downstream of it
    while the request stays byte-identical.
    """
    before = _id(conn=_Conn({(indicator_asset_key("a"), ""): "version-1"}))
    after = _id(conn=_Conn({(indicator_asset_key("a"), ""): "version-2"}))
    assert before != after


def test_each_request_field_changes_the_id():
    base = _id()
    for field, value in (
        ("changed_attributes", ["num_levels"]),
        ("edited_features", ["f1", "f3"]),
        ("scenario_id", "s1"),
        ("aoi_id", "aoi-1"),
        ("code_version", "def456"),
        ("indicators", ["a"]),
        ("unit_slugs", ["a"]),
    ):
        assert _id(**{field: value}) != base, f"{field} does not affect the id"


def test_a_scenario_reads_its_own_partition():
    """Baseline and scenario are different work even with the same request.

    They resolve different values for the same attribute, and their indicator
    assets are different partitions, so the fingerprints differ.
    """
    part = partition_key({"scenario": "s1"})
    conn = _Conn(
        {
            (indicator_asset_key("a"), ""): "baseline-version",
            (indicator_asset_key("a"), part): "scenario-version",
        }
    )
    assert _id(conn=conn, unit_slugs=["a"]) != _id(
        conn=conn, unit_slugs=["a"], scenario_id="s1"
    )


# ── failing open ────────────────────────────────────────────────────────────


def test_a_unit_without_a_fingerprint_disables_deduplication():
    """No id rather than a weaker one.

    A unit whose inputs cannot be fingerprinted — an asset graph not yet
    backfilled — cannot be checked for input changes. Deduplicating on the
    request alone would be exactly the collapse this module exists to avoid,
    so nothing is deduplicated instead. A redundant run costs CPU; a wrongly
    suppressed one costs an answer.
    """
    conn = _Conn(missing={(indicator_asset_key("b"), "")})
    assert _id(conn=conn) is None


def test_an_empty_plan_has_no_id():
    assert _id(unit_slugs=[]) is None


def test_the_id_names_its_scheme():
    """So a future composition cannot be mistaken for this one.

    The ids are hashes sharing one namespace; without the prefix, changing
    what goes into the digest would let an old in-flight run capture a new
    submission whose hash happened to collide.
    """
    assert _id().startswith("v1:")

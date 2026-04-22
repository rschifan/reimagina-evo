"""Unit tests for the advisory-lock key derivation.

``pg_try_advisory_xact_lock`` accepts any int64. Our derivation must be
(a) deterministic so the same prefix always locks the same key, (b)
pinned to big-endian + signed int semantics so it matches across
Python interpreter builds and Postgres client versions, and (c)
namespaced so we don't collide with any other advisory-lock user in
the database.
"""
from __future__ import annotations

from reimagina.jobs.raster_zonal_stats import _prefix_lock_key


def test_deterministic_across_calls():
    assert _prefix_lock_key("lst_day") == _prefix_lock_key("lst_day")


def test_different_prefixes_produce_different_keys():
    keys = {_prefix_lock_key(p) for p in ("lst_day", "lst_night", "ndvi")}
    assert len(keys) == 3


def test_result_fits_in_signed_int64():
    lo = -(2 ** 63)
    hi = 2 ** 63 - 1
    for prefix in ("a", "a" * 1024, "lst_day", "ndvi_mean"):
        key = _prefix_lock_key(prefix)
        assert lo <= key <= hi
        assert isinstance(key, int)


def test_empty_prefix_still_returns_int64():
    # The caller-facing validation (``retire_current_by_prefix``) is
    # what rejects empty prefixes — this function is a pure derivation
    # and must not raise.
    key = _prefix_lock_key("")
    assert isinstance(key, int)
    assert -(2 ** 63) <= key <= 2 ** 63 - 1


def test_namespace_salt_isolates_from_bare_blake2b():
    # The key for prefix "x" must NOT equal blake2b("x") — the
    # ``reimagina.raster.`` salt is what namespaces us against other
    # advisory-lock users.
    import hashlib
    bare = int.from_bytes(
        hashlib.blake2b(b"x", digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    assert _prefix_lock_key("x") != bare

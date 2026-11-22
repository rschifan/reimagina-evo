"""The bare-key probe must not swallow a DB error it cannot recover from."""

from __future__ import annotations

import psycopg2
import pytest

from reimagina.tiles.sources import _attribute_key_for_recipe


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_args):
        raise psycopg2.OperationalError("canceling statement due to timeout")


class _Conn:
    def cursor(self):
        return _Cursor()


class _Backend:
    connection = _Conn()


def test_a_failed_probe_propagates_instead_of_poisoning_the_transaction():
    # Swallowing it left the request's transaction aborted, so the next
    # resolver query failed with an unrelated InFailedSqlTransaction 500.
    recipe = {
        "attribute_key_prefix": "censo_2020_pobtot_ageb",
        "input_kind": "observation",
        "method": "sum",
    }
    with pytest.raises(psycopg2.OperationalError):
        _attribute_key_for_recipe(recipe, backend=_Backend())

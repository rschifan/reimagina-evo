"""A decodable but meaningless cursor is a client error, not a SQL cast 500."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import pytest

from reimagina.storage._utils import decode_cursor, encode_cursor


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


@pytest.mark.parametrize(
    "payload",
    ["x|y", f"x|{uuid.uuid4()}", "2026-01-01T00:00:00+00:00|not-a-uuid"],
)
def test_well_formed_base64_with_bogus_content_raises_value_error(payload):
    with pytest.raises(ValueError):
        decode_cursor(_b64(payload))


def test_round_trip():
    ts = datetime(2026, 11, 22, 10, 30, 1, 123456, tzinfo=UTC)
    fid = str(uuid.uuid4())
    assert decode_cursor(encode_cursor(ts, fid)) == (ts.isoformat(), fid)

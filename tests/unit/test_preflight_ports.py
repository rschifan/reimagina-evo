"""`make dev` must fail before it claims a port it cannot have.

The failure it replaces: the recipe printed "API → http://127.0.0.1:8001",
then uvicorn died with a bare `[Errno 98] Address already in use` — no port
named, no owner, no remedy — and the trap took the web server down with it. A
developer sees a cheerful banner followed by an errno, and nothing says which
of the two ports was the problem.
"""

from __future__ import annotations

import socket

from scripts.preflight_ports import PortInUse, check_ports, describe_holder


def _occupied() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def test_free_ports_pass():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()  # closed, so nothing holds it
    check_ports([(free, "API")])  # must not raise


def test_a_held_port_is_reported_with_its_label():
    sock = _occupied()
    try:
        port = sock.getsockname()[1]
        try:
            check_ports([(port, "API")])
        except PortInUse as exc:
            assert str(port) in str(exc)
            assert "API" in str(exc), "the message must say which server wanted it"
        else:
            raise AssertionError("a held port must not pass the preflight")
    finally:
        sock.close()


def test_every_held_port_is_named_not_just_the_first():
    """Both servers' ports are checked before either starts.

    Reporting only the first means a developer frees one port, re-runs, and
    hits the other — two round trips to learn what one message could say.
    """
    first, second = _occupied(), _occupied()
    try:
        ports = [
            (first.getsockname()[1], "API"),
            (second.getsockname()[1], "Web"),
        ]
        try:
            check_ports(ports)
        except PortInUse as exc:
            assert str(ports[0][0]) in str(exc)
            assert str(ports[1][0]) in str(exc)
        else:
            raise AssertionError("held ports must not pass the preflight")
    finally:
        first.close()
        second.close()


def test_describe_holder_never_raises_on_a_free_port():
    """Naming the process is a convenience; `lsof` may be absent or refuse.

    The preflight's job is to fail clearly, so an unavailable helper must
    degrade to saying nothing rather than replacing a useful message with a
    traceback.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()
    assert isinstance(describe_holder(free), str)

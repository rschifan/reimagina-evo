"""Refuse to start a dev server on a port something already holds.

Why this exists as a step of its own rather than letting the servers fail:
``make dev`` prints its banner *before* either server binds, so the previous
failure read as

    API  → http://127.0.0.1:8001
    Web  → http://localhost:5173
    ...
    ERROR:    [Errno 98] Address already in use

— a cheerful address, then an errno that names no port, no owner and no
remedy, with the surviving server torn down by the trap a moment later. Both
ports are checked here, before anything is claimed or announced, so one
message can say which port, what holds it, and what to do instead.

Run directly: ``python scripts/preflight_ports.py 8001:API 5173:Web``
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys


class PortInUse(RuntimeError):
    """One or more requested ports are already bound."""


def describe_holder(port: int) -> str:
    """Name the process holding ``port``, or return "" if it cannot be known.

    Best-effort by design. ``lsof`` is absent on a plain Debian image and may
    decline to report another user's process, and this runs on macOS and Linux
    both — so every failure degrades to saying nothing. The preflight's value
    is the clear refusal; the owner's name is a bonus that must never replace
    it with a traceback.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return ""
    try:
        done = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "cp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    pid = command = ""
    for line in done.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            command = line[1:]
    if pid and command:
        return f"{command} (pid {pid})"
    return ""


def _is_free(port: int) -> bool:
    """True when ``port`` can be bound on the loopback address.

    ⚠ No ``SO_REUSEADDR``. Setting it is the usual reflex and would defeat the
    whole check: it lets a bind succeed against a socket in TIME_WAIT, and on
    Linux it would also mask a listener bound to a different local address.
    The question here is exactly "would a server starting now fail?", so the
    bind must be as strict as the server's own.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_ports(ports: list[tuple[int, str]]) -> None:
    """Raise :class:`PortInUse` naming every busy port, not merely the first.

    All of them, because reporting one at a time costs a developer a round
    trip per port: free the API's, re-run, discover the web server's.
    """
    busy = [(port, label) for port, label in ports if not _is_free(port)]
    if not busy:
        return

    lines = []
    for port, label in busy:
        holder = describe_holder(port)
        held_by = f" — held by {holder}" if holder else ""
        lines.append(f"  {label} wants port {port}{held_by}")

    remedies = [
        "Either stop whatever holds the port, or run on another one:",
        "  make dev API_PORT=8123 WEB_PORT=5174",
        "A stale server from an earlier run is the usual cause; a previous",
        "`make dev` in another terminal holds both.",
    ]
    raise PortInUse(
        "cannot start: a port is already in use.\n"
        + "\n".join(lines)
        + "\n"
        + "\n".join(remedies)
    )


def main(argv: list[str]) -> int:
    ports: list[tuple[int, str]] = []
    for arg in argv:
        port, _, label = arg.partition(":")
        ports.append((int(port), label or port))
    try:
        check_ports(ports)
    except PortInUse as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

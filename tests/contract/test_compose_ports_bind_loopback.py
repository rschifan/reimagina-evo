"""Every port the compose stack publishes binds loopback unless told otherwise.

Docker's `ports: "H:C"` binds 0.0.0.0 and sidesteps host firewalls (ufw) via
iptables. The `postgres` image makes POSTGRES_USER a superuser and
`.env.example` ships a working password, so an unprefixed `5433:5432` on a
laptop on shared Wi-Fi hands anyone on the network a superuser shell
(`COPY ... TO PROGRAM`). Martin serves every tile unauthenticated.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = (
    Path(__file__).resolve().parents[2] / "infra" / "docker" / "docker-compose.yml"
)
LOOPBACK_DEFAULT = "${REIMAGINA_BIND_ADDRESS:-127.0.0.1}:"


def test_every_published_port_defaults_to_loopback() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    published = {
        f"{name}:{port}": port
        for name, svc in services.items()
        for port in svc.get("ports", [])
    }
    assert published, "compose publishes no ports; this test is stale"
    wide_open = [
        k for k, p in published.items() if not str(p).startswith(LOOPBACK_DEFAULT)
    ]
    assert not wide_open, f"published on every interface: {wide_open}"

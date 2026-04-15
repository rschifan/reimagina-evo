from __future__ import annotations

from apps.api.app import create_app
from apps.cli.app import create_cli
from apps.worker.app import create_worker


def test_api_scaffold_boots():
    assert create_app()["app"] == "api"


def test_worker_scaffold_boots():
    assert create_worker()["app"] == "worker"


def test_cli_scaffold_boots():
    assert create_cli()["app"] == "cli"

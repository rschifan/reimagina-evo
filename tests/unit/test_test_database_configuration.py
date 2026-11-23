"""Standalone test suites must use the checkout's declared test database."""

import os
from pathlib import Path
import subprocess
import sys
import runpy

import pytest


@pytest.mark.parametrize("exported", [False, True])
def test_test_database_configuration_loads_dotenv_without_overriding_exports(
    tmp_path, exported
):
    source = Path(__file__).resolve().parents[1] / "conftest.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text(source.read_text())
    (tmp_path / ".env").write_text(
        "TEST_DATABASE_URL=postgresql://localhost:65432/from_dotenv_test\n"
    )
    env = dict(os.environ)
    env.pop("TEST_DATABASE_URL", None)
    if exported:
        env["TEST_DATABASE_URL"] = "postgresql://localhost:65433/from_export_test"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; print(runpy.run_path('tests/conftest.py')['_test_db_url']())",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == (
        "postgresql://localhost:65433/from_export_test"
        if exported
        else "postgresql://localhost:65432/from_dotenv_test"
    )


def test_test_database_guard_checks_the_effective_libpq_database(monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
    guard = module["_assert_db_name_is_test_safe"]
    monkeypatch.delenv("REIMAGINA_TEST_DB_ALLOW_NAME", raising=False)
    with pytest.raises(RuntimeError, match="city"):
        guard("postgresql://localhost/scratch_test?dbname=city")
    guard("postgresql://localhost/city?dbname=scratch_test")


def test_database_suites_do_not_silently_select_a_default_database(tmp_path):
    source = Path(__file__).resolve().parents[1] / "conftest.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text(source.read_text())
    env = dict(os.environ)
    env.pop("TEST_DATABASE_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; print(runpy.run_path('tests/conftest.py')['_test_db_url']())",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "TEST_DATABASE_URL" in result.stderr


@pytest.mark.parametrize("name", ["reimagina_latest", "contest", "attestation"])
def test_test_database_guard_wants_test_as_a_word_not_a_substring(monkeypatch, name):
    """`downgrade base` wipes the database; "latest" is not a test database."""
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
    guard = module["_assert_db_name_is_test_safe"]
    monkeypatch.delenv("REIMAGINA_TEST_DB_ALLOW_NAME", raising=False)
    with pytest.raises(RuntimeError, match=name):
        guard(f"postgresql://localhost/{name}")


@pytest.mark.parametrize(
    "name", ["reimagina_test", "reimagina_test_bench_dbos", "test_reimagina", "ci-test"]
)
def test_test_database_guard_accepts_test_named_databases(monkeypatch, name):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))
    monkeypatch.delenv("REIMAGINA_TEST_DB_ALLOW_NAME", raising=False)
    module["_assert_db_name_is_test_safe"](f"postgresql://localhost/{name}")

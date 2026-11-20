"""Safety boundaries for shell harnesses; never connect to a database."""

from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def fixture_repo(tmp_path):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "data/sources/lake/inegi/marco_geostadistico/2024").mkdir(parents=True)
    (tmp_path / "apps/cli").mkdir(parents=True)
    (tmp_path / "apps/__init__.py").touch()
    (tmp_path / "apps/cli/__init__.py").touch()
    (tmp_path / "apps/cli/app.py").write_text(
        'import json, os, sys\nprint("ENV=" + json.dumps(dict(os.environ)))\nsys.exit(77)\n'
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE_", "REIMAGINA_", "TEST_DATABASE_"))
    }
    env["PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(tmp_path)
    return env


@pytest.mark.parametrize("script", ["acceptance_run.sh", "mexicali_run.sh"])
def test_harness_preserves_exports_and_does_not_execute_dotenv(tmp_path, script):
    env = fixture_repo(tmp_path)
    marker = tmp_path / "executed"
    (tmp_path / ".env").write_text(
        f"DATABASE_URL=postgresql://localhost/file_db\nDATABASE_URL_DIRECT=postgresql://localhost/file_db\nDATABASE_URL_ADMIN=postgresql://localhost/postgres\nINERT=$(touch {marker})\n"
    )
    env.update(
        DATABASE_URL="postgresql://localhost/scratch",
        DATABASE_URL_DIRECT="postgresql://localhost/scratch",
        DATABASE_URL_ADMIN="postgresql://localhost/postgres",
    )
    done = subprocess.run(
        ["bash", f"scripts/{script}", "--keep"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert not marker.exists(), "dotenv content executed as shell"
    lines = [line for line in done.stdout.splitlines() if line.startswith("ENV=")]
    assert lines, done.stdout + done.stderr
    actual = json.loads(lines[0][4:])
    assert actual["DATABASE_URL"] == env["DATABASE_URL"]
    assert actual["DATABASE_URL_DIRECT"] == env["DATABASE_URL_DIRECT"]


@pytest.mark.parametrize(
    "direct,admin",
    [
        ("postgresql://localhost/other", "postgresql://localhost/postgres"),
        ("postgresql://remote/scratch", "postgresql://localhost/postgres"),
        ("postgresql://localhost/scratch", "postgresql://remote/postgres"),
        ("postgresql://localhost/scratch", "postgresql://localhost/scratch"),
    ],
)
@pytest.mark.parametrize("script", ["acceptance_run.sh", "mexicali_run.sh"])
def test_harness_rejects_inconsistent_targets_before_connect(
    tmp_path, script, direct, admin
):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="postgresql://u:private@localhost/scratch",
        DATABASE_URL_DIRECT=direct,
        DATABASE_URL_ADMIN=admin,
    )
    # A DB driver stub makes any attempt to connect observable and harmless.
    (tmp_path / "sitecustomize.py").write_text(
        'import psycopg2\ndef refuse(*a, **kw):\n print("UNSAFE_CONNECT", flush=True)\n raise RuntimeError("must validate before connecting")\npsycopg2.connect = refuse\n'
    )
    done = subprocess.run(
        ["bash", f"scripts/{script}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "UNSAFE_CONNECT" not in done.stdout + done.stderr
    assert "private" not in done.stdout + done.stderr
    assert "DATABASE_URL" in done.stderr


@pytest.mark.parametrize("script", ["acceptance_run.sh", "mexicali_run.sh"])
def test_harness_preflights_cli_before_drop_and_migrates_only_app(tmp_path, script):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="postgresql://localhost/scratch",
        DATABASE_URL_ADMIN="postgresql://localhost/postgres",
    )
    (tmp_path / "apps/cli/app.py").write_text(
        'import json, os, sys\nprint("ARGS=" + json.dumps(sys.argv))\nsys.exit(77)\n'
    )
    (tmp_path / "sitecustomize.py").write_text(
        'import psycopg2\ndef refuse(*a, **kw):\n print("UNSAFE_CONNECT", flush=True)\n raise RuntimeError("preflight must fail before connecting")\npsycopg2.connect = refuse\n'
    )
    done = subprocess.run(
        ["bash", f"scripts/{script}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "UNSAFE_CONNECT" not in done.stdout + done.stderr
    kept = subprocess.run(
        ["bash", f"scripts/{script}", "--keep"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = [
        json.loads(line[5:])
        for line in kept.stdout.splitlines()
        if line.startswith("ARGS=")
    ]
    assert calls
    assert all("--with-test-db" not in call for call in calls)


def test_drop_uses_decoded_database_identifier_and_parameterized_termination(tmp_path):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="postgresql://localhost/odd%22%3B--",
        DATABASE_URL_DIRECT="postgresql://localhost/odd%22%3B--",
        DATABASE_URL_ADMIN="postgresql://localhost/postgres",
    )
    (tmp_path / "sitecustomize.py").write_text("""import json, psycopg2
from psycopg2 import sql
class Connection:
 def cursor(self): return self
 def __enter__(self): return self
 def __exit__(self, *a): pass
 def close(self): pass
 def execute(self, query, params=None):
  if isinstance(query, sql.Composed):
   assert isinstance(query.seq[1], sql.Identifier)
   print("IDENTIFIER=" + json.dumps(query.seq[1].strings))
  else:
   print("PARAMS=" + json.dumps(params))
psycopg2.connect = lambda *a, **kw: Connection()
""")
    done = subprocess.run(
        [sys.executable, "scripts/runtime_env.py", "drop"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'IDENTIFIER=["odd\\";--"]' in done.stdout
    assert 'PARAMS=["odd\\";--"]' in done.stdout


@pytest.mark.parametrize("script", ["acceptance_run.sh", "mexicali_run.sh"])
def test_harness_rejects_different_cluster_port_before_drop(tmp_path, script):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="postgresql://localhost:55434/scratch",
        DATABASE_URL_DIRECT="postgresql://localhost:5432/scratch",
        DATABASE_URL_ADMIN="postgresql://localhost:5432/postgres",
    )
    (tmp_path / "sitecustomize.py").write_text(
        'import psycopg2\ndef refuse(*a, **kw):\n print("UNSAFE_CONNECT", flush=True)\n raise RuntimeError("different cluster")\npsycopg2.connect = refuse\n'
    )
    (tmp_path / "apps/cli/app.py").write_text(
        'import sys\nsys.exit(0 if "status" in sys.argv or "verify-sources" in sys.argv else 77)\n'
    )
    done = subprocess.run(
        ["bash", f"scripts/{script}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "UNSAFE_CONNECT" not in done.stdout + done.stderr
    assert "DATABASE_URL" in done.stderr


@pytest.mark.parametrize(
    "app", ["postgresql://localhost/scratch", "host=localhost dbname=scratch"]
)
def test_harness_checks_effective_pgport_before_drop(tmp_path, app):
    env = fixture_repo(tmp_path)
    env.update(
        PGPORT="55434",
        DATABASE_URL=app,
        DATABASE_URL_DIRECT="postgresql://localhost:5432/scratch",
        DATABASE_URL_ADMIN="postgresql://localhost:5432/postgres",
    )
    done = subprocess.run(
        [sys.executable, "scripts/runtime_env.py", "harness"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "DATABASE_URL" in done.stderr


@pytest.mark.parametrize(
    "key,value", [("PGSERVICE", "other_cluster"), ("PGHOSTADDR", "192.0.2.10")]
)
def test_harness_rejects_libpq_environment_endpoint_overrides(tmp_path, key, value):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="host=localhost dbname=scratch",
        DATABASE_URL_ADMIN="host=localhost dbname=postgres",
    )
    env[key] = value
    done = subprocess.run(
        [sys.executable, "scripts/runtime_env.py", "harness"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert key in done.stderr


def test_harness_allows_matching_pgport_and_libpq_credentials(tmp_path):
    env = fixture_repo(tmp_path)
    env.update(
        PGPORT="55434",
        PGUSER="local_user",
        PGPASSWORD="local_password",
        DATABASE_URL="postgresql://localhost/scratch",
        DATABASE_URL_DIRECT="postgresql://localhost:55434/scratch",
        DATABASE_URL_ADMIN="host=127.0.0.1 dbname=postgres",
    )
    done = subprocess.run(
        [sys.executable, "scripts/runtime_env.py", "harness"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "local_password" not in done.stderr


@pytest.mark.parametrize("key", ["DATABASE_URL", "DATABASE_URL_DIRECT"])
@pytest.mark.parametrize(
    "dsn", ["host=localhost dbname=scratch", "postgres://localhost/scratch"]
)
def test_drop_rejects_unsupported_migration_dsns_before_connect(tmp_path, key, dsn):
    env = fixture_repo(tmp_path)
    env.update(
        DATABASE_URL="postgresql://localhost/scratch",
        DATABASE_URL_DIRECT="postgresql://localhost/scratch",
        DATABASE_URL_ADMIN="host=localhost dbname=postgres",
    )
    env[key] = dsn
    (tmp_path / "sitecustomize.py").write_text(
        'import psycopg2\ndef refuse(*a, **kw):\n print("UNSAFE_CONNECT", flush=True)\n raise RuntimeError("unsupported migration DSN")\npsycopg2.connect = refuse\n'
    )
    done = subprocess.run(
        [sys.executable, "scripts/runtime_env.py", "drop"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "UNSAFE_CONNECT" not in done.stdout + done.stderr
    assert key in done.stderr

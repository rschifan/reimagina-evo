"""Exercise the real Make recipes with isolated executables and no services."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    shutil.copy(ROOT / "Makefile", tmp_path)
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/__init__.py").touch()
    (tmp_path / "apps/worker.py").write_text(
        "import runpy\nrunpy.run_path('stub.py', run_name='__main__')\n"
    )
    (tmp_path / "stub.py").write_text("""import json, os, subprocess, sys, time
from pathlib import Path
name = sys.argv[1] if len(sys.argv) > 1 else "worker"
log = Path(os.environ["EVENTS"])
child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
with log.open("a") as f:
 f.write(json.dumps({"name": name, "pid": os.getpid(), "child": child.pid, "env": dict(os.environ), "args": sys.argv}) + "\\n")
while not Path(os.environ["RELEASE"]).exists(): time.sleep(.02)
if name == os.environ.get("FAIL_SERVICE"): sys.exit(int(os.environ.get("EXIT_CODE", "7")))
time.sleep(120)
""")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    npm = bindir / "npm"
    npm.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path}/stub.py" web\n')
    npm.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE_", "REIMAGINA_", "TEST_DATABASE_"))
    }
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        EVENTS=str(tmp_path / "events"),
        RELEASE=str(tmp_path / "release"),
    )
    return tmp_path, env


def events(path: Path) -> list[dict]:
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting for subprocess behavior")


def alive(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize(
    "stop", ["api", "web", "worker", "api-zero", "interrupt", "terminate"]
)
def test_make_dev_starts_and_reaps_all_descendants(tmp_path: Path, stop: str):
    cwd, env = sandbox(tmp_path)
    (cwd / ".env").write_text(
        "DATABASE_URL=postgresql://file:secret@localhost/file_db\nREIMAGINA_TILE_LIVE_BASE_URL=http://wrong-tiles\n"
    )
    env["DATABASE_URL"] = "postgresql://caller:caller_password_123@localhost/scratch"
    env["REIMAGINA_APP_DSN"] = env["DATABASE_URL"]
    env["FAIL_SERVICE"] = "api" if stop == "api-zero" else stop
    env["EXIT_CODE"] = "0" if stop == "api-zero" else "7"
    proc = subprocess.Popen(
        [
            "make",
            "dev",
            f"PYTHON={sys.executable}",
            f"UVICORN={sys.executable} stub.py api",
            "API_PORT=0",
            "WEB_PORT=0",
        ],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    rows = []
    try:
        wait_for(lambda: len(events(cwd / "events")) == 3)
        rows = events(cwd / "events")
        assert {r["name"] for r in rows} == {"api", "web", "worker"}
        for row in rows:
            assert row["env"]["REIMAGINA_APP_DSN"] == env["DATABASE_URL"]
            assert row["env"]["REIMAGINA_TILE_LIVE_BASE_URL"] == "/api/tiles"
        web = next(r for r in rows if r["name"] == "web")
        assert web["env"]["VITE_PORT"] == "0"
        assert web["env"]["REIMAGINA_API_URL"] == "http://127.0.0.1:0"
        if stop in {"interrupt", "terminate"}:
            os.killpg(
                proc.pid, signal.SIGINT if stop == "interrupt" else signal.SIGTERM
            )
        else:
            (cwd / "release").touch()
        output, _ = proc.communicate(timeout=8)
        assert proc.returncode != 0, output
        assert "caller_password_123" not in output and "secret" not in output
        assert "Traceback" not in output, output
        wait_for(lambda: all(not alive(r[k]) for r in rows for k in ("pid", "child")))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        for row in rows:
            for key in ("pid", "child"):
                try:
                    os.kill(row[key], signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("target", ["dev-api", "dev-worker"])
@pytest.mark.parametrize("tile_override", [None, "https://tiles.example/get_tile"])
def test_individual_dev_targets_preserve_environment(tmp_path, target, tile_override):
    cwd, env = sandbox(tmp_path)
    (cwd / ".env").write_text(
        "DATABASE_URL=postgresql://localhost/from_file\nREIMAGINA_TILE_LIVE_BASE_URL=http://wrong\n"
    )
    env["DATABASE_URL"] = "postgresql://localhost/from_caller"
    if tile_override:
        env["REIMAGINA_TILE_LIVE_BASE_URL"] = tile_override
    proc = subprocess.Popen(
        [
            "make",
            target,
            f"PYTHON={sys.executable}",
            f"UVICORN={sys.executable} stub.py api",
            "API_PORT=0",
        ],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    rows = []
    try:
        wait_for(lambda: len(events(cwd / "events")) == 1)
        rows = events(cwd / "events")
        assert rows[0]["env"]["REIMAGINA_APP_DSN"] == env["DATABASE_URL"]
        assert rows[0]["env"]["REIMAGINA_TILE_LIVE_BASE_URL"] == (
            tile_override or "/api/tiles"
        )
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)
        for row in rows:
            for key in ("pid", "child"):
                try:
                    os.kill(row[key], signal.SIGKILL)
                except ProcessLookupError:
                    pass

"""Supervise API, web and worker on macOS/Linux, including their descendants."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from runtime_env import load_environment, masked_dsn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--uvicorn", default="uv run uvicorn")
    parser.add_argument("--api-port", default="8001")
    parser.add_argument("--web-port", default="5173")
    args = parser.parse_args()
    try:
        load_environment("dev")
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"DSN  → {masked_dsn(os.environ['REIMAGINA_APP_DSN'])}", flush=True)
    print(f"Tiles→ {os.environ['REIMAGINA_TILE_LIVE_BASE_URL']}", flush=True)
    print(f"API  → http://127.0.0.1:{args.api_port}", flush=True)
    print(f"Web  → http://localhost:{args.web_port}", flush=True)
    commands = [
        (
            "API",
            shlex.split(args.uvicorn)
            + [
                "apps.api.app:app",
                "--reload",
                "--host",
                "127.0.0.1",
                "--port",
                args.api_port,
            ],
            None,
        ),
        ("Web", ["npm", "run", "dev"], Path("apps/web")),
        ("Worker", shlex.split(args.python) + ["-m", "apps.worker"], None),
    ]
    stopped = 0

    def stop(signum, _frame):
        nonlocal stopped
        stopped = signum

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, command, cwd in commands:
            env = dict(os.environ)
            if name == "Web":
                env.update(
                    VITE_PORT=args.web_port,
                    REIMAGINA_API_URL=f"http://127.0.0.1:{args.api_port}",
                )
            processes.append(
                (
                    name,
                    subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True),
                )
            )
        while not stopped:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    print(
                        f"{name} exited ({code}); stopping dev services.",
                        file=sys.stderr,
                    )
                    return code if code > 0 else 1
            time.sleep(0.05)
        return 128 + stopped
    except OSError as exc:
        print(f"Cannot start dev service: {exc}", file=sys.stderr)
        return 1
    finally:
        # Signal groups even when their launcher exited: grandchildren may remain.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for _, process in processes:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin reports EPERM for a group whose last members
                    # already exited; Linux reports ESRCH instead.
                    if sys.platform != "darwin":
                        raise
            if sig == signal.SIGTERM:
                time.sleep(0.5)
        for _, process in processes:
            process.wait()


if __name__ == "__main__":
    raise SystemExit(main())

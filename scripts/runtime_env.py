"""Load dotenv as data, preserve caller exports, and validate destructive targets."""

from __future__ import annotations

import os
import re
import shlex
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv
from psycopg2.extensions import make_dsn, parse_dsn


def masked_dsn(value: str) -> str:
    """Show a libpq target without credentials or arbitrary query parameters."""
    try:
        params = parse_dsn(value)
    except Exception:
        return "<invalid DSN>"
    return make_dsn(
        **{
            key: val
            for key, val in params.items()
            if key in {"host", "hostaddr", "port", "dbname", "user"}
        }
    )


def load_environment(mode: str) -> None:
    tiles = os.environ.get("REIMAGINA_TILE_LIVE_BASE_URL")
    load_dotenv(".env", override=False)
    if mode == "dev":
        os.environ["REIMAGINA_TILE_LIVE_BASE_URL"] = tiles or "/api/tiles"
        os.environ.setdefault("REIMAGINA_APP_DSN", os.environ.get("DATABASE_URL", ""))
        if not os.environ["REIMAGINA_APP_DSN"]:
            raise ValueError("REIMAGINA_APP_DSN is unset and .env has no DATABASE_URL.")
    else:
        validate_targets()


def _params(key: str) -> dict[str, str]:
    try:
        value = os.environ[key]
        params = parse_dsn(value)
        if not params.get("dbname") or not params.get("host"):
            raise ValueError("explicit host and database required")
        # Multiple-host failover can select a different server at connect time.
        if "," in params["host"] or params.get("service") or params.get("hostaddr"):
            raise ValueError("single explicit host required")
        # libpq fills an omitted/empty DSN port from PGPORT before 5432.
        port = int(params.get("port") or os.environ.get("PGPORT") or "5432")
        if not 1 <= port <= 65535:
            raise ValueError("invalid PostgreSQL port")
        params["port"] = str(port)
        return params
    except Exception:
        raise ValueError(
            f"{key} must identify one explicit PostgreSQL host and database."
        ) from None


def _host(params: dict[str, str]) -> str:
    host = params["host"].lower()
    return "loopback" if host in {"localhost", "127.0.0.1", "::1"} else host


def validate_targets() -> None:
    for key in ("PGSERVICE", "PGHOSTADDR"):
        if os.environ.get(key):
            raise ValueError(
                f"Unset {key} for destructive harnesses; use explicit DSN endpoints."
            )
    os.environ.setdefault("DATABASE_URL_DIRECT", os.environ.get("DATABASE_URL", ""))
    # Alembic/SQLAlchemy migrations need URLs even though libpq accepts
    # keyword DSNs. Reject unsupported migration inputs before DROP.
    for key in ("DATABASE_URL", "DATABASE_URL_DIRECT"):
        try:
            scheme = urlsplit(os.environ.get(key, "")).scheme
        except ValueError:
            scheme = ""
        # Alembic uses SQLAlchemy, whose dialect is postgresql, not the
        # libpq-only postgres alias. Reject unsupported migration inputs
        # before dropping the database they currently connect to.
        if scheme != "postgresql":
            raise ValueError(f"{key} must use a postgresql:// URL.")
    app = _params("DATABASE_URL")
    direct = _params("DATABASE_URL_DIRECT")
    if (
        app["dbname"] != direct["dbname"]
        or _host(app) != _host(direct)
        or app.get("port", "5432") != direct.get("port", "5432")
    ):
        raise ValueError(
            "DATABASE_URL and DATABASE_URL_DIRECT must name the same direct host, port and database."
        )
    if "DATABASE_URL_ADMIN" not in os.environ:
        admin_params = dict(direct, dbname="postgres")
        os.environ["DATABASE_URL_ADMIN"] = make_dsn(**admin_params)
    admin = _params("DATABASE_URL_ADMIN")
    if (_host(admin), admin.get("port", "5432")) != (
        _host(direct),
        direct.get("port", "5432"),
    ):
        raise ValueError(
            "DATABASE_URL_ADMIN must use the DATABASE_URL_DIRECT server and port."
        )
    if admin["dbname"] == app["dbname"]:
        raise ValueError(
            "DATABASE_URL_ADMIN must use a maintenance database distinct from DATABASE_URL."
        )
    os.environ["DB_NAME"] = app["dbname"]


def drop_database() -> None:
    """Validate again before terminating sessions; quote the database as an identifier."""
    import psycopg2
    from psycopg2 import sql

    validate_targets()
    name = os.environ["DB_NAME"]
    conn = psycopg2.connect(os.environ["DATABASE_URL_ADMIN"])
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )
    finally:
        conn.close()
    print(f"  dropped {name}")


def main() -> int:
    try:
        mode = sys.argv[1]
        if mode == "drop":
            drop_database()
            return 0
        load_environment(mode)
        for key, value in os.environ.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                print(f"export {key}={shlex.quote(value)}")
        dsn = (
            os.environ.get("REIMAGINA_APP_DSN")
            if mode == "dev"
            else os.environ["DATABASE_URL"]
        )
        print(f"DSN  → {masked_dsn(dsn or '')}", file=sys.stderr)
        if mode == "dev":
            print(
                f"Tiles→ {os.environ['REIMAGINA_TILE_LIVE_BASE_URL']}", file=sys.stderr
            )
        return 0
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

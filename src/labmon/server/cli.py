"""``labmon-server`` command line.

labmon-server migrate                  # alembic upgrade head
labmon-server serve --host 127.0.0.1 --port 8000
labmon-server bootstrap-dev --out .devdata/collector --server-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from labmon.server import migrate
from labmon.server.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labmon-server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="upgrade the schema to the current head")
    serve = sub.add_parser("serve", help="run the API with uvicorn")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    boot = sub.add_parser("bootstrap-dev", help="development-only demo data and collector config")
    boot.add_argument("--out", type=Path, default=Path(".devdata/collector"))
    boot.add_argument("--server-url", default="http://127.0.0.1:8000")
    boot.add_argument("--password", default=None, help="scientist password (>= 12 chars)")
    boot.add_argument("--with-ambient", action="store_true", help="also seed the smoke fixture")
    boot.add_argument("--rotate-token", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "migrate":
        migrate.upgrade(settings.database_url)
        print(f"schema at {migrate.head_revision()}")
        return 0
    if args.command == "serve":
        import uvicorn

        from labmon.server.app import create_app

        uvicorn.run(
            create_app(settings),
            host=args.host,
            port=args.port,
            proxy_headers=False,
            server_header=False,
            log_level="info",
        )
        return 0
    if args.command == "bootstrap-dev":
        if not settings.dev_tools_allowed:
            print(
                "bootstrap-dev refuses to run unless LABMON_ENV is development or test",
                file=sys.stderr,
            )
            return 2
        from labmon.server.bootstrap import bootstrap_dev
        from labmon.server.db import make_engine

        result = bootstrap_dev(
            make_engine(settings.database_url),
            args.out,
            args.server_url,
            args.password,
            args.with_ambient,
            args.rotate_token,
        )
        print(f"collector config: {result['collector_config']}")
        print(f"sign in as: {result['username']} / {result['password']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

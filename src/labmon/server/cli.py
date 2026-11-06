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
    worker = sub.add_parser(
        "worker", aliases=["rollup-worker"], help="rollups and durable exports (separate process)"
    )
    worker.add_argument("--once", action="store_true", help="drain the queue once and exit")
    worker.add_argument("--interval", type=float, default=2.0)
    fixture = sub.add_parser("load-fixture", help="development/test only: load a long fixture")
    fixture.add_argument("key", help="fixture key, e.g. furnace-30d")
    fixture.add_argument("--channels", default=None, help="comma-separated channel keys")
    boot = sub.add_parser("bootstrap-dev", help="development-only demo data and collector config")
    boot.add_argument("--out", type=Path, default=Path(".devdata/collector"))
    boot.add_argument("--server-url", default="http://127.0.0.1:8000")
    boot.add_argument("--password", default=None, help="scientist password (>= 12 chars)")
    boot.add_argument("--with-dsc", action="store_true", help="add the opt-in DSC profile")
    boot.add_argument("--with-ambient", action="store_true", help="also seed the smoke fixture")
    boot.add_argument("--rotate-token", action="store_true")
    admin = sub.add_parser("admin", help="operator administration (audited)")
    adm = admin.add_subparsers(dest="admin_command", required=True)
    p = adm.add_parser("lab-add")
    p.add_argument("lab_id")
    p.add_argument("name")
    p = adm.add_parser("user-add")
    p.add_argument("username")
    p.add_argument("--display-name", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p = adm.add_parser("user-password")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    p = adm.add_parser("user-disable")
    p.add_argument("username")
    adm.add_parser("user-list")
    p = adm.add_parser("grant")
    p.add_argument("username")
    p.add_argument("lab_id")
    p.add_argument("role")
    p = adm.add_parser("grant-revoke")
    p.add_argument("username")
    p.add_argument("lab_id")
    p = adm.add_parser("collector-add")
    p.add_argument("collector_id")
    p.add_argument("--lab", required=True)
    p.add_argument("--name", required=True)
    p = adm.add_parser("collector-token")
    p.add_argument("collector_id")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--keep-existing", action="store_true")
    p = adm.add_parser("collector-revoke")
    p.add_argument("collector_id")
    p = adm.add_parser("collector-config")
    p.add_argument("collector_id")
    p.add_argument("--server-url", required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p = adm.add_parser("instrument-enrol", help="enrol a simulated instrument")
    p.add_argument("instrument_id")
    p.add_argument("--lab", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--collector", required=True)
    p.add_argument("--location", default=None)
    p.add_argument("--asset-id", default=None)
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "admin":
        return run_admin(args, settings)

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
    if args.command == "load-fixture":
        if not settings.dev_tools_allowed:
            print(
                "load-fixture refuses to run unless LABMON_ENV is development or test",
                file=sys.stderr,
            )
            return 2
        from labmon.server.db import make_engine
        from labmon.server.fixture_loader import load_fixture

        chans = tuple(args.channels.split(",")) if args.channels else None
        print(load_fixture(make_engine(settings.database_url, pool_size=2), args.key, chans))
        return 0
    if args.command in ("worker", "rollup-worker"):
        from labmon.server.db import make_engine
        from labmon.server.worker import run_rollup_worker

        return run_rollup_worker(
            make_engine(settings.database_url, pool_size=2), args.once, args.interval, settings
        )
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
            with_dsc=args.with_dsc,
        )
        print(f"collector config: {result['collector_config']}")
        print(f"sign in as: {result['username']} / {result['password']}")
        return 0
    return 1


def run_admin(args: argparse.Namespace, settings: Settings) -> int:
    import json

    from labmon.server import admin
    from labmon.server.db import make_engine

    engine = make_engine(settings.database_url, pool_size=1)
    cmd = args.admin_command
    try:
        if cmd == "lab-add":
            admin.add_lab(engine, args.lab_id, args.name)
        elif cmd == "user-add":
            admin.add_user(
                engine, args.username, args.display_name, admin.read_password(args.password_stdin)
            )
        elif cmd == "user-password":
            admin.set_password(engine, args.username, admin.read_password(args.password_stdin))
        elif cmd == "user-disable":
            admin.disable_user(engine, args.username)
        elif cmd == "user-list":
            print(json.dumps(admin.list_users(engine), indent=2))
        elif cmd == "grant":
            admin.grant(engine, args.username, args.lab_id, args.role)
        elif cmd == "grant-revoke":
            admin.revoke_grant(engine, args.username, args.lab_id)
        elif cmd == "collector-add":
            admin.add_collector(engine, args.collector_id, args.lab, args.name)
        elif cmd == "collector-token":
            admin.issue_token(engine, args.collector_id, args.out, args.keep_existing)
            print(f"credential written to {args.out} (owner-only); restart the collector")
        elif cmd == "collector-revoke":
            admin.revoke_collector(engine, args.collector_id)
        elif cmd == "collector-config":
            print(admin.write_config(engine, args.collector_id, args.server_url, args.out_dir))
        elif cmd == "instrument-enrol":
            print(
                admin.enrol_simulated(
                    engine,
                    args.instrument_id,
                    args.lab,
                    args.name,
                    args.profile,
                    args.seed,
                    args.collector,
                    args.location,
                    args.asset_id,
                )
            )
    except (admin.AdminError, ValueError) as exc:
        print(f"labmon-server admin {cmd}: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

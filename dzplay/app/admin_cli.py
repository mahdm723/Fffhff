"""Admin command line.

  python -m app.admin_cli stats
  python -m app.admin_cli reports [--status open]
  python -m app.admin_cli resolve <report_id> dismiss|warn|suspend|ban
  python -m app.admin_cli set-status <user_ref> active|suspended|banned
  python -m app.admin_cli events [--type login_failed]
  python -m app.admin_cli cleanup
  python -m app.admin_cli gen-secret
  python -m app.admin_cli gen-vapid
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets

from app.config import get_settings
from app.db import Database
from app.services import admin
from app.services.cleanup import run_cleanup


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _gen_vapid() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    raw_private = key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    print(f"VAPID_PUBLIC_KEY={b64(raw_public)}")
    print(f"VAPID_PRIVATE_KEY={b64(raw_private)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dzplay-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    p = sub.add_parser("reports")
    p.add_argument("--status", default="open")
    p = sub.add_parser("resolve")
    p.add_argument("report_id")
    p.add_argument("action", choices=["dismiss", "warn", "remove", "suspend", "ban"])
    p = sub.add_parser("set-status")
    p.add_argument("user_ref")
    p.add_argument("status", choices=["active", "suspended", "banned"])
    p = sub.add_parser("events")
    p.add_argument("--type", default=None)
    sub.add_parser("cleanup")
    sub.add_parser("gen-secret")
    sub.add_parser("gen-vapid")
    args = parser.parse_args(argv)

    if args.cmd == "gen-secret":
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        return
    if args.cmd == "gen-vapid":
        _gen_vapid()
        return

    settings = get_settings()
    database = Database(settings.DATABASE_URL)
    database.create_all()
    with database.session() as db:
        if args.cmd == "stats":
            _print(admin.stats(db))
        elif args.cmd == "reports":
            _print(admin.list_reports(db, args.status))
        elif args.cmd == "resolve":
            _print(admin.resolve_report(db, args.report_id, args.action))
        elif args.cmd == "set-status":
            _print(admin.set_user_status(db, args.user_ref, args.status))
        elif args.cmd == "events":
            _print(admin.security_events(db, args.type))
        elif args.cmd == "cleanup":
            _print(run_cleanup(db, settings))


if __name__ == "__main__":
    main()

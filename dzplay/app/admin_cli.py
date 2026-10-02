"""Admin command line.

  python -m app.admin_cli stats
  python -m app.admin_cli reports [--status open]
  python -m app.admin_cli resolve <report_id> dismiss|warn|suspend|ban
  python -m app.admin_cli set-status <user_ref> active|suspended|banned
  python -m app.admin_cli events [--type login_failed]
  python -m app.admin_cli cleanup
  python -m app.admin_cli create-admin <username>      (asks for a password, prints the 2FA QR code)
  python -m app.admin_cli reset-admin-2fa <username>
  python -m app.admin_cli set-admin-password <username>
  python -m app.admin_cli audit [--limit 50]
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


def _show_totp(settings, username: str, secret: str) -> None:
    from app.security.totp import provisioning_uri

    uri = provisioning_uri(secret, f"{username}@{settings.APP_NAME}")
    print("\nScan this QR code with Google Authenticator / Microsoft Authenticator / Aegis / 2FAS:\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print(f"Or enter this key manually: {secret}\n")
    if settings.ADMIN_PATH:
        print(f"Panel path: {settings.ADMIN_PATH}\n")


def _ask_password() -> str:
    import getpass

    first = getpass.getpass("New admin password (12+ characters): ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


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
    for name in ("create-admin", "reset-admin-2fa", "set-admin-password"):
        sp = sub.add_parser(name)
        sp.add_argument("username")
        if name == "create-admin":
            sp.add_argument("--role", default="super_admin")
    p = sub.add_parser("audit")
    p.add_argument("--limit", type=int, default=50)
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
    if args.cmd in ("create-admin", "reset-admin-2fa", "set-admin-password"):
        from app.services import admin_auth, audit

        password = _ask_password() if args.cmd != "reset-admin-2fa" else None
        with database.session() as db:
            try:
                if args.cmd == "create-admin":
                    _admin, secret = admin_auth.create_admin(db, settings, args.username, password, args.role)
                elif args.cmd == "reset-admin-2fa":
                    secret = admin_auth.reset_totp(db, settings, args.username)
                else:
                    admin_auth.set_password(db, args.username, password)
                    secret = None
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            audit.record(db, "cli", args.cmd.replace("-", "_"), target_type="admin", target_id=args.username.lower())
        if secret:
            _show_totp(settings, args.username.lower(), secret)
        print("Done.")
        return

    with database.session() as db:
        if args.cmd == "audit":
            from app.services import audit

            _print({"chain": audit.verify_chain(db), "entries": audit.list_entries(db, args.limit)})
            return
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

"""Operator commands that are deliberately not exposed over HTTP.

The one job here is claiming data that predates accounts. A legacy device or tariff has
`owner_id = NULL`; it is preserved but invisible to every account. Attaching it to a user is
a decision only an operator can make, so it happens here, explicitly, by id or by name.

Nothing is deleted, and a row that already belongs to an account is never reassigned.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from backend.app.database import SessionLocal, init_db
from backend.app.models import Device, Tariff, User


def _resolve_user(db, email: str) -> User | None:
    return db.execute(select(User).where(User.email == email.strip().lower())).scalars().first()


def _legacy_devices(db) -> list[Device]:
    return list(db.execute(select(Device).where(Device.owner_id.is_(None)).order_by(Device.id)).scalars())


def _legacy_tariffs(db) -> list[Tariff]:
    return list(db.execute(select(Tariff).where(Tariff.owner_id.is_(None)).order_by(Tariff.id)).scalars())


def command_claim_legacy(args: argparse.Namespace) -> int:
    init_db()
    db = SessionLocal()
    try:
        user = _resolve_user(db, args.email)
        if user is None:
            print(f"No account with email {args.email}. Register it in the dashboard first.")
            return 1

        devices = _legacy_devices(db)
        if args.all:
            selected = devices
        elif args.device:
            by_id = {device.id: device for device in devices}
            selected = []
            for device_id in args.device:
                device = by_id.get(device_id)
                if device is None:
                    existing = db.get(Device, device_id)
                    if existing is None:
                        print(f"  ! {device_id}: no such device")
                    else:
                        print(f"  ! {device_id}: already owned by user {existing.owner_id}; not reassigned")
                    continue
                selected.append(device)
        else:
            selected = []

        tariffs = _legacy_tariffs(db)
        selected_tariffs = tariffs if args.tariff else []

        if not selected and not selected_tariffs:
            print("Nothing selected. Legacy rows available:")
            for device in devices:
                print(f"  device  {device.id}  ({device.name or 'unnamed'})")
            for tariff in tariffs:
                print(f"  tariff  #{tariff.id}  {tariff.name}")
            if not devices and not tariffs:
                print("  (none - everything is already owned)")
            print("Pass --device <id> ... or --all, and optionally --tariff.")
            return 1

        for device in selected:
            device.owner_id = user.id
        for tariff in selected_tariffs:
            tariff.owner_id = user.id

        if args.dry_run:
            db.rollback()
            print("Dry run: nothing was written.")
        else:
            db.commit()

        print(f"{'Would claim' if args.dry_run else 'Claimed'} for {user.email}:")
        for device in selected:
            print(f"  device  {device.id}  ({device.name or 'unnamed'})")
        for tariff in selected_tariffs:
            print(f"  tariff  #{tariff.id}  {tariff.name}")
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wattxpense-admin",
        description="WattXpense operator commands.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser(
        "claim-legacy",
        help="Attach pre-account devices/tariffs to an existing account. Never reassigns.",
    )
    claim.add_argument("--email", required=True, help="Account that will own the claimed rows.")
    claim.add_argument("--device", action="append", default=[], help="Device id to claim. Repeatable.")
    claim.add_argument("--all", action="store_true", help="Claim every unowned device.")
    claim.add_argument("--tariff", action="store_true", help="Also claim every unowned tariff.")
    claim.add_argument("--dry-run", action="store_true", help="Show what would change without writing.")
    claim.set_defaults(func=command_claim_legacy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

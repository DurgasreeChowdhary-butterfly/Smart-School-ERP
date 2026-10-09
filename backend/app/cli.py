"""Administrative command line.

    python -m app.cli create-school --name "Green Hills School" --code GHS \\
        --timezone Asia/Kolkata
    python -m app.cli create-user --email admin@school.example --full-name "Admin" \\
        --role SUPER_ADMIN

The password is read interactively (never from argv, which leaks into shell
history and process lists) or, for automation, from the environment variable
named by ``--password-env``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Sequence

from pydantic import ValidationError

from app.config import get_settings
from app.core.errors import AppError
from app.database import Database
from app.schemas.school import SchoolCreate
from app.security.permissions import RoleName
from app.services.school import SchoolService
from app.services.users import create_user_account


def _read_password(env_var: str | None) -> str | None:
    if env_var:
        value = os.environ.get(env_var)
        if not value:
            raise SystemExit(f"Environment variable {env_var} is not set.")
        return value
    if not sys.stdin.isatty():
        raise SystemExit("No TTY: use --password-env to supply the password.")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


def _create_user(args: argparse.Namespace) -> int:
    settings = get_settings()
    password = None if args.no_password else _read_password(args.password_env)
    database = Database(settings)
    try:
        with database.session() as session:
            user = create_user_account(
                session,
                settings,
                full_name=args.full_name,
                email=args.email,
                phone=args.phone,
                password=password,
                roles=[RoleName(role) for role in args.role],
                school_id=uuid.UUID(args.school_id) if args.school_id else None,
            )
            session.commit()
            sys.stdout.write(f"Created user {user.id}\n")
    except AppError as exc:
        sys.stderr.write(f"Error: {exc.message}\n")
        for detail in exc.details:
            sys.stderr.write(f"  - {detail.message}\n")
        return 1
    finally:
        database.dispose()
    return 0


def _create_school(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = Database(settings)
    try:
        data = SchoolCreate(
            name=args.name,
            code=args.code,
            timezone=args.timezone,
            email=args.email,
            phone=args.phone,
            city=args.city,
            country=args.country,
        )
        with database.session() as session:
            school = SchoolService(session).create(data, actor_id=None, source="cli")
            session.commit()
            sys.stdout.write(f"Created school {school.id} ({school.code})\n")
    except ValidationError as exc:
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            sys.stderr.write(f"Error: {field}: {error['msg']}\n")
        return 1
    except AppError as exc:
        sys.stderr.write(f"Error: {exc.message}\n")
        return 1
    finally:
        database.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: otherwise "--password <pw>" would silently match "--password-env".
    parser = argparse.ArgumentParser(prog="python -m app.cli", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create-user", help="Create a user account with roles.", allow_abbrev=False
    )
    create.add_argument("--full-name", required=True)
    create.add_argument("--email")
    create.add_argument("--phone", help="International format, e.g. +919876543210.")
    create.add_argument(
        "--role", action="append", required=True, choices=[role.value for role in RoleName]
    )
    create.add_argument("--school-id", help="UUID of the school (required unless SUPER_ADMIN).")
    create.add_argument("--password-env", help="Read the password from this environment variable.")
    create.add_argument(
        "--no-password", action="store_true", help="Create an OTP-only account (no password)."
    )
    create.set_defaults(handler=_create_user)

    school = commands.add_parser(
        "create-school", help="Create a school (platform operator).", allow_abbrev=False
    )
    school.add_argument("--name", required=True)
    school.add_argument("--code", required=True, help="Unique, immutable short code.")
    school.add_argument("--timezone", default="UTC", help="IANA name, e.g. Asia/Kolkata.")
    school.add_argument("--email")
    school.add_argument("--phone")
    school.add_argument("--city")
    school.add_argument("--country")
    school.set_defaults(handler=_create_school)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

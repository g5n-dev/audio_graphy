"""Create the first tenant and administrator account.

A fresh database has no users and there is no self-service registration, so
without this there is no way to log in. Running it twice is safe: an existing
tenant or user is reported and left untouched, and ``--reset-password`` is the
only way to change an existing account.

The password is never printed. Supply it via ``--password``, the
``BOOTSTRAP_ADMIN_PASSWORD`` environment variable, or an interactive prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Make `audio_graphy` importable when run as a plain script (python scripts/...).
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audio_graphy.auth.passwords import PasswordHasher  # noqa: E402
from audio_graphy.config import get_settings  # noqa: E402
from audio_graphy.models.enums import UserRole  # noqa: E402
from audio_graphy.models.tenant import Tenant  # noqa: E402
from audio_graphy.models.user import User  # noqa: E402

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12


def resolve_password(explicit: str | None, *, allow_prompt: bool) -> str:
    """Resolve the admin password from argv, the environment, or a prompt.

    Args:
        explicit: Value passed via ``--password``, if any.
        allow_prompt: Whether an interactive prompt is acceptable.

    Returns:
        The plaintext password.

    Raises:
        RuntimeError: No source supplied one, or it is too short.
    """
    password = explicit or os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or ""
    if not password and allow_prompt and sys.stdin.isatty():
        password = getpass.getpass("Admin password: ")
        if password != getpass.getpass("Repeat password: "):
            raise RuntimeError("passwords did not match")
    if not password:
        raise RuntimeError(
            "no password supplied — pass --password, set BOOTSTRAP_ADMIN_PASSWORD, "
            "or run interactively"
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


async def bootstrap_admin(
    *,
    tenant_code: str,
    tenant_name: str,
    email: str,
    name: str,
    password: str,
    reset_password: bool,
) -> None:
    """Ensure a tenant and an administrator exist.

    Args:
        tenant_code: Tenant code; also the ``tenant_id`` every scoped row carries.
        tenant_name: Human-readable tenant name.
        email: Administrator email, unique within the tenant.
        name: Administrator display name.
        password: Plaintext password, hashed with bcrypt before it is stored.
        reset_password: Overwrite the password when the user already exists.

    Raises:
        RuntimeError: The database is unreachable or has not been migrated.
    """
    settings = get_settings()
    engine = create_async_engine(settings.mysql_dsn_async, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    hasher = PasswordHasher(bcrypt_rounds=settings.bcrypt_rounds)

    try:
        async with session_factory() as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.code == tenant_code))
            ).scalar_one_or_none()
            if tenant is None:
                session.add(Tenant(code=tenant_code, name=tenant_name))
                logger.info("created tenant %s", tenant_code)
            else:
                logger.info("tenant %s already exists — left unchanged", tenant_code)

            user = (
                await session.execute(
                    select(User).where(
                        User.tenant_id == tenant_code,
                        User.email == email,
                    )
                )
            ).scalar_one_or_none()

            if user is None:
                session.add(
                    User(
                        tenant_id=tenant_code,
                        name=name,
                        email=email,
                        role=UserRole.ADMIN.value,
                        # bcrypt is intentionally slow; keep it off the event loop.
                        password_hash=await asyncio.to_thread(hasher.hash, password),
                    )
                )
                logger.info("created administrator %s in tenant %s", email, tenant_code)
            elif reset_password:
                user.password_hash = await asyncio.to_thread(hasher.hash, password)
                logger.info("reset password for %s", email)
            else:
                logger.info(
                    "user %s already exists — left unchanged (pass --reset-password to overwrite)",
                    email,
                )

            await session.commit()
    except Exception as exc:
        # Re-raised verbatim: the operator needs the driver's own message to tell
        # "wrong host" from "database not migrated".
        raise RuntimeError(
            f"bootstrap failed: {exc}. Is the database reachable and migrated "
            f"(alembic upgrade head)?"
        ) from exc
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Create the first tenant and administrator account.",
    )
    parser.add_argument("--tenant-code", default="default", help="tenant code (default: default)")
    parser.add_argument("--tenant-name", default=None, help="tenant display name")
    parser.add_argument("--email", required=True, help="administrator email")
    parser.add_argument("--name", default=None, help="administrator display name")
    parser.add_argument(
        "--password",
        default=None,
        help="administrator password; prefer BOOTSTRAP_ADMIN_PASSWORD or the prompt",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="overwrite the password when the account already exists",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="fail instead of prompting when no password was supplied",
    )
    args = parser.parse_args()

    # Compose passes the value through unset-tolerant interpolation, so an
    # operator who forgets BOOTSTRAP_ADMIN_EMAIL reaches here with "".
    email = args.email.strip()
    if not email:
        parser.error("--email must not be empty")

    password = resolve_password(args.password, allow_prompt=not args.no_prompt)

    asyncio.run(
        bootstrap_admin(
            tenant_code=args.tenant_code,
            tenant_name=args.tenant_name or args.tenant_code,
            email=email,
            name=args.name or email.split("@", 1)[0],
            password=password,
            reset_password=args.reset_password,
        )
    )
    logger.info("bootstrap complete — sign in at the web UI with %s", email)


if __name__ == "__main__":
    main()

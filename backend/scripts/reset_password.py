"""Terminal recovery: set a new password for an EXISTING account.

For the case where a company's only owner forgets their password (there is
no email-based reset). Requires shell access to the server, which is the
trust boundary. Run from backend/ after 'alembic upgrade head':

    python scripts/reset_password.py --username owner

The new password is typed at a hidden prompt (never a CLI argument). All of
that person's sessions are ended and the reset is recorded in the company's
account history as a terminal action. This never creates, deletes or moves
accounts, and never changes roles. Use --reactivate only to restore an owner
who was deactivated by mistake.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import password_hash, revoke_sessions  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AccountEvent, User  # noqa: E402


def main(argv: list[str] | None = None, *, prompt=getpass.getpass, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description="Reset an existing TicketFlow account password")
    parser.add_argument("--username", required=True)
    parser.add_argument("--reactivate", action="store_true",
                        help="Also reactivate the account if it was deactivated")
    args = parser.parse_args(argv)
    username = args.username.strip().casefold()
    with session_factory() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            print("No account with that username.", file=sys.stderr)
            return 1
        first = prompt(f"New password for {user.username} (12+ characters): ")
        second = prompt("Confirm new password: ")
        if first != second:
            print("Passwords did not match. Nothing changed.", file=sys.stderr)
            return 1
        try:
            user.password_hash = password_hash(first)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        user.must_change_password = False
        revoke_sessions(db, user.id)
        db.add(AccountEvent(organization_id=user.organization_id, actor_user_id=None,
                            target_user_id=user.id, action="password_reset_terminal"))
        if args.reactivate and not user.active:
            user.active = True
            db.add(AccountEvent(organization_id=user.organization_id, actor_user_id=None,
                                target_user_id=user.id, action="reactivated_terminal"))
        db.commit()
    print("Password updated. All of this account's previous sessions were signed out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

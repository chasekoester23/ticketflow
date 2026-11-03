"""Company team administration: one shared rule set for API and terminal scripts.

Safeguards:
- Every company always keeps at least one active owner.
- An owner cannot deactivate or demote their own account from the browser
  (prevents accidental self-lockout; another owner must do it).
- Deactivation, role change and password reset end the person's sessions
  immediately, so access changes take effect on the very next request.
- Every change is recorded as an immutable AccountEvent with the actor.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    generate_temporary_password, password_hash, password_valid,
    revoke_sessions, validate_new_password,
)
from ..models import AccountEvent, User
from ..permissions import OWNER, ROLES


class AccountError(Exception):
    """A rejected team change. ``status`` maps to the HTTP status code."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def person_name(user: User | None) -> str | None:
    if user is None:
        return None
    return user.display_name or user.username


def names_by_id(db: Session, organization_id: int) -> dict[int, str]:
    """Display names for one company's people, for attributing audit rows."""
    rows = db.execute(
        select(User.id, User.display_name, User.username)
        .where(User.organization_id == organization_id)
    ).all()
    return {uid: (display or username) for uid, display, username in rows}


def _clean_display_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    if len(value) > 100:
        raise AccountError("Display name must be 100 characters or fewer.", 422)
    return value or None


def _record(db: Session, *, organization_id: int, actor: User | None, target: User,
            action: str, old: str | None = None, new: str | None = None) -> None:
    db.add(AccountEvent(
        organization_id=organization_id,
        actor_user_id=actor.id if actor is not None else None,
        target_user_id=target.id, action=action, old_value=old, new_value=new,
    ))


def _lock_active_owners(db: Session, organization_id: int) -> list[int]:
    """Row-lock the company's active owners (PostgreSQL) so two concurrent
    demotions cannot both pass the last-owner check. No-op on SQLite, whose
    writes are already serialized."""
    return list(db.scalars(
        select(User.id).where(
            User.organization_id == organization_id,
            User.role == OWNER, User.active.is_(True),
        ).with_for_update()
    ))


def _ensure_another_owner_remains(db: Session, target: User) -> None:
    if target.role != OWNER or not target.active:
        return
    owners = _lock_active_owners(db, target.organization_id)
    if not [uid for uid in owners if uid != target.id]:
        raise AccountError("A company must keep at least one active owner. Add another owner first.")


def create_member(db: Session, *, actor: User, username: str, role: str,
                  display_name: str | None) -> tuple[User, str]:
    """Create an employee with a one-time temporary password. Caller commits."""
    if role not in ROLES:
        raise AccountError("Unknown role.", 422)
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        # Usernames are global sign-in names; do not reveal which company has it.
        raise AccountError("That username is not available. Choose another.")
    temporary = generate_temporary_password()
    user = User(
        organization_id=actor.organization_id, username=username, role=role,
        display_name=_clean_display_name(display_name),
        password_hash=password_hash(temporary), must_change_password=True,
        active=True,
    )
    db.add(user)
    db.flush()
    _record(db, organization_id=actor.organization_id, actor=actor, target=user,
            action="member_added", new=role)
    return user, temporary


def update_member(db: Session, *, actor: User, target: User, role: str | None = None,
                  active: bool | None = None, display_name: str | None = None,
                  display_name_set: bool = False) -> None:
    """Apply role/active/display-name changes. Caller commits."""
    org = actor.organization_id
    revoke = False
    if role is not None and role != target.role:
        if role not in ROLES:
            raise AccountError("Unknown role.", 422)
        if target.id == actor.id:
            raise AccountError("You cannot change your own role. Ask another owner.")
        if target.role == OWNER:
            _ensure_another_owner_remains(db, target)
        _record(db, organization_id=org, actor=actor, target=target,
                action="role_changed", old=target.role, new=role)
        target.role = role
        revoke = True
    if active is not None and active != target.active:
        if target.id == actor.id:
            raise AccountError("You cannot deactivate your own account.")
        if not active:
            _ensure_another_owner_remains(db, target)
        _record(db, organization_id=org, actor=actor, target=target,
                action="reactivated" if active else "deactivated")
        target.active = active
        revoke = True
    if display_name_set:
        cleaned = _clean_display_name(display_name)
        if cleaned != target.display_name:
            _record(db, organization_id=org, actor=actor, target=target,
                    action="display_name_changed", old=target.display_name, new=cleaned)
            target.display_name = cleaned
    if revoke:
        revoke_sessions(db, target.id)


def reset_member_password(db: Session, *, actor: User | None, target: User) -> str:
    """Issue a new temporary password and end the person's sessions. Caller commits."""
    temporary = generate_temporary_password()
    target.password_hash = password_hash(temporary)
    target.must_change_password = True
    revoke_sessions(db, target.id)
    _record(db, organization_id=target.organization_id, actor=actor, target=target,
            action="password_reset")
    return temporary


def change_own_password(db: Session, *, user: User, current_password: str,
                        new_password: str, keep_session_digest: str | None) -> None:
    """Self-service change. Ends the person's OTHER sessions. Caller commits."""
    if not password_valid(current_password, user.password_hash):
        raise AccountError("Current password is incorrect.", 400)
    if new_password == current_password:
        raise AccountError("Choose a password different from the current one.", 422)
    if new_password.strip().casefold() == user.username:
        raise AccountError("Password cannot be your username.", 422)
    try:
        validate_new_password(new_password)
    except ValueError as exc:
        raise AccountError(str(exc), 422) from exc
    user.password_hash = password_hash(new_password)
    user.must_change_password = False
    revoke_sessions(db, user.id, keep_token_digest=keep_session_digest)
    _record(db, organization_id=user.organization_id, actor=user, target=user,
            action="password_changed")

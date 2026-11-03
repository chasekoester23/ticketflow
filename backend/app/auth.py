"""Session authentication and explicit company identity for every API request.

Opaque sessions are stored only as SHA-256 digests. Passwords use salted
scrypt. Do not put tokens in URLs, JS-visible storage or logs.

Every account is ONE individual person with one company role. Audit records
attribute actions to that person, so shared logins must not be used.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import LoginSession, Organization, User

COOKIE_NAME = "ticketflow_session"
SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
PASSWORD_CHANGE_REQUIRED = "password_change_required"
KNOWN_ROLES = frozenset({"owner", "manager", "clerk", "viewer"})

_WINDOW_SECONDS = 900
# Per (client address, username): stops one source from guessing one login.
_MAX_FAILURES = 8
# Per real account, from any source: slows distributed guessing. Higher, so an
# attacker cannot trivially lock a real employee out from a single address.
_MAX_ACCOUNT_FAILURES = 20
# Bound memory. Overflow evicts expired entries first, then the oldest; it
# never clears everything (that would let an attacker reset every lockout by
# spraying usernames). Account counters are keyed by real user IDs only, so
# fake usernames cannot evict them.
_MAX_TRACKED = 5000

_fail_lock = threading.Lock()
_failures: dict[str, tuple[int, float]] = {}
_account_failures: dict[int, tuple[int, float]] = {}

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,99}$")


def validate_new_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Use a password between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters."
        )
    if not password.strip():
        raise ValueError("Password cannot be only spaces.")


def password_hash(password: str) -> str:
    validate_new_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def password_valid(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 1):
            return False
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=2**14, r=8, p=1, dklen=32,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, MemoryError):
        return False


# Verifying against this for unknown usernames keeps sign-in timing the same
# whether or not an account exists (no username discovery by timing).
_DUMMY_HASH = password_hash(secrets.token_urlsafe(24))


def normalize_username(raw: str) -> str:
    """Return the canonical login name or raise ValueError."""
    username = raw.strip().casefold()
    if not _USERNAME.fullmatch(username):
        raise ValueError(
            "Username must be 3-100 characters: letters, numbers, dots, dashes or "
            "underscores, starting with a letter or number."
        )
    return username


def generate_temporary_password() -> str:
    """A one-time password an owner hands to an employee; ~120 bits."""
    return secrets.token_urlsafe(15)


def _rate_key(request: Request, username: str) -> str:
    # A proxy must configure trusted forwarding separately. Never blindly
    # accept user-supplied X-Forwarded-For values for security decisions.
    ip = request.client.host if request.client else "unknown"
    return ip + ":" + username.casefold()


def _expired(started: float, now: float) -> bool:
    return now - started > _WINDOW_SECONDS


def _trim(table: dict, now: float) -> None:
    """Caller holds _fail_lock. Evict expired, then oldest; never clear all."""
    if len(table) < _MAX_TRACKED:
        return
    for key in [k for k, (_, started) in table.items() if _expired(started, now)]:
        del table[key]
    if len(table) >= _MAX_TRACKED:
        # Evict down to 90% so a sustained flood does not re-sort on every request.
        target = int(_MAX_TRACKED * 0.9)
        for key in sorted(table, key=lambda k: table[k][1])[: len(table) - target]:
            del table[key]


def _blocked(table: dict, key, limit: int, now: float) -> bool:
    count, started = table.get(key, (0, now))
    if _expired(started, now):
        table.pop(key, None)
        return False
    return count >= limit


def _record(table: dict, key, now: float) -> None:
    count, started = table.get(key, (0, now))
    if _expired(started, now):
        count, started = 0, now
    if key not in table:
        _trim(table, now)
    table[key] = (count + 1, started)


def login_blocked(key: str, user_id: int | None = None) -> bool:
    now = time.monotonic()
    with _fail_lock:
        if _blocked(_failures, key, _MAX_FAILURES, now):
            return True
        return user_id is not None and _blocked(_account_failures, user_id, _MAX_ACCOUNT_FAILURES, now)


def record_login_failure(key: str, user_id: int | None = None) -> None:
    now = time.monotonic()
    with _fail_lock:
        _record(_failures, key, now)
        if user_id is not None:
            _record(_account_failures, user_id, now)


def clear_login_failures(key: str, user_id: int | None = None) -> None:
    with _fail_lock:
        _failures.pop(key, None)
        if user_id is not None:
            _account_failures.pop(user_id, None)


def reset_login_throttle() -> None:
    """Test helper: forget all failure counters."""
    with _fail_lock:
        _failures.clear()
        _account_failures.clear()


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _presented_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 256 or not token.isascii():
        return None
    return token


def revoke_sessions(db: Session, user_id: int, *, keep_token_digest: str | None = None) -> None:
    """End every session for a user (optionally except the caller's own).

    Caller commits. Used on deactivation, role change, and password change/reset
    so a removed or demoted employee cannot keep acting on an old session.
    """
    q = delete(LoginSession).where(LoginSession.user_id == user_id)
    if keep_token_digest is not None:
        q = q.where(LoginSession.token_digest != keep_token_digest)
    db.execute(q)


def authenticated_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Any signed-in, active account, including one that must change its password.

    Only the sign-in status, sign-out and password-change endpoints use this
    directly. Everything else uses ``current_user``.
    """
    token = _presented_token(request)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to access TicketFlow")
    session = db.get(LoginSession, digest_token(token))
    if not session or session.expires_at <= _naive_utc_now():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired; sign in again")
    user = db.get(User, session.user_id)
    if not user or not user.active or user.organization_id is None or user.role not in KNOWN_ROLES:
        # Unknown roles fail closed rather than defaulting to any access.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account unavailable")
    if db.get(Organization, user.organization_id) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account organization unavailable")
    request.state.session_digest = session.token_digest
    return user


def current_user(user: User = Depends(authenticated_user)) -> User:
    """A signed-in account that is allowed to use the application."""
    if user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": PASSWORD_CHANGE_REQUIRED,
             "message": "Set a new password before continuing."},
        )
    return user


def current_organization_id(user: User = Depends(current_user)) -> int:
    """Never trust a company ID from a URL, header or request body."""
    if user.organization_id is None:
        raise HTTPException(403, "Account has no organization")
    return user.organization_id


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


def login(request: Request, response: Response, body: Credentials, db: Session) -> User:
    username = body.username.strip().casefold()
    key = _rate_key(request, username)
    user = db.scalar(select(User).where(User.username == username, User.active.is_(True)))
    user_id = user.id if user else None
    if login_blocked(key, user_id):
        raise HTTPException(429, "Too many failed sign-in attempts. Try again later.")
    valid = password_valid(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid or user.role not in KNOWN_ROLES:
        record_login_failure(key, user_id)
        raise HTTPException(401, "Invalid username or password")
    clear_login_failures(key, user_id)

    now = _naive_utc_now()
    # Housekeeping: expired sessions are useless and only grow the table.
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
    # Rotate: a session cookie presented at sign-in is never reused.
    previous = _presented_token(request)
    if previous is not None:
        db.execute(delete(LoginSession).where(LoginSession.token_digest == digest_token(previous)))
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(
        token_digest=digest_token(token), user_id=user.id,
        expires_at=now + timedelta(hours=SESSION_HOURS),
    ))
    db.commit()
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True,
        secure=settings.session_secure_cookie, samesite="lax", path="/",
        max_age=SESSION_HOURS * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return user


def logout(request: Request, response: Response, db: Session) -> dict:
    token = _presented_token(request)
    if token is not None:
        active = db.get(LoginSession, digest_token(token))
        if active:
            db.delete(active)
            db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax",
                           secure=settings.session_secure_cookie, httponly=True)
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}

"""Single-workspace owner authentication (NOT multi-tenant authorization).

Opaque sessions are stored only as SHA-256 digests in SQLite. Passwords use
salted scrypt. Do not put tokens in URLs, JS-visible storage or logs.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import LoginSession, User

COOKIE_NAME = "ticketflow_session"
SESSION_HOURS = 12
_fail_lock = threading.Lock()
_failures: dict[str, tuple[int, float]] = {}
_WINDOW_SECONDS = 900
_MAX_FAILURES = 8


def password_hash(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
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


def _rate_key(request: Request, username: str) -> str:
    # A proxy must configure trusted forwarding separately. Never blindly
    # accept user-supplied X-Forwarded-For values for security decisions.
    ip = request.client.host if request.client else "unknown"
    return ip + ":" + username.casefold()


def login_blocked(key: str) -> bool:
    now = time.monotonic()
    with _fail_lock:
        count, started = _failures.get(key, (0, now))
        if now - started > _WINDOW_SECONDS:
            _failures.pop(key, None)
            return False
        return count >= _MAX_FAILURES


def record_login_failure(key: str) -> None:
    now = time.monotonic()
    with _fail_lock:
        count, started = _failures.get(key, (0, now))
        if now - started > _WINDOW_SECONDS:
            count, started = 0, now
        if len(_failures) >= 3000:
            _failures.clear()  # bounded memory, not distributed rate limiting
        _failures[key] = (count + 1, started)


def clear_login_failures(key: str) -> None:
    with _fail_lock:
        _failures.pop(key, None)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 256:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to access TicketFlow")
    session = db.get(LoginSession, digest_token(token))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not session or session.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired; sign in again")
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account unavailable")
    return user


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


def login(request: Request, response: Response, body: Credentials, db: Session) -> dict:
    username = body.username.strip().casefold()
    key = _rate_key(request, username)
    if login_blocked(key):
        raise HTTPException(429, "Too many failed sign-in attempts. Try again later.")
    user = db.scalar(select(User).where(User.username == username, User.active.is_(True)))
    if not user or not password_valid(body.password, user.password_hash):
        record_login_failure(key)
        raise HTTPException(401, "Invalid username or password")
    clear_login_failures(key)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=SESSION_HOURS)
    db.add(LoginSession(token_digest=digest_token(token), user_id=user.id, expires_at=expires))
    db.commit()
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True,
        secure=settings.session_secure_cookie, samesite="lax", path="/",
        max_age=SESSION_HOURS * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"id": user.id, "username": user.username}


def logout(request: Request, response: Response, db: Session) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        active = db.get(LoginSession, digest_token(token))
        if active:
            db.delete(active)
            db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax",
                           secure=settings.session_secure_cookie, httponly=True)
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}

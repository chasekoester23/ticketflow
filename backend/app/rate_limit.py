"""A small limit on expensive requests, per person: uploads, exports and reports.

PER PROCESS ONLY. Counters live in this process's memory, so each API process counts
separately and a restart forgets them. That's enough to stop one person, or one stolen
session, tying up a single-machine installation. It is not protection against a
distributed attack, and a hosted deployment needs limits at its edge as well. See
SECURITY_PRELAUNCH.md.

Limits are per minute, from settings. Zero turns one off.
"""
from __future__ import annotations

import math
import threading
import time

from fastapi import Depends, HTTPException

from .auth import current_user
from .config import settings
from .models import User

_WINDOW = 60.0
_MAX_TRACKED = 10_000
_LABELS = {"uploads": "uploads", "exports": "exports", "reports": "reports"}

_lock = threading.Lock()
_windows: dict[tuple[str, int], tuple[float, int]] = {}


def reset() -> None:
    """Forget every counter. Called at startup."""
    with _lock:
        _windows.clear()


def allowed(bucket: str, user_id: int, limit: int) -> int | None:
    """None if this request may go ahead, otherwise the seconds until it may."""
    now = time.monotonic()
    with _lock:
        started, count = _windows.get((bucket, user_id), (now, 0))
        if now - started >= _WINDOW:
            started, count = now, 0
        if count >= limit:
            return max(1, math.ceil(_WINDOW - (now - started)))
        if (bucket, user_id) not in _windows and len(_windows) >= _MAX_TRACKED:
            # Only finished windows are dropped, so nobody's count is ever lost early.
            for key in [key for key, (began, _n) in _windows.items() if now - began >= _WINDOW]:
                del _windows[key]
            if len(_windows) >= _MAX_TRACKED:
                oldest = min(_windows, key=lambda key: _windows[key][0])
                del _windows[oldest]
        _windows[(bucket, user_id)] = (started, count + 1)
    return None


def limited(bucket: str):
    """FastAPI dependency: refuse with 429 once this person passes the bucket's limit."""
    if bucket not in _LABELS:
        raise KeyError(bucket)

    def dependency(user: User = Depends(current_user)) -> None:
        limit = getattr(settings, f"rate_limit_{bucket}_per_minute")
        if limit <= 0:
            return
        wait = allowed(bucket, user.id, limit)
        if wait is not None:
            raise HTTPException(429, f"Too many {_LABELS[bucket]} in a short time: the limit is {limit} per minute. "
                                     f"Try again in {wait} seconds.", headers={"Retry-After": str(wait)})

    dependency.bucket = bucket  # type: ignore[attr-defined] - lets a test prove every expensive route has one
    dependency.__name__ = f"rate_limited_{bucket}"
    return dependency

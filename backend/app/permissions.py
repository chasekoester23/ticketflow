"""Company roles and the named permissions each role grants.

Routes depend on a *permission* (``require("tickets.verify")``), never on a
role name, so the role table below is the single place that decides who may
do what. The frontend receives the resolved permission list from
``/api/auth/me`` only to hide unusable buttons; the server always enforces.

Design notes for construction teams:
- Clerks prepare records (upload, transcribe, correct, flag) but cannot
  approve quantities or change prices. Approval locks tonnage and a unit
  price into project costs and invoice comparisons, so it is a manager action.
- Withdrawing an approval (editing or flagging a verified ticket) is allowed
  for clerks: it can only remove a quantity from totals, never add one, and
  it is audited with the clerk's identity.
- Skipping excludes a scan from totals entirely, so it is a manager action.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException

from .auth import current_user
from .models import User

OWNER, MANAGER, CLERK, VIEWER = "owner", "manager", "clerk", "viewer"
ROLES: tuple[str, ...] = (OWNER, MANAGER, CLERK, VIEWER)

ROLE_LABELS = {
    OWNER: "Owner",
    MANAGER: "Manager",
    CLERK: "Clerk",
    VIEWER: "Viewer",
}

_EVERYONE = frozenset(ROLES)
_PREPARERS = frozenset({OWNER, MANAGER, CLERK})
_APPROVERS = frozenset({OWNER, MANAGER})

PERMISSIONS: dict[str, frozenset[str]] = {
    "records.read": _EVERYONE,
    "tickets.upload": _PREPARERS,
    "tickets.edit": _PREPARERS,
    "tickets.flag": _PREPARERS,
    "tickets.verify": _APPROVERS,
    "tickets.skip": _APPROVERS,
    "tickets.price_override": _APPROVERS,
    "projects.manage": _APPROVERS,
    "pricing.manage": _APPROVERS,
    "invoices.manage": _APPROVERS,
    "team.manage": frozenset({OWNER}),
}


def has_permission(user: User, permission: str) -> bool:
    if permission not in PERMISSIONS:
        # A typo must fail closed, not silently allow access.
        raise KeyError(f"Unknown permission {permission!r}")
    return user.role in PERMISSIONS[permission]


def permissions_for(role: str) -> list[str]:
    return sorted(name for name, roles in PERMISSIONS.items() if role in roles)


def require(permission: str) -> Callable[..., User]:
    """FastAPI dependency: the signed-in user, if their role grants ``permission``."""
    if permission not in PERMISSIONS:
        raise KeyError(f"Unknown permission {permission!r}")

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in PERMISSIONS[permission]:
            raise HTTPException(
                403, f"Your role ({ROLE_LABELS.get(user.role, user.role)}) does not allow this action.",
            )
        return user

    # Lets the route-coverage test prove every mutating route is guarded.
    dependency.permission = permission  # type: ignore[attr-defined]
    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    return dependency

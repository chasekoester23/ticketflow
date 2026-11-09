"""Creating a separate company workspace and its first owner.

The one place a new company comes into being, whether from a trusted terminal or,
later, from verified self-service sign-up. It always makes a new organization and
never touches another company's rows. /api/setup stays separate on purpose: it only
ever adopts the placeholder company on a server with no users.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..auth import generate_temporary_password, normalize_username, password_hash
from ..models import AccessRequest, AccountEvent, Organization, User


class ProvisioningError(ValueError):
    """Why a workspace couldn't be created, in words for the operator."""


def create_workspace(db: Session, *, company_name: str, owner_username: str, encoded_password: str,
                     owner_display_name: str | None = None, must_change_password: bool = False) -> tuple[Organization, User]:
    """Flushes without committing, so the caller's transaction decides. A taken username raises IntegrityError."""
    organization = Organization(name=company_name)
    db.add(organization)
    db.flush()
    owner = User(organization_id=organization.id, username=owner_username, display_name=owner_display_name,
                 password_hash=encoded_password, role="owner", must_change_password=must_change_password)
    db.add(owner)
    db.flush()
    db.add(AccountEvent(organization_id=organization.id, actor_user_id=owner.id, target_user_id=owner.id,
                        action="workspace_set_up", new_value="owner"))
    return organization, owner


def provision_request(db: Session, request_id: int, username: str) -> tuple[Organization, User, str]:
    """Turn a waiting request into a workspace. The owner must replace the returned password at first sign-in."""
    request = db.get(AccessRequest, request_id)
    if request is None:
        raise ProvisioningError("No request has that number.")
    if request.status not in {"new", "contacted"}:
        raise ProvisioningError(f"That request is already {request.status}.")
    try:
        login = normalize_username(username)
    except ValueError as exc:
        raise ProvisioningError(str(exc)) from None
    temporary = generate_temporary_password()
    organization, owner = create_workspace(
        db, company_name=request.company_name, owner_username=login, owner_display_name=request.contact_name,
        encoded_password=password_hash(temporary), must_change_password=True,
    )
    request.status = "provisioned"
    request.organization_id = organization.id
    return organization, owner, temporary

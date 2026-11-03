"""Sign-in, sign-out and self-service password endpoints.

These are intentionally separate from the protected API routers: they must
work for a person whose owner-issued temporary password still has to be changed.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import MAX_PASSWORD_LENGTH, Credentials, authenticated_user, login, logout
from ..db import get_db
from ..models import Organization, User
from ..permissions import ROLE_LABELS, permissions_for
from ..services.accounts import AccountError, change_own_password

router = APIRouter(prefix="/api/auth")


def account_profile(db: Session, user: User) -> dict:
    organization = db.get(Organization, user.organization_id)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "permissions": permissions_for(user.role),
        "must_change_password": user.must_change_password,
        "organization_name": organization.name if organization else None,
    }


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


@router.post("/login")
def sign_in(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    return account_profile(db, login(request, response, body, db))


@router.post("/logout")
def sign_out(request: Request, response: Response, db: Session = Depends(get_db)):
    # Logout must work with an expired session too.
    return logout(request, response, db)


@router.get("/me")
def me(user: User = Depends(authenticated_user), db: Session = Depends(get_db)):
    return account_profile(db, user)


@router.post("/password")
def change_password(body: PasswordChange, request: Request,
                    user: User = Depends(authenticated_user), db: Session = Depends(get_db)):
    """Change your own password; your other signed-in browsers are signed out."""
    try:
        change_own_password(
            db, user=user, current_password=body.current_password,
            new_password=body.new_password,
            keep_session_digest=getattr(request.state, "session_digest", None),
        )
    except AccountError as exc:
        db.rollback()
        raise HTTPException(exc.status, str(exc)) from exc
    db.commit()
    return account_profile(db, user)

"""Login endpoints are intentionally separate from the protected API routers."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..auth import Credentials, current_user, login, logout
from ..db import get_db
from ..models import User

router = APIRouter(prefix="/api/auth")


@router.post("/login")
def sign_in(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    return login(request, response, body, db)


@router.post("/logout")
def sign_out(request: Request, response: Response, db: Session = Depends(get_db)):
    # Logout must work with an expired session too.
    return logout(request, response, db)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username}

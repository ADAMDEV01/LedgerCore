import secrets
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.enums import Role
from app.core.errors import AuthenticationError, PermissionDenied
from app.core.security import decode_access_token
from app.models import User

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DbSession = Annotated[Session, Depends(get_db)]


def verify_csrf(request: Request) -> None:
    """Double-submit cookie check for every state-changing request."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise PermissionDenied("CSRF validation failed")


def get_current_user(request: Request, db: DbSession) -> User:
    verify_csrf(request)
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise AuthenticationError("Not authenticated")
    try:
        user_id = decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise AuthenticationError("Invalid or expired session") from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is disabled or no longer exists")
    return user


def require_roles(*allowed: Role):  # type: ignore[no-untyped-def]
    def checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in {r.value for r in allowed}:
            raise PermissionDenied("Your role is not permitted to perform this action")
        return user

    return checker


CurrentUser = Annotated[User, Depends(get_current_user)]
LedgerWriter = Annotated[User, Depends(require_roles(Role.ACCOUNTANT, Role.CFO))]
CfoUser = Annotated[User, Depends(require_roles(Role.CFO))]

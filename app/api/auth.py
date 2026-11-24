from fastapi import APIRouter, Depends, Request, Response
import re
import uuid
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    REFRESH_COOKIE,
    CurrentUser,
    DbSession,
    verify_csrf,
)
from app.core.errors import AuthenticationError, PermissionDenied
from app.core.security import create_access_token, new_csrf_token
from app.schemas import LoginRequest, RegisterRequest, SessionOut, UserOut
from app.models import Account, DocumentSequence, Organization
from app.core.enums import Role
from app.seed import CHART_OF_ACCOUNTS
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
REFRESH_PATH = "/api/v1/auth"


def _set_session_cookies(response: Response, user_id: int, refresh_token: str) -> None:
    secure = settings.cookie_secure
    refresh_age = settings.REFRESH_TOKEN_DAYS * 86400
    response.set_cookie(
        ACCESS_COOKIE,
        create_access_token(user_id),
        max_age=settings.ACCESS_TOKEN_MINUTES * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    # The refresh token is only ever sent to the auth endpoints.
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=refresh_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=REFRESH_PATH,
    )
    # Readable by JS on purpose: the SPA echoes it back in X-CSRF-Token (double-submit).
    response.set_cookie(
        CSRF_COOKIE,
        new_csrf_token(),
        max_age=refresh_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


@router.post("/register", response_model=SessionOut, status_code=201)
def register(
    payload: RegisterRequest, request: Request, response: Response, db: DbSession
) -> SessionOut:
    origin = request.headers.get("origin")
    host = request.headers.get("host", "").lower()
    if (settings.ENVIRONMENT == "production" and not origin) or (
        origin and urlsplit(origin).netloc.lower() != host
    ):
        raise PermissionDenied("Registration must be submitted from this site")

    company_name = payload.company_name.strip()
    base_slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")[:55] or "company"
    organization = Organization(name=company_name, slug=f"{base_slug}-{uuid.uuid4().hex[:8]}")
    db.add(organization)
    db.flush()
    set_tenant_context(db, organization.id)

    db.add_all(
        DocumentSequence(organization_id=organization.id, name=name, last_value=0)
        for name in ("JOURNAL", "INVOICE", "AUDIT")
    )
    db.add_all(
        Account(code=code, name=name, account_type=kind.value)
        for code, name, kind in CHART_OF_ACCOUNTS
    )
    user = auth_service.create_user(
        db,
        full_name=payload.full_name,
        email=str(payload.email),
        password=payload.password,
        role=Role.CFO.value,
        actor="system",
    )
    refresh = auth_service.create_refresh_token(db, user.id)
    db.commit()
    _set_session_cookies(response, user.id, refresh)
    return SessionOut(user=UserOut.model_validate(user))


@router.post("/login", response_model=SessionOut)
def login(payload: LoginRequest, request: Request, response: Response, db: DbSession) -> SessionOut:
    ip = request.client.host if request.client else "unknown"
    user = auth_service.authenticate(db, payload.email, payload.password, ip)
    refresh = auth_service.create_refresh_token(db, user.id)
    db.commit()
    _set_session_cookies(response, user.id, refresh)
    return SessionOut(user=UserOut.model_validate(user))


@router.post("/refresh", response_model=SessionOut, dependencies=[Depends(verify_csrf)])
def refresh(request: Request, response: Response, db: DbSession) -> SessionOut:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise AuthenticationError("Missing refresh token")
    user, new_raw = auth_service.rotate_refresh_token(db, raw)
    db.commit()
    _set_session_cookies(response, user.id, new_raw)
    return SessionOut(user=UserOut.model_validate(user))


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, response: Response, db: DbSession) -> dict[str, bool]:
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        auth_service.revoke_token(db, raw)
        db.commit()
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)

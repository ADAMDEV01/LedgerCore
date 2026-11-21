import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.errors import AuthenticationError, ConflictError, TooManyRequests
from app.core.security import (
    burn_verify,
    hash_password,
    hash_token,
    new_opaque_token,
    password_needs_rehash,
    verify_password,
)
from app.models import LoginAttempt, RefreshToken, User
from app.services.audit import log_audit


def check_login_allowed(db: Session, email: str, ip: str) -> None:
    """Database-backed throttle: counts recent failures per email and per IP."""
    since = clock.utcnow() - timedelta(minutes=settings.LOGIN_WINDOW_MINUTES)
    base = select(func.count()).select_from(LoginAttempt)
    base = base.where(LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at >= since)
    by_email = db.scalar(base.where(LoginAttempt.email == email)) or 0
    by_ip = db.scalar(base.where(LoginAttempt.ip == ip)) or 0
    if by_email >= settings.LOGIN_MAX_FAILURES or by_ip >= settings.LOGIN_MAX_FAILURES * 4:
        raise TooManyRequests(
            "Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(settings.LOGIN_WINDOW_MINUTES * 60)},
        )


def authenticate(db: Session, email: str, password: str, ip: str) -> User:
    """Auth use-cases commit themselves: failed attempts must persist even though we raise."""
    email = email.strip().lower()
    check_login_allowed(db, email, ip)
    user = db.scalar(select(User).where(User.email == email))
    ok = False
    if user is not None and user.is_active:
        ok = verify_password(password, user.password_hash)
    else:
        burn_verify(password)
    db.add(LoginAttempt(email=email, ip=ip, succeeded=ok, attempted_at=clock.utcnow()))
    if not ok or user is None:
        db.commit()
        raise AuthenticationError("Invalid email or password")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    log_audit(db, user.email, "LOGIN", f"user:{user.id}", "Signed in")
    return user


def create_refresh_token(db: Session, user_id: int, family_id: Optional[str] = None) -> str:
    raw = new_opaque_token()
    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=hash_token(raw),
            family_id=family_id or str(uuid.uuid4()),
            expires_at=clock.utcnow() + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        )
    )
    db.flush()
    return raw


def revoke_family(db: Session, family_id: str) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=clock.utcnow())
    )


def revoke_all_for_user(db: Session, user_id: int) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=clock.utcnow())
    )


def rotate_refresh_token(db: Session, raw: str) -> tuple[User, str]:
    """Atomically consume a refresh token before issuing its successor.

    The conditional UPDATE is the synchronization point: PostgreSQL serializes concurrent
    writers on the token row, and only the request that changes ``revoked_at`` from NULL can
    receive a successor. A losing request observes the committed revocation and triggers the
    existing token-family reuse response.
    """
    now = clock.utcnow()
    token_hash = hash_token(raw)
    consumed = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(RefreshToken.user_id, RefreshToken.family_id)
        .execution_options(synchronize_session=False)
    ).one_or_none()

    if consumed is None:
        token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        if token is None:
            raise AuthenticationError("Invalid refresh token")
        if token.revoked_at is not None:
            revoke_family(db, token.family_id)
            db.commit()
            raise AuthenticationError("Refresh token reuse detected; session revoked")
        if clock.as_utc(token.expires_at) <= now:
            raise AuthenticationError("Refresh token expired")
        # Defensive fallback for a database/driver that reports an unexpected conditional
        # update result. Never issue a successor unless this request consumed the row.
        raise AuthenticationError("Refresh token could not be consumed")

    user_id, family_id = consumed
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is disabled")
    return user, create_refresh_token(db, user.id, family_id)


def revoke_token(db: Session, raw: str) -> None:
    """Revoke the complete refresh-token family, even when the presented token was used.

    The first UPDATE takes the same row lock as rotation's conditional UPDATE. If a refresh
    is already in flight, logout waits for it to commit and then revokes the newly-issued
    successor as part of the same transaction. If the token was already consumed, its
    family_id is still returned and the entire family is revoked.
    """
    now = clock.utcnow()
    token_hash = hash_token(raw)
    family_id = db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .values(revoked_at=func.coalesce(RefreshToken.revoked_at, now))
        .returning(RefreshToken.family_id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()

    if family_id is not None:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id)
            .values(revoked_at=func.coalesce(RefreshToken.revoked_at, now))
            .execution_options(synchronize_session=False)
        )


def create_user(
    db: Session, *, full_name: str, email: str, password: str, role: str, actor: str
) -> User:
    email = email.strip().lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise ConflictError("A user with this email already exists")
    user = User(
        full_name=full_name.strip(),
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    log_audit(db, actor, "USER_CREATED", f"user:{user.id}", f"{email} as {role}")
    return user

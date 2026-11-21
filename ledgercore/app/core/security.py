import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core import clock
from app.core.config import settings

ALGORITHM = "HS256"
_hasher = PasswordHasher()
# Verified against when the account does not exist, so timing doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash("ledgercore-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def burn_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def password_needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def create_access_token(user_id: int) -> str:
    now = clock.utcnow()
    payload = {
        "sub": str(user_id),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> int:
    """Return the user id or raise jwt.InvalidTokenError."""
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub", "typ"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return int(payload["sub"])


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)

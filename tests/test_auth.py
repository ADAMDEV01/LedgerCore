from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.core.database import SessionLocal, engine
from app.core.errors import AuthenticationError, TooManyRequests
from app.models import LoginAttempt, User
from app.services import auth as auth_service
from tests.helpers import API, PASSWORD, create_user, login, make_client


def test_login_sets_hardened_cookies(anon):
    response = anon.post(f"{API}/auth/login", json={"email": "cfo@ledgertest.io", "password": PASSWORD})
    assert response.status_code == 200
    cookies = {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}
    assert "httponly" in cookies["access_token"] and "samesite=strict" in cookies["access_token"]
    assert "httponly" in cookies["refresh_token"] and "path=/api/v1/auth" in cookies["refresh_token"]
    assert "httponly" not in cookies["csrf_token"]  # the SPA must be able to read it
    assert "access_token" not in response.text  # tokens never appear in the body


def test_passwords_are_hashed_with_argon2id(db):
    user = db.query(User).filter_by(email="cfo@ledgertest.io").one()
    assert user.password_hash.startswith("$argon2id$")


def test_wrong_password_and_unknown_email_give_identical_errors(anon):
    wrong = anon.post(f"{API}/auth/login", json={"email": "cfo@ledgertest.io", "password": "nope-nope-nope"})
    unknown = anon.post(f"{API}/auth/login", json={"email": "ghost@ledgertest.io", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid email or password"}


def test_login_is_throttled_after_repeated_failures(anon):
    for _ in range(5):
        assert anon.post(f"{API}/auth/login", json={"email": "cfo@ledgertest.io", "password": "bad-bad-bad"}).status_code == 401
    blocked = anon.post(f"{API}/auth/login", json={"email": "cfo@ledgertest.io", "password": PASSWORD})
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_protected_routes_require_a_session(anon):
    assert anon.get(f"{API}/auth/me").status_code == 401
    assert anon.get(f"{API}/accounts").status_code == 401


def test_state_changing_requests_require_the_csrf_header(cfo):
    cfo.headers.pop("X-CSRF-Token")
    response = cfo.post(f"{API}/accounts", json={"code": "6000", "name": "Rent", "account_type": "EXPENSE"})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_refresh_rotates_the_refresh_token(cfo):
    before = cfo.cookies.get("refresh_token")
    response = cfo.post(f"{API}/auth/refresh")
    assert response.status_code == 200
    assert cfo.cookies.get("refresh_token") != before


def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(db):
    with pytest.raises(AuthenticationError):
        auth_service.rotate_refresh_token(db, "never-issued")
    user = db.query(User).filter_by(email="cfo@ledgertest.io").one()
    first = auth_service.create_refresh_token(db, user.id)
    db.commit()
    _, second = auth_service.rotate_refresh_token(db, first)
    db.commit()
    with pytest.raises(AuthenticationError, match="reuse"):
        auth_service.rotate_refresh_token(db, first)  # attacker replays the old token
    with pytest.raises(AuthenticationError):
        auth_service.rotate_refresh_token(db, second)  # ...and the legitimate chain is dead too


def test_logout_revokes_the_refresh_token(cfo, db):
    raw = cfo.cookies.get("refresh_token")
    assert cfo.post(f"{API}/auth/logout").status_code == 200
    with pytest.raises(AuthenticationError):
        auth_service.rotate_refresh_token(db, raw)


def test_logout_of_consumed_refresh_token_revokes_its_entire_family(db):
    user = db.query(User).filter_by(email="cfo@ledgertest.io").one()
    consumed = auth_service.create_refresh_token(db, user.id)
    db.commit()
    _, active_successor = auth_service.rotate_refresh_token(db, consumed)
    db.commit()

    # Simulate a browser logging out with a stale cookie while a refresh just rotated it.
    auth_service.revoke_token(db, consumed)
    db.commit()

    with pytest.raises(AuthenticationError, match="reuse"):
        auth_service.rotate_refresh_token(db, active_successor)


def test_logout_requires_csrf_and_does_not_revoke_without_it(cfo, db):
    raw = cfo.cookies.get("refresh_token")
    cfo.headers.pop("X-CSRF-Token")
    response = cfo.post(f"{API}/auth/logout")
    assert response.status_code == 403
    # A CSRF-rejected request must not consume or revoke the browser's refresh token.
    user, _ = auth_service.rotate_refresh_token(db, raw)
    assert user.email == "cfo@ledgertest.io"


def test_deactivated_users_lose_access_immediately(cfo):
    create_user("temp@ledgertest.io", "VIEWER")
    temp = login(make_client(), "temp@ledgertest.io")
    user_id = temp.get(f"{API}/auth/me").json()["id"]
    assert cfo.patch(f"{API}/users/{user_id}", json={"is_active": False}).status_code == 200
    assert temp.get(f"{API}/auth/me").status_code == 401
    retry = make_client().post(f"{API}/auth/login", json={"email": "temp@ledgertest.io", "password": PASSWORD})
    assert retry.status_code == 401


def test_registration_requires_company_identity_and_strong_password(anon):
    response = anon.post(f"{API}/auth/register", json={"email": "x@ledgertest.io", "password": PASSWORD})
    assert response.status_code == 422


def test_production_rejects_insecure_cookie_configuration():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="COOKIE_SECURE=false"):
        Settings(SECRET_KEY="a-strong-enough-secret-key-for-test-only", ENVIRONMENT="production", COOKIE_SECURE=False)

def test_expired_login_attempts_are_pruned_in_bounded_batches(db):
    from datetime import timedelta

    from app.core import clock

    now = clock.utcnow()
    expired = LoginAttempt(
        email="expired@ledgertest.io",
        ip="192.0.2.200",
        succeeded=False,
        attempted_at=now - timedelta(days=31),
    )
    recent = LoginAttempt(
        email="recent@ledgertest.io",
        ip="192.0.2.201",
        succeeded=False,
        attempted_at=now - timedelta(minutes=1),
    )
    db.add_all([expired, recent])
    db.commit()
    expired_id = expired.id
    recent_id = recent.id

    auth_service.check_login_allowed(db, "new@ledgertest.io", "192.0.2.202")
    db.commit()

    assert db.get(LoginAttempt, expired_id) is None
    assert db.get(LoginAttempt, recent_id) is not None


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="PostgreSQL advisory locks are used in production")
def test_login_throttle_cannot_be_exceeded_by_concurrent_failures():
    email = f"parallel-{uuid4().hex}@ledgertest.io"
    ip = "192.0.2.203"

    def attempt(_index: int) -> str:
        with SessionLocal() as session:
            try:
                auth_service.authenticate(session, email, "incorrect-password", ip)
            except TooManyRequests:
                return "limited"
            except AuthenticationError:
                return "denied"
        raise AssertionError("An unknown-email login must never authenticate")

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(attempt, range(12)))

    assert outcomes.count("denied") == 5
    assert outcomes.count("limited") == 7


from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import Account
from app.services import auth as auth_service

PASSWORD = "Correct-Horse-Battery-9"
API = "/api/v1"


def idempotency_headers(key: Optional[str] = None) -> dict[str, str]:
    return {"Idempotency-Key": key or uuid4().hex}


def make_client() -> TestClient:
    return TestClient(app)  # not used as a context manager, so the lifespan/seed does not run


def login(client: TestClient, email: str, password: str = PASSWORD) -> TestClient:
    response = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf_token")
    return client


def create_user(email: str, role: str) -> None:
    with SessionLocal() as db:
        auth_service.create_user(
            db, full_name="Test User", email=email, password=PASSWORD, role=role, actor="tests"
        )
        db.commit()


def acc(code: str) -> int:
    with SessionLocal() as db:
        return db.scalars(select(Account.id).where(Account.code == code)).one()


def entry_body(
    debit_code: str,
    credit_code: str,
    amount: str,
    description: str = "Test entry",
    entry_date: Optional[date] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "description": description,
        "lines": [
            {"account_id": acc(debit_code), "debit": amount, "credit": "0"},
            {"account_id": acc(credit_code), "debit": "0", "credit": amount},
        ],
    }
    if entry_date:
        body["entry_date"] = entry_date.isoformat()
    return body


def post_je(client: TestClient, debit_code: str, credit_code: str, amount: str, **kw: Any):  # type: ignore[no-untyped-def]
    return client.post(
        f"{API}/journal-entries",
        json=entry_body(debit_code, credit_code, amount, **kw),
        headers=idempotency_headers(),
    )


def balance(client: TestClient, code: str) -> Decimal:
    items = client.get(f"{API}/accounts?page_size=500").json()["items"]
    return Decimal(next(a["balance"] for a in items if a["code"] == code))


def new_invoice(client: TestClient, **overrides: Any):  # type: ignore[no-untyped-def]
    idempotency_key = overrides.pop("idempotency_key", None)
    body: dict[str, Any] = {
        "client_name": "Acme Ltd",
        "currency": "USD",
        "exchange_rate": "1",
        "amount": "1000.00",
        "tax_rate": "14.00",
        "due_date": (date.today() + timedelta(days=30)).isoformat(),
    }
    body.update(overrides)
    return client.post(f"{API}/invoices", json=body, headers=idempotency_headers(idempotency_key))


def pay(client: TestClient, invoice_id: int, amount: str, rate: Optional[str] = None):  # type: ignore[no-untyped-def]
    body: dict[str, Any] = {"amount": amount}
    if rate:
        body["payment_rate"] = rate
    return client.post(
        f"{API}/invoices/{invoice_id}/payments", json=body, headers=idempotency_headers()
    )

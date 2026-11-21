from app.core.config import settings
from app.seed import seed_database
from tests.helpers import API, acc, balance, entry_body, idempotency_headers, login, make_client, post_je


def test_viewer_can_read_but_not_write(viewer):
    assert viewer.get(f"{API}/accounts").status_code == 200
    assert viewer.get(f"{API}/reports/trial-balance").status_code == 200
    assert viewer.post(
        f"{API}/journal-entries",
        json=entry_body("1000", "3000", "10.00"),
        headers=idempotency_headers(),
    ).status_code == 403
    assert viewer.post(f"{API}/invoices", json={}).status_code in (403, 422)


def test_configured_demo_account_is_provisioned_as_read_only(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_VIEWER_EMAIL", "demo@ledgertest.io")
    monkeypatch.setattr(settings, "DEMO_VIEWER_PASSWORD", "View-Only-Demo-Account-2026!")
    seed_database()
    demo = login(make_client(), "demo@ledgertest.io", "View-Only-Demo-Account-2026!")
    assert demo.get(f"{API}/reports/trial-balance").status_code == 200
    denied = demo.post(
        f"{API}/journal-entries",
        json=entry_body("1000", "3000", "1.00"),
        headers=idempotency_headers(),
    )
    assert denied.status_code == 403


def test_accountant_can_post_but_not_reverse_or_administer(accountant, cfo):
    response = post_je(accountant, "1000", "3000", "250.00")
    assert response.status_code == 201
    entry_id = response.json()["id"]
    assert accountant.post(
        f"{API}/journal-entries/{entry_id}/reverse",
        json={"reason": "oops"},
        headers=idempotency_headers(),
    ).status_code == 403
    assert accountant.post(f"{API}/accounts", json={"code": "6000", "name": "Rent", "account_type": "EXPENSE"}).status_code == 403
    assert accountant.get(f"{API}/users").status_code == 403
    assert accountant.get(f"{API}/audit-logs").status_code == 403
    assert accountant.post(f"{API}/periods/2026-01/close").status_code == 403
    assert balance(cfo, "1000") == 250


def test_only_cfo_can_manage_users_and_cannot_edit_self(cfo):
    me = cfo.get(f"{API}/auth/me").json()
    created = cfo.post(
        f"{API}/users",
        json={"full_name": "New Person", "email": "new@ledgertest.io", "password": "A-long-enough-pass-1", "role": "VIEWER"},
    )
    assert created.status_code == 201
    assert cfo.patch(f"{API}/users/{me['id']}", json={"role": "VIEWER"}).status_code == 409
    duplicate = cfo.post(
        f"{API}/users",
        json={"full_name": "Dup", "email": "NEW@ledgertest.io", "password": "A-long-enough-pass-1", "role": "VIEWER"},
    )
    assert duplicate.status_code == 409


def test_weak_passwords_are_rejected(cfo):
    response = cfo.post(
        f"{API}/users",
        json={"full_name": "Weak One", "email": "weak@ledgertest.io", "password": "short", "role": "VIEWER"},
    )
    assert response.status_code == 422
    assert acc("1000")  # seeded chart exists

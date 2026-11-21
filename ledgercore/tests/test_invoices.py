from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm.exc import StaleDataError

from app.core import clock
from app.core.database import SessionLocal
from app.models import Invoice
from tests.helpers import API, balance, idempotency_headers, new_invoice, pay


def test_invoice_with_tax_posts_ar_revenue_and_tax_payable(cfo):
    response = new_invoice(cfo)
    assert response.status_code == 201
    invoice = response.json()
    assert invoice["invoice_number"] == "INV-000001"
    assert (invoice["tax_amount"], invoice["total_amount"]) == ("140.00", "1140.00")
    assert balance(cfo, "1100") == Decimal("1140.00")
    assert balance(cfo, "4000") == Decimal("1000.00")
    assert balance(cfo, "2100") == Decimal("140.00")


def test_invoice_creation_is_idempotent_and_rejects_key_reuse_with_different_data(cfo):
    key = "invoice-create-idempotency-0001"
    first = new_invoice(cfo, idempotency_key=key)
    retry = new_invoice(cfo, idempotency_key=key)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()

    changed = new_invoice(cfo, idempotency_key=key, amount="1001.00")
    assert changed.status_code == 409
    assert cfo.get(f"{API}/invoices?page_size=50").json()["total"] == 1


def test_payment_retry_does_not_create_a_second_payment_or_journal_entry(cfo):
    invoice = new_invoice(cfo, tax_rate="0").json()
    key = "payment-idempotency-key-0000001"
    body = {"amount": "125.00"}
    headers = {"Idempotency-Key": key}
    first = cfo.post(f"{API}/invoices/{invoice['id']}/payments", json=body, headers=headers)
    retry = cfo.post(f"{API}/invoices/{invoice['id']}/payments", json=body, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    assert len(retry.json()["payments"]) == 1
    assert retry.json()["amount_paid"] == "125.00"


def test_foreign_currency_invoice_is_booked_at_the_issue_rate(cfo):
    invoice = new_invoice(cfo, currency="EUR", exchange_rate="1.100000", tax_rate="0").json()
    assert invoice["base_total"] == "1100.00"
    assert balance(cfo, "1100") == Decimal("1100.00")


def test_partial_then_full_payment_clears_receivables(cfo):
    invoice = new_invoice(cfo).json()
    first = pay(cfo, invoice["id"], "500.00").json()
    assert first["status"] == "PARTIALLY_PAID" and first["balance_due"] == "640.00"
    final = pay(cfo, invoice["id"], "640.00").json()
    assert final["status"] == "PAID" and final["balance_due"] == "0.00"
    assert balance(cfo, "1100") == 0
    assert balance(cfo, "1000") == Decimal("1140.00")


def test_final_payment_never_leaves_a_stray_cent_in_receivables(cfo):
    invoice = new_invoice(cfo, currency="EUR", exchange_rate="1.333333", amount="100.00", tax_rate="0").json()
    assert invoice["base_total"] == "133.33"
    for amount in ("33.33", "33.33", "33.34"):
        assert pay(cfo, invoice["id"], amount).status_code == 201
    assert balance(cfo, "1100") == 0


def test_paying_at_a_better_rate_books_a_realized_fx_gain(cfo):
    invoice = new_invoice(cfo, currency="EUR", exchange_rate="1.100000", amount="1000.00", tax_rate="0").json()
    response = pay(cfo, invoice["id"], "1000.00", rate="1.200000")
    assert response.status_code == 201
    payment = response.json()["payments"][0]
    assert (payment["cash_base"], payment["ar_cleared_base"], payment["fx_difference"]) == ("1200.00", "1100.00", "100.00")
    assert balance(cfo, "1000") == Decimal("1200.00")
    assert balance(cfo, "1100") == 0
    assert balance(cfo, "4900") == Decimal("100.00")


def test_paying_at_a_worse_rate_books_a_realized_fx_loss(cfo):
    invoice = new_invoice(cfo, currency="EUR", exchange_rate="1.100000", amount="1000.00", tax_rate="0").json()
    pay(cfo, invoice["id"], "1000.00", rate="1.000000")
    assert balance(cfo, "4900") == Decimal("-100.00")
    assert balance(cfo, "1100") == 0


def test_overpayment_and_double_payment_are_rejected(cfo):
    invoice = new_invoice(cfo, tax_rate="0").json()
    assert pay(cfo, invoice["id"], "1000.01").status_code == 422
    assert pay(cfo, invoice["id"], "1000.00").status_code == 201
    assert pay(cfo, invoice["id"], "1.00").status_code == 409


def test_base_currency_invoice_cannot_carry_a_fx_rate(cfo):
    assert new_invoice(cfo, currency="USD", exchange_rate="1.2").status_code == 422


def test_due_date_in_the_past_is_rejected(cfo):
    assert new_invoice(cfo, due_date=(date.today() - timedelta(days=1)).isoformat()).status_code == 422


def test_overdue_is_derived_from_the_due_date_not_stored(cfo, monkeypatch):
    new_invoice(cfo, due_date=(date.today() + timedelta(days=5)).isoformat())
    assert cfo.get(f"{API}/invoices?status=OVERDUE").json()["total"] == 0
    monkeypatch.setattr(clock, "today", lambda: date.today() + timedelta(days=40))
    overdue = cfo.get(f"{API}/invoices?status=OVERDUE").json()
    assert overdue["total"] == 1 and overdue["items"][0]["status"] == "OVERDUE"
    assert cfo.get(f"{API}/invoices?status=ISSUED").json()["total"] == 0
    assert cfo.get(f"{API}/dashboard/summary").json()["overdue_invoices"] == 1


def test_voiding_an_invoice_reverses_its_ledger_effect(cfo):
    invoice = new_invoice(cfo).json()
    voided = cfo.post(f"{API}/invoices/{invoice['id']}/void", headers=idempotency_headers())
    assert voided.status_code == 200 and voided.json()["status"] == "VOID"
    assert balance(cfo, "1100") == 0 and balance(cfo, "4000") == 0 and balance(cfo, "2100") == 0
    assert cfo.post(
        f"{API}/invoices/{invoice['id']}/void", headers=idempotency_headers()
    ).status_code == 409
    assert pay(cfo, invoice["id"], "10.00").status_code == 409


def test_invoices_with_payments_cannot_be_voided(cfo):
    invoice = new_invoice(cfo).json()
    pay(cfo, invoice["id"], "10.00")
    assert cfo.post(
        f"{API}/invoices/{invoice['id']}/void", headers=idempotency_headers()
    ).status_code == 409


def test_invoice_entries_cannot_be_reversed_by_hand(cfo):
    new_invoice(cfo)
    entry = cfo.get(f"{API}/journal-entries").json()["items"][0]
    assert entry["source"] == "INVOICE"
    assert cfo.post(
        f"{API}/journal-entries/{entry['id']}/reverse",
        json={"reason": "manual fix"},
        headers=idempotency_headers(),
    ).status_code == 409


def test_invoice_numbers_are_gapless_and_sequential(cfo):
    numbers = [new_invoice(cfo).json()["invoice_number"] for _ in range(3)]
    assert numbers == ["INV-000001", "INV-000002", "INV-000003"]


def test_concurrent_modification_is_detected_by_optimistic_locking(cfo):
    invoice_id = new_invoice(cfo).json()["id"]
    first, second = SessionLocal(), SessionLocal()
    try:
        a, b = first.get(Invoice, invoice_id), second.get(Invoice, invoice_id)
        a.client_name = "Writer A"
        first.commit()
        b.client_name = "Writer B"
        with pytest.raises(StaleDataError):
            second.commit()
    finally:
        first.close()
        second.close()

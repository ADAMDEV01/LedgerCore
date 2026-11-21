from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import LedgerValidationError
from app.models import JournalEntry, LedgerLine
from app.services.ledger import LineInput, validate_lines
from tests.helpers import API, acc, balance, entry_body, idempotency_headers, post_je


def test_posting_updates_both_sides_and_numbers_entries_sequentially(cfo):
    first = post_je(cfo, "1000", "3000", "500.00")
    second = post_je(cfo, "5000", "1000", "120.50")
    assert first.status_code == second.status_code == 201
    assert (first.json()["reference_no"], second.json()["reference_no"]) == ("JE-000001", "JE-000002")
    assert balance(cfo, "1000") == Decimal("379.50")  # asset: +500 - 120.50
    assert balance(cfo, "3000") == Decimal("500.00")  # equity grows with credits
    assert balance(cfo, "5000") == Decimal("120.50")  # expense grows with debits


def test_journal_post_is_idempotent_for_a_retried_request(cfo):
    body = entry_body("1000", "3000", "42.00", description="Retry-safe entry")
    headers = {"Idempotency-Key": "journal-create-idempotency-00001"}
    first = cfo.post(f"{API}/journal-entries", json=body, headers=headers)
    retry = cfo.post(f"{API}/journal-entries", json=body, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    assert cfo.get(f"{API}/journal-entries?page_size=50").json()["total"] == 1


def test_unbalanced_entry_is_rejected(cfo):
    body = entry_body("1000", "3000", "100.00")
    body["lines"][1]["credit"] = "90.00"
    response = cfo.post(f"{API}/journal-entries", json=body, headers=idempotency_headers())
    assert response.status_code == 422
    assert "not balanced" in response.text
    assert balance(cfo, "1000") == 0


def test_sub_cent_amounts_are_rejected_not_rounded(cfo):
    assert post_je(cfo, "1000", "3000", "10.005").status_code == 422


def test_a_line_cannot_be_both_debit_and_credit_or_empty(cfo):
    both = entry_body("1000", "3000", "10.00")
    both["lines"][0]["credit"] = "10.00"
    empty = entry_body("1000", "3000", "10.00")
    empty["lines"][0]["debit"] = "0"
    assert cfo.post(f"{API}/journal-entries", json=both, headers=idempotency_headers()).status_code == 422
    assert cfo.post(f"{API}/journal-entries", json=empty, headers=idempotency_headers()).status_code == 422


def test_single_line_entries_are_rejected(cfo):
    body = entry_body("1000", "3000", "10.00")
    body["lines"] = body["lines"][:1]
    assert cfo.post(f"{API}/journal-entries", json=body, headers=idempotency_headers()).status_code == 422


def test_unknown_account_is_404(cfo):
    body = entry_body("1000", "3000", "10.00")
    body["lines"][1]["account_id"] = 99999
    assert cfo.post(f"{API}/journal-entries", json=body, headers=idempotency_headers()).status_code == 404


def test_reversal_restores_balances_and_flags_the_original(cfo):
    original = post_je(cfo, "1000", "3000", "800.00").json()
    reversal = cfo.post(
        f"{API}/journal-entries/{original['id']}/reverse",
        json={"reason": "Posted twice"},
        headers=idempotency_headers(),
    )
    assert reversal.status_code == 201
    assert reversal.json()["reverses_entry_id"] == original["id"]
    assert balance(cfo, "1000") == 0 and balance(cfo, "3000") == 0
    listing = cfo.get(f"{API}/journal-entries?page_size=50").json()["items"]
    flags = {e["id"]: e["is_reversed"] for e in listing}
    assert flags[original["id"]] is True and flags[reversal.json()["id"]] is False


def test_an_entry_can_be_reversed_only_once_and_reversals_are_final(cfo):
    original = post_je(cfo, "1000", "3000", "50.00").json()
    first = cfo.post(
        f"{API}/journal-entries/{original['id']}/reverse",
        json={"reason": "mistake"}, headers=idempotency_headers(),
    )
    again = cfo.post(
        f"{API}/journal-entries/{original['id']}/reverse",
        json={"reason": "mistake"}, headers=idempotency_headers(),
    )
    of_reversal = cfo.post(
        f"{API}/journal-entries/{first.json()['id']}/reverse",
        json={"reason": "undo undo"}, headers=idempotency_headers(),
    )
    assert again.status_code == 409
    assert of_reversal.status_code == 409


def test_closed_periods_reject_postings_until_reopened(cfo):
    period = date.today().strftime("%Y-%m")
    assert cfo.post(f"{API}/periods/{period}/close").status_code == 200
    assert cfo.post(f"{API}/periods/{period}/close").status_code == 409  # already closed
    blocked = post_je(cfo, "1000", "3000", "10.00")
    assert blocked.status_code == 409 and "closed" in blocked.json()["detail"]
    assert cfo.post(f"{API}/periods/{period}/reopen").status_code == 200
    assert post_je(cfo, "1000", "3000", "10.00").status_code == 201


def test_invalid_period_format_is_rejected(cfo):
    assert cfo.post(f"{API}/periods/2026-13/close").status_code == 422


def test_journal_listing_supports_pagination(cfo):
    for _ in range(3):
        post_je(cfo, "1000", "3000", "1.00")
    page = cfo.get(f"{API}/journal-entries?page=2&page_size=2").json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_service_level_validation_does_not_trust_callers():
    cash = acc("1000")
    with pytest.raises(LedgerValidationError):
        validate_lines([LineInput(cash, debit=Decimal("5.00"))])
    with pytest.raises(LedgerValidationError):
        validate_lines([LineInput(cash, debit=Decimal("5.00")), LineInput(cash, credit=Decimal("4.00"))])
    validate_lines([LineInput(cash, debit=Decimal("5.00")), LineInput(cash, credit=Decimal("5.00"))])


def test_database_check_constraint_rejects_two_sided_lines(db):
    entry = JournalEntry(
        reference_no="JE-X", entry_date=date.today(), accounting_period="2026-01",
        description="raw", source="MANUAL", posted_by="tests",
    )
    entry.lines = [LedgerLine(account_id=acc("1000"), debit=Decimal("1.00"), credit=Decimal("1.00"))]
    db.add(entry)
    with pytest.raises(IntegrityError):
        db.flush()

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.core.money import money
from app.schemas import JournalEntryCreate
from app.services.invoices import plan_settlement
from tests.helpers import API, acc, idempotency_headers

amounts = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("99999.99"), places=2,
    allow_nan=False, allow_infinity=False,
)
rates = st.decimals(
    min_value=Decimal("0.2"), max_value=Decimal("5"), places=6,
    allow_nan=False, allow_infinity=False,
)


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(debits=st.lists(amounts, min_size=1, max_size=5))
def test_any_balanced_entry_is_accepted_and_the_ledger_stays_balanced(cfo, debits):
    total = sum(debits, Decimal("0"))
    lines = [{"account_id": acc("1000"), "debit": str(d), "credit": "0"} for d in debits]
    lines.append({"account_id": acc("3000"), "debit": "0", "credit": str(total)})
    response = cfo.post(
        f"{API}/journal-entries",
        json={"description": "property", "lines": lines},
        headers=idempotency_headers(),
    )
    assert response.status_code == 201, response.text
    report = cfo.get(f"{API}/reports/trial-balance").json()
    assert report["is_balanced"] and report["total_debit"] == report["total_credit"]


@settings(max_examples=100, deadline=None)
@given(pair=st.tuples(amounts, amounts).filter(lambda t: t[0] != t[1]))
def test_unbalanced_entries_never_pass_validation(pair):
    debit, credit = pair
    with pytest.raises(ValidationError):
        JournalEntryCreate(
            description="unbalanced",
            lines=[
                {"account_id": 1, "debit": debit, "credit": 0},
                {"account_id": 2, "debit": 0, "credit": credit},
            ],
        )


@settings(max_examples=300, deadline=None)
@given(data=st.data(), total=amounts, invoice_rate=rates, payment_rate=rates)
def test_installments_always_settle_receivables_exactly(data, total, invoice_rate, payment_rate):
    cents = int(total * 100)
    assume(cents >= 2)
    cuts = sorted(data.draw(st.sets(st.integers(1, cents - 1), max_size=5)))
    bounds = [0, *cuts, cents]
    installments = [Decimal(b - a) / 100 for a, b in zip(bounds, bounds[1:])]
    base_total = money(total * invoice_rate)

    paid, settled = Decimal("0"), Decimal("0")
    for amount in installments:
        plan = plan_settlement(
            total=total, base_total=base_total, base_settled=settled, paid_before=paid,
            amount=amount, invoice_rate=invoice_rate, payment_rate=payment_rate,
        )
        assert 0 <= plan.ar_cleared <= base_total - settled  # never over-clears receivables
        assert plan.cash - plan.ar_cleared == plan.fx_diff  # the entry always balances
        paid += amount
        settled += plan.ar_cleared
    assert paid == total
    assert settled == base_total  # no stray cents, whatever the rounding path

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import settings
from app.schemas import InvoiceCreate
from tests.helpers import API


def test_public_application_settings_use_the_server_base_currency(anon):
    response = anon.get(f"{API}/settings")

    assert response.status_code == 200
    assert response.json() == {"base_currency": settings.BASE_CURRENCY}


def test_current_accounting_period_is_tenant_scoped_and_tracks_closed_state(cfo):
    from app.core import clock

    period = clock.today().strftime("%Y-%m")
    response = cfo.get(f"{API}/accounting-period")
    assert response.status_code == 200
    assert response.json() == {"period": period, "status": "OPEN"}

    closed = cfo.post(f"{API}/periods/{period}/close")
    assert closed.status_code == 200
    updated = cfo.get(f"{API}/accounting-period")
    assert updated.status_code == 200
    assert updated.json() == {"period": period, "status": "CLOSED"}


def test_invoice_default_currency_tracks_runtime_base_currency(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "BASE_CURRENCY", "EUR")

    invoice = InvoiceCreate(
        client_name="Example Client",
        amount=Decimal("125.00"),
        due_date=date(2026, 12, 31),
    )

    assert invoice.currency == "EUR"

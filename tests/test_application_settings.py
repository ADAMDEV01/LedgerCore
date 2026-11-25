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

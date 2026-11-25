import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.currencies import (
    ACTIVE_CURRENCY_CODES,
    ISO4217_LIST_ONE_PUBLISHED,
    is_active_currency_code,
)
from tests.helpers import API, new_invoice


def test_registry_contains_current_currency_codes_and_excludes_historical_bgn():
    assert ISO4217_LIST_ONE_PUBLISHED == "2026-09-17"
    assert "USD" in ACTIVE_CURRENCY_CODES
    assert "EGP" in ACTIVE_CURRENCY_CODES
    assert "EUR" in ACTIVE_CURRENCY_CODES
    assert "BGN" not in ACTIVE_CURRENCY_CODES
    assert "ZZZ" not in ACTIVE_CURRENCY_CODES
    assert not is_active_currency_code("USD ")
    assert not is_active_currency_code("usd")


@pytest.mark.parametrize("code", ["ZZZ", "BGN", "XXX", "USD ", "usd"])
def test_invoice_rejects_non_active_iso4217_codes(cfo, code):
    response = new_invoice(cfo, currency=code, tax_rate="0")

    assert response.status_code == 422
    assert cfo.get(f"{API}/invoices?page_size=50").json()["total"] == 0


def test_invoice_accepts_current_egyptian_pound_code(cfo):
    response = new_invoice(cfo, currency="EGP", tax_rate="0")

    assert response.status_code == 201
    assert response.json()["currency"] == "EGP"


@pytest.mark.parametrize("code", ["USD", "EUR", "EGP", "JPY"])
def test_base_currency_requires_an_active_iso4217_code(code):
    settings = Settings(
        _env_file=None,
        SECRET_KEY="test-secret-value-that-is-longer-than-32-bytes",
        ENVIRONMENT="test",
        BASE_CURRENCY=code,
    )
    assert settings.BASE_CURRENCY == code


@pytest.mark.parametrize("code", ["ZZZ", "BGN", "XXX"])
def test_base_currency_rejects_unknown_or_historical_codes(code):
    with pytest.raises(ValidationError, match="active ISO 4217"):
        Settings(
            _env_file=None,
            SECRET_KEY="test-secret-value-that-is-longer-than-32-bytes",
            ENVIRONMENT="test",
            BASE_CURRENCY=code,
        )



def test_base_currency_still_requires_uppercase_alpha_code():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SECRET_KEY="test-secret-value-that-is-longer-than-32-bytes",
            ENVIRONMENT="test",
            BASE_CURRENCY="usd",
        )

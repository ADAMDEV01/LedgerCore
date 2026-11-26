import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.currencies import (
    ACTIVE_CURRENCY_CODES,
    ISO4217_LIST_ONE_CURRENCY_COUNT,
    ISO4217_LIST_ONE_PUBLISHED,
    is_active_currency_code,
)
from tests.helpers import API, new_invoice


def test_registry_contains_only_current_currencies_with_minor_units():
    assert ISO4217_LIST_ONE_PUBLISHED == "2026-09-17"
    assert ISO4217_LIST_ONE_CURRENCY_COUNT == 155
    assert len(ACTIVE_CURRENCY_CODES) == ISO4217_LIST_ONE_CURRENCY_COUNT
    assert {"USD", "EGP", "EUR", "XAF", "XCG", "YER", "ZAR", "ZMW", "ZWG"} <= ACTIVE_CURRENCY_CODES
    assert {
        "BGN", "BOV", "CHE", "CHW", "CLF", "MXV", "USN", "UYI", "UYW",
        "XAD", "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD",
        "XPT", "XSU", "XTS", "XUA", "XXX", "ZZZ",
    }.isdisjoint(ACTIVE_CURRENCY_CODES)
    assert not is_active_currency_code("USD ")
    assert not is_active_currency_code("usd")


@pytest.mark.parametrize(
    "code",
    ["ZZZ", "BGN", "XXX", "BOV", "XAG", "XAU", "XBA", "XDR", "XTS", "XUA", "USD ", "usd"],
)
def test_invoice_rejects_non_currency_or_inactive_iso4217_codes(cfo, code):
    response = new_invoice(cfo, currency=code, tax_rate="0")

    assert response.status_code == 422
    assert cfo.get(f"{API}/invoices?page_size=50").json()["total"] == 0


@pytest.mark.parametrize("code", ["EGP", "XCG", "YER", "ZAR", "ZMW", "ZWG"])
def test_invoice_accepts_current_iso4217_currencies(cfo, code):
    response = new_invoice(cfo, currency=code, tax_rate="0")

    assert response.status_code == 201
    assert response.json()["currency"] == code


@pytest.mark.parametrize("code", ["USD", "EUR", "EGP", "JPY"])
def test_base_currency_requires_an_active_iso4217_code(code):
    settings = Settings(
        _env_file=None,
        SECRET_KEY="test-secret-value-that-is-longer-than-32-bytes",
        ENVIRONMENT="test",
        BASE_CURRENCY=code,
    )
    assert settings.BASE_CURRENCY == code


@pytest.mark.parametrize(
    "code",
    ["ZZZ", "BGN", "XXX", "BOV", "XAU", "XDR", "XTS", "XUA"],
)
def test_base_currency_rejects_unknown_historical_or_non_currency_codes(code):
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

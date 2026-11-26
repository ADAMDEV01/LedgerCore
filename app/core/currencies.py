"""ISO 4217 currencies supported by LedgerCore's two-decimal money model.

Source: SIX List One (Current Currency & Funds), published
2026-09-17. LedgerCore stores monetary values
in NUMERIC(14, 2) columns and validates input to two decimal places. Only
non-fund current currencies with a minor-unit exponent of exactly 2 are
accepted. Currencies with zero, three, or four minor-unit digits, funds,
precious metals, bond-market units, special accounting units, test codes,
and the no-currency code are intentionally unsupported until the money model
supports their precision.
"""

from __future__ import annotations

ISO4217_LIST_ONE_PUBLISHED = "2026-09-17"
SUPPORTED_CURRENCY_MINOR_UNIT = 2
SUPPORTED_CURRENCY_COUNT = 132

ACTIVE_CURRENCY_CODES = frozenset(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CNY COP CRC CUP CVE CZK
    DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ
    GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD
    KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR
    MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP
    PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS
    SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH
    USD UYU UZS VED VES WST XCD XCG YER ZAR ZMW ZWG
    """.split()
)


def is_active_currency_code(value: object) -> bool:
    """Return whether value is a supported current two-decimal ISO 4217 currency."""
    return isinstance(value, str) and value in ACTIVE_CURRENCY_CODES


def validate_currency_code(value: str) -> str:
    """Validate an invoice currency code without silently normalizing it."""
    if not is_active_currency_code(value):
        raise ValueError(
            f"'{value}' is not a supported ISO 4217 currency code. "
            "LedgerCore currently supports active currencies with a two-digit minor unit."
        )
    return value

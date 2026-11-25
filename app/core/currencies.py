"""Active ISO 4217 currency codes used by LedgerCore invoices.

Source snapshot: SIX, the ISO 4217 Maintenance Agency, List One
(Current Currency & Funds), published 2026-09-17. Fund codes and historical
codes are intentionally excluded from invoice currencies.
"""

from __future__ import annotations

ISO4217_LIST_ONE_PUBLISHED = "2026-09-17"

ACTIVE_CURRENCY_CODES = frozenset(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND
    BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE
    CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD
    GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD
    MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO
    NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB
    TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND
    VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT
    XSU
    """.split()
)


def is_active_currency_code(value: object) -> bool:
    """Return whether value is an active ISO 4217 List One currency code."""
    return isinstance(value, str) and value in ACTIVE_CURRENCY_CODES


def validate_currency_code(value: str) -> str:
    """Validate an invoice currency code without silently normalizing user input."""
    if not is_active_currency_code(value):
        raise ValueError(
            f"'{value}' is not an active ISO 4217 currency code. "
            "Use a current three-letter code from ISO 4217 List One."
        )
    return value

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: Any) -> Decimal:
    """Quantize to cents with HALF_UP rounding (what accountants expect)."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    return f"{value:.2f}"

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Generic, Optional, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    PlainSerializer,
    model_validator,
)

from app.core import clock
from app.core.constants import PERIOD_PATTERN
from app.core.enums import AccountType, Role

T = TypeVar("T")

# Output money is always a fixed 2dp string, never a float.
Money = Annotated[
    Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="always")
]
RateOut = Annotated[
    Decimal, PlainSerializer(lambda v: f"{v:.6f}", return_type=str, when_used="always")
]
# Input money: positive, at most 2 decimal places. 10.005 is rejected, never silently rounded.
MoneyIn = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
MoneyInOrZero = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
RateIn = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6)]


class Page(BaseModel, Generic[T]):
    total: int
    page: int
    page_size: int
    items: list[T]


# ---- auth / users -------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=160)
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: str
    role: Role
    is_active: bool
    created_at: datetime


class SessionOut(BaseModel):
    user: UserOut


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    role: Role = Role.VIEWER


class UserUpdate(BaseModel):
    role: Optional[Role] = None
    is_active: Optional[bool] = None


# ---- accounts -----------------------------------------------------------------------------
class AccountCreate(BaseModel):
    code: str = Field(pattern=r"^[0-9]{4,10}$")
    name: str = Field(min_length=2, max_length=120)
    account_type: AccountType


class AccountOut(BaseModel):
    id: int
    code: str
    name: str
    account_type: AccountType
    is_active: bool
    balance: Money


# ---- journal ------------------------------------------------------------------------------
class LineIn(BaseModel):
    account_id: int
    debit: MoneyInOrZero = Decimal("0")
    credit: MoneyInOrZero = Decimal("0")

    @model_validator(mode="after")
    def _one_sided(self) -> "LineIn":
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each line must have exactly one of debit or credit greater than zero")
        return self


class JournalEntryCreate(BaseModel):
    description: str = Field(min_length=3, max_length=500)
    entry_date: date = Field(default_factory=lambda: clock.today())
    lines: list[LineIn] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def _balanced(self) -> "JournalEntryCreate":
        debits = sum((line.debit for line in self.lines), Decimal("0"))
        credits = sum((line.credit for line in self.lines), Decimal("0"))
        if debits != credits:
            raise ValueError(f"entry is not balanced: debits {debits} != credits {credits}")
        return self


class ReverseRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class LedgerLineOut(BaseModel):
    account_id: int
    account_code: str
    account_name: str
    debit: Money
    credit: Money


class JournalEntryOut(BaseModel):
    id: int
    reference_no: str
    entry_date: date
    accounting_period: str
    description: str
    source: str
    posted_by: str
    created_at: datetime
    reverses_entry_id: Optional[int]
    is_reversed: bool
    lines: list[LedgerLineOut]


# ---- invoices -----------------------------------------------------------------------------
class InvoiceCreate(BaseModel):
    client_name: str = Field(min_length=2, max_length=160)
    client_email: Optional[EmailStr] = None
    currency: str = Field(default_factory=lambda: settings.BASE_CURRENCY, pattern=r"^[A-Z]{3}$")
    exchange_rate: RateIn = Decimal("1")
    amount: MoneyIn
    tax_rate: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)] = Decimal("0")
    due_date: date


class PaymentCreate(BaseModel):
    amount: MoneyIn
    payment_rate: Optional[RateIn] = None
    paid_on: Optional[date] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: Money
    payment_rate: RateOut
    cash_base: Money
    ar_cleared_base: Money
    fx_difference: Money
    paid_on: date
    recorded_by: str
    journal_entry_id: int


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    client_name: str
    client_email: Optional[str]
    currency: str
    exchange_rate: RateOut
    amount: Money
    tax_rate: Money
    tax_amount: Money
    total_amount: Money
    amount_paid: Money
    balance_due: Money
    base_total: Money
    status: str
    issue_date: date
    due_date: date
    created_by: str


class InvoiceDetailOut(InvoiceOut):
    payments: list[PaymentOut]


# ---- periods ------------------------------------------------------------------------------
class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: str = Field(pattern=PERIOD_PATTERN)
    status: str
    closed_by: Optional[str]
    closed_at: Optional[datetime]


# ---- audit --------------------------------------------------------------------------------
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    actor: str
    action: str
    entity: str
    details: str
    created_at: datetime
    row_hash: str


class AuditVerifyOut(BaseModel):
    valid: bool
    checked: int
    first_invalid_seq: Optional[int]

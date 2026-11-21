from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import clock
from app.core.database import Base
from app.core.enums import InvoiceStatus, PeriodStatus, Role
from app.core.errors import AppendOnlyViolation

MONEY = Numeric(14, 2)
RATE = Numeric(12, 6)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=Role.VIEWER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_email_attempted_at", "email", "attempted_at"),
        Index("ix_login_attempts_ip_attempted_at", "ip", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254))
    ip: Mapped[str] = mapped_column(String(45))
    succeeded: Mapped[bool] = mapped_column(Boolean)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    account_type: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)
    # No stored balance: balances are always derived from ledger_lines, so they cannot drift.


class AccountingPeriod(Base):
    """Only closed/reopened periods have a row; a missing row means the period is open."""

    __tablename__ = "accounting_periods"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default=PeriodStatus.OPEN.value)
    closed_by: Mapped[Optional[str]] = mapped_column(String(254))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class DocumentSequence(Base):
    __tablename__ = "document_sequences"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference_no: Mapped[str] = mapped_column(String(20), unique=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    accounting_period: Mapped[str] = mapped_column(String(7), index=True)
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))
    posted_by: Mapped[str] = mapped_column(String(254))
    # Unique: the database itself guarantees an entry can be reversed at most once.
    reverses_entry_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)

    lines: Mapped[list["LedgerLine"]] = relationship(
        back_populates="entry", order_by="LedgerLine.id"
    )


class JournalEntrySeal(Base):
    """Immutable marker that closes a journal entry to further ledger-line inserts."""

    __tablename__ = "journal_entry_seals"

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"), primary_key=True
    )
    sealed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)


class LedgerLine(Base):
    __tablename__ = "ledger_lines"
    __table_args__ = (
        CheckConstraint(
            "(debit > 0 AND credit = 0) OR (debit = 0 AND credit > 0)", name="one_sided"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(20), unique=True)
    client_name: Mapped[str] = mapped_column(String(160))
    client_email: Mapped[Optional[str]] = mapped_column(String(254))
    currency: Mapped[str] = mapped_column(String(3))
    exchange_rate: Mapped[Decimal] = mapped_column(RATE)
    amount: Mapped[Decimal] = mapped_column(MONEY)  # net, invoice currency
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    tax_amount: Mapped[Decimal] = mapped_column(MONEY)
    total_amount: Mapped[Decimal] = mapped_column(MONEY)
    base_total: Mapped[Decimal] = mapped_column(MONEY)  # total in base currency at issue rate
    base_settled: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"))
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(20), default=InvoiceStatus.ISSUED.value)
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    created_by: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)
    # Optimistic concurrency: a stale write raises StaleDataError, surfaced as HTTP 409.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version_id}


class InvoicePayment(Base):
    __tablename__ = "invoice_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # invoice currency
    payment_rate: Mapped[Decimal] = mapped_column(RATE)
    cash_base: Mapped[Decimal] = mapped_column(MONEY)
    ar_cleared_base: Mapped[Decimal] = mapped_column(MONEY)
    fx_difference: Mapped[Decimal] = mapped_column(MONEY)  # signed: + gain, - loss
    paid_on: Mapped[date] = mapped_column(Date)
    recorded_by: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)


class AuditLog(Base):
    """Append-only and hash-chained: each row commits to the previous row's hash."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True)
    actor: Mapped[str] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(60))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyRecord(Base):
    """Durable response cache for retried financial POST requests."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor", "operation", "key", name="uq_idempotency_actor_operation_key"),
        Index("ix_idempotency_records_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(254))
    operation: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=clock.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


def _forbid_mutation(_mapper: Any, _connection: Any, target: Any) -> None:
    raise AppendOnlyViolation(f"{type(target).__name__} rows are append-only")


for _model in (AuditLog, LedgerLine, JournalEntry, JournalEntrySeal):
    event.listen(_model, "before_update", _forbid_mutation)
    event.listen(_model, "before_delete", _forbid_mutation)

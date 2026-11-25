from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.currencies import is_active_currency_code
from app.core.constants import ACC_AR, ACC_CASH, ACC_FX, ACC_REVENUE, ACC_TAX_PAYABLE, SEQ_INVOICE
from app.core.enums import OVERDUE, EntrySource, InvoiceStatus
from app.core.errors import (
    ConflictError,
    LedgerValidationError,
    NotFoundError,
    SystemAccountMissing,
)
from app.core.money import ZERO, money
from app.models import Account, Invoice, InvoicePayment
from app.schemas import InvoiceDetailOut, InvoiceOut, PaymentOut
from app.services.audit import log_audit
from app.services.ledger import LineInput, post_entry, reverse_entry
from app.services.sequences import next_value

_OPEN_STATUSES = (InvoiceStatus.ISSUED.value, InvoiceStatus.PARTIALLY_PAID.value)


@dataclass(frozen=True)
class Settlement:
    ar_cleared: Decimal  # base-currency receivable removed
    cash: Decimal  # base-currency cash received
    fx_diff: Decimal  # cash - ar_cleared; + realized gain, - realized loss


def plan_settlement(
    *,
    total: Decimal,
    base_total: Decimal,
    base_settled: Decimal,
    paid_before: Decimal,
    amount: Decimal,
    invoice_rate: Decimal,
    payment_rate: Decimal,
) -> Settlement:
    """Pure money maths for one payment.

    Receivable is cleared at the *invoice* rate, cash is received at the *payment* rate, and the
    difference is a realized FX gain/loss. The final payment clears exactly the remaining base
    receivable, so per-payment rounding can never leave a stray cent in AR.
    """
    remaining_base = base_total - base_settled
    if amount == total - paid_before:
        ar_cleared = remaining_base
    else:
        ar_cleared = min(money(amount * invoice_rate), remaining_base)
    cash = money(amount * payment_rate)
    return Settlement(ar_cleared=ar_cleared, cash=cash, fx_diff=cash - ar_cleared)


def effective_status(invoice: Invoice, today: date) -> str:
    if invoice.status in _OPEN_STATUSES and invoice.due_date < today:
        return OVERDUE
    return invoice.status


def _system_account(db: Session, code: str) -> Account:
    account = db.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise SystemAccountMissing(f"Required system account {code} is missing")
    return account


def to_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        invoice_number=invoice.invoice_number,
        client_name=invoice.client_name,
        client_email=invoice.client_email,
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,
        amount=invoice.amount,
        tax_rate=invoice.tax_rate,
        tax_amount=invoice.tax_amount,
        total_amount=invoice.total_amount,
        amount_paid=invoice.amount_paid,
        balance_due=invoice.total_amount - invoice.amount_paid,
        base_total=invoice.base_total,
        status=effective_status(invoice, clock.today()),
        issue_date=invoice.issue_date,
        due_date=invoice.due_date,
        created_by=invoice.created_by,
    )


def to_detail(db: Session, invoice: Invoice) -> InvoiceDetailOut:
    payments = db.scalars(
        select(InvoicePayment)
        .where(InvoicePayment.invoice_id == invoice.id)
        .order_by(InvoicePayment.id)
    )
    return InvoiceDetailOut(
        **to_out(invoice).model_dump(),
        payments=[PaymentOut.model_validate(p) for p in payments],
    )


def create_invoice(
    db: Session,
    *,
    client_name: str,
    client_email: Optional[str],
    currency: str,
    exchange_rate: Decimal,
    amount: Decimal,
    tax_rate: Decimal,
    due_date: date,
    actor: str,
) -> Invoice:
    if not is_active_currency_code(currency):
        raise LedgerValidationError(
            f"'{currency}' is not an active ISO 4217 currency code"
        )
    today = clock.today()
    if due_date < today:
        raise LedgerValidationError("due_date cannot be in the past")
    if currency == settings.BASE_CURRENCY:
        if exchange_rate != 1:
            raise LedgerValidationError(f"exchange_rate must be 1 for {currency} (base currency)")
        exchange_rate = Decimal("1")

    ar = _system_account(db, ACC_AR)
    revenue = _system_account(db, ACC_REVENUE)
    tax_acc = _system_account(db, ACC_TAX_PAYABLE)

    tax_amount = money(amount * tax_rate / Decimal(100))
    total = amount + tax_amount
    base_net = money(amount * exchange_rate)
    base_tax = money(tax_amount * exchange_rate)
    base_total = base_net + base_tax

    number = next_value(db, SEQ_INVOICE)
    invoice_number = f"INV-{number:06d}"

    lines = [LineInput(ar.id, debit=base_total), LineInput(revenue.id, credit=base_net)]
    if base_tax > 0:
        lines.append(LineInput(tax_acc.id, credit=base_tax))
    entry = post_entry(
        db,
        entry_date=today,
        description=f"Invoice {invoice_number} to {client_name}",
        lines=lines,
        actor=actor,
        source=EntrySource.INVOICE,
    )
    invoice = Invoice(
        invoice_number=invoice_number,
        client_name=client_name,
        client_email=client_email,
        currency=currency,
        exchange_rate=exchange_rate,
        amount=amount,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total_amount=total,
        base_total=base_total,
        base_settled=ZERO,
        amount_paid=ZERO,
        status=InvoiceStatus.ISSUED.value,
        issue_date=today,
        due_date=due_date,
        journal_entry_id=entry.id,
        created_by=actor,
    )
    db.add(invoice)
    db.flush()
    log_audit(db, actor, "INVOICE_CREATED", invoice_number, f"{currency} {total:.2f}")
    return invoice


def _locked_invoice(db: Session, invoice_id: int) -> Invoice:
    invoice = db.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())
    if invoice is None:
        raise NotFoundError("Invoice not found")
    return invoice


def get_invoice(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFoundError("Invoice not found")
    return invoice


def record_payment(
    db: Session,
    invoice_id: int,
    *,
    amount: Decimal,
    payment_rate: Optional[Decimal],
    paid_on: Optional[date],
    actor: str,
) -> Invoice:
    invoice = _locked_invoice(db, invoice_id)  # row lock on PostgreSQL + optimistic version check
    if invoice.status == InvoiceStatus.VOID.value:
        raise ConflictError("Invoice is void")
    if invoice.status == InvoiceStatus.PAID.value:
        raise ConflictError("Invoice is already fully paid")
    remaining = invoice.total_amount - invoice.amount_paid
    if amount > remaining:
        raise LedgerValidationError(f"Payment exceeds the outstanding balance of {remaining:.2f}")
    paid_on = paid_on or clock.today()
    if paid_on > clock.today():
        raise LedgerValidationError("paid_on cannot be in the future")

    if invoice.currency == settings.BASE_CURRENCY:
        rate = Decimal("1")
    else:
        rate = payment_rate or invoice.exchange_rate
    plan = plan_settlement(
        total=invoice.total_amount,
        base_total=invoice.base_total,
        base_settled=invoice.base_settled,
        paid_before=invoice.amount_paid,
        amount=amount,
        invoice_rate=invoice.exchange_rate,
        payment_rate=rate,
    )
    if plan.cash <= 0:
        raise LedgerValidationError("Payment is too small to convert to the base currency")

    cash = _system_account(db, ACC_CASH)
    lines = [LineInput(cash.id, debit=plan.cash)]
    if plan.ar_cleared > 0:
        lines.append(LineInput(_system_account(db, ACC_AR).id, credit=plan.ar_cleared))
    if plan.fx_diff != 0:
        fx = _system_account(db, ACC_FX)
        if plan.fx_diff > 0:
            lines.append(LineInput(fx.id, credit=plan.fx_diff))
        else:
            lines.append(LineInput(fx.id, debit=-plan.fx_diff))
    entry = post_entry(
        db,
        entry_date=paid_on,
        description=f"Payment on {invoice.invoice_number}",
        lines=lines,
        actor=actor,
        source=EntrySource.PAYMENT,
    )
    db.add(
        InvoicePayment(
            invoice_id=invoice.id,
            journal_entry_id=entry.id,
            amount=amount,
            payment_rate=rate,
            cash_base=plan.cash,
            ar_cleared_base=plan.ar_cleared,
            fx_difference=plan.fx_diff,
            paid_on=paid_on,
            recorded_by=actor,
        )
    )
    invoice.amount_paid = invoice.amount_paid + amount
    invoice.base_settled = invoice.base_settled + plan.ar_cleared
    fully_paid = invoice.amount_paid == invoice.total_amount
    invoice.status = (InvoiceStatus.PAID if fully_paid else InvoiceStatus.PARTIALLY_PAID).value
    db.flush()
    log_audit(
        db,
        actor,
        "PAYMENT_RECORDED",
        invoice.invoice_number,
        f"{invoice.currency} {amount:.2f} received, status {invoice.status}",
    )
    return invoice


def void_invoice(db: Session, invoice_id: int, *, actor: str) -> Invoice:
    invoice = _locked_invoice(db, invoice_id)
    if invoice.status == InvoiceStatus.VOID.value:
        raise ConflictError("Invoice is already void")
    if invoice.amount_paid > 0:
        raise ConflictError("Invoices with recorded payments cannot be voided")
    reverse_entry(
        db,
        invoice.journal_entry_id,
        reason=f"Void {invoice.invoice_number}",
        actor=actor,
        allow_system=True,
    )
    invoice.status = InvoiceStatus.VOID.value
    db.flush()
    log_audit(db, actor, "INVOICE_VOIDED", invoice.invoice_number, "Invoice voided")
    return invoice


def _status_filter(status: str, today: date):  # type: ignore[no-untyped-def]
    if status == OVERDUE:
        return Invoice.status.in_(_OPEN_STATUSES) & (Invoice.due_date < today)
    if status in _OPEN_STATUSES:
        return (Invoice.status == status) & (Invoice.due_date >= today)
    return Invoice.status == status


def list_invoices(
    db: Session, *, page: int, page_size: int, status: Optional[str] = None
) -> tuple[int, list[Invoice]]:
    today = clock.today()
    base = select(Invoice)
    count = select(func.count()).select_from(Invoice)
    if status:
        condition = _status_filter(status, today)
        base = base.where(condition)
        count = count.where(condition)
    total = db.scalar(count) or 0
    rows = list(
        db.scalars(base.order_by(Invoice.id.desc()).limit(page_size).offset((page - 1) * page_size))
    )
    return total, rows


def count_overdue(db: Session) -> int:
    condition = _status_filter(OVERDUE, clock.today())
    return db.scalar(select(func.count()).select_from(Invoice).where(condition)) or 0

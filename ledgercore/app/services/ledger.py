from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.core.constants import SEQ_JOURNAL
from app.core.enums import DEBIT_NORMAL, EntrySource
from app.core.errors import ConflictError, LedgerValidationError, NotFoundError
from app.core.money import ZERO, money
from app.models import Account, JournalEntry, JournalEntrySeal, LedgerLine
from app.services.audit import log_audit
from app.services.periods import ensure_period_open
from app.services.sequences import next_value


@dataclass(frozen=True)
class LineInput:
    account_id: int
    debit: Decimal = ZERO
    credit: Decimal = ZERO


def validate_lines(lines: Sequence[LineInput]) -> None:
    """Defence in depth: the API schema checks this too, but the service never trusts callers."""
    if len(lines) < 2:
        raise LedgerValidationError("a journal entry needs at least two lines")
    for line in lines:
        if line.debit != money(line.debit) or line.credit != money(line.credit):
            raise LedgerValidationError("amounts must have at most 2 decimal places")
        if line.debit < 0 or line.credit < 0 or (line.debit > 0) == (line.credit > 0):
            raise LedgerValidationError("each line must have exactly one positive side")
    debits = sum((line.debit for line in lines), ZERO)
    credits = sum((line.credit for line in lines), ZERO)
    if debits != credits:
        raise LedgerValidationError(f"entry is not balanced: debits {debits} != credits {credits}")


def post_entry(
    db: Session,
    *,
    entry_date: date,
    description: str,
    lines: Sequence[LineInput],
    actor: str,
    source: EntrySource = EntrySource.MANUAL,
    reverses_entry_id: Optional[int] = None,
) -> JournalEntry:
    """Post a balanced entry atomically with the caller's transaction (does not commit)."""
    validate_lines(lines)
    period = entry_date.strftime("%Y-%m")
    ensure_period_open(db, period)

    ids = {line.account_id for line in lines}
    accounts = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))}
    missing = ids - accounts.keys()
    if missing:
        raise NotFoundError(f"Account(s) not found: {sorted(missing)}")
    if reverses_entry_id is None:
        inactive = sorted(a.code for a in accounts.values() if not a.is_active)
        if inactive:
            raise ConflictError(f"Inactive account(s): {', '.join(inactive)}")

    number = next_value(db, SEQ_JOURNAL)
    entry = JournalEntry(
        reference_no=f"JE-{number:06d}",
        entry_date=entry_date,
        accounting_period=period,
        description=description,
        source=source.value,
        posted_by=actor,
        reverses_entry_id=reverses_entry_id,
    )
    entry.lines = [
        LedgerLine(account_id=line.account_id, debit=line.debit, credit=line.credit)
        for line in lines
    ]
    db.add(entry)
    db.flush()
    # A seal is an immutable database marker. Once it exists, triggers reject any later
    # INSERT into ledger_lines for this entry, including inserts made through raw SQL.
    db.add(JournalEntrySeal(entry_id=entry.id, sealed_at=clock.utcnow()))
    db.flush()
    total = sum((line.debit for line in lines), ZERO)
    log_audit(
        db, actor, "JOURNAL_POSTED", entry.reference_no, f"{source.value} entry, total {total:.2f}"
    )
    return entry


def reverse_entry(
    db: Session,
    entry_id: int,
    *,
    reason: str,
    actor: str,
    allow_system: bool = False,
) -> JournalEntry:
    original = db.scalar(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id)
        .options(selectinload(JournalEntry.lines))
        .with_for_update()
    )
    if original is None:
        raise NotFoundError("Journal entry not found")
    if original.reverses_entry_id is not None:
        raise ConflictError("A reversal entry cannot itself be reversed")
    if original.source in ("INVOICE", "PAYMENT") and not allow_system:
        raise ConflictError("System-generated entries must be corrected through the invoice flow")
    if db.scalar(select(JournalEntry.id).where(JournalEntry.reverses_entry_id == entry_id)):
        raise ConflictError("Journal entry has already been reversed")

    flipped = [
        LineInput(line.account_id, debit=line.credit, credit=line.debit) for line in original.lines
    ]
    try:
        return post_entry(
            db,
            entry_date=clock.today(),
            description=f"REVERSAL of {original.reference_no}: {reason}"[:500],
            lines=flipped,
            actor=actor,
            source=EntrySource.REVERSAL,
            reverses_entry_id=original.id,
        )
    except IntegrityError as exc:  # concurrent reversal lost the unique-constraint race
        raise ConflictError("Journal entry has already been reversed") from exc


def get_entry(db: Session, entry_id: int) -> JournalEntry:
    entry = db.scalar(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id)
        .options(selectinload(JournalEntry.lines).selectinload(LedgerLine.account))
    )
    if entry is None:
        raise NotFoundError("Journal entry not found")
    return entry


def list_entries(
    db: Session,
    *,
    page: int,
    page_size: int,
    period: Optional[str] = None,
    source: Optional[str] = None,
) -> tuple[int, list[JournalEntry], set[int]]:
    base = select(JournalEntry)
    count = select(func.count()).select_from(JournalEntry)
    if period:
        base = base.where(JournalEntry.accounting_period == period)
        count = count.where(JournalEntry.accounting_period == period)
    if source:
        base = base.where(JournalEntry.source == source)
        count = count.where(JournalEntry.source == source)
    total = db.scalar(count) or 0
    entries = list(
        db.scalars(
            base.options(selectinload(JournalEntry.lines).selectinload(LedgerLine.account))
            .order_by(JournalEntry.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    ids = [e.id for e in entries]
    reversed_ids: set[int] = set()
    if ids:
        reversed_ids = {
            rid
            for rid in db.scalars(
                select(JournalEntry.reverses_entry_id).where(JournalEntry.reverses_entry_id.in_(ids))
            )
            if rid is not None
        }
    return total, entries, reversed_ids


def ledger_totals(
    db: Session, *, date_from: Optional[date] = None, date_to: Optional[date] = None
) -> dict[int, tuple[Decimal, Decimal]]:
    """account_id -> (sum of debits, sum of credits), optionally restricted by entry date."""
    stmt = (
        select(LedgerLine.account_id, func.sum(LedgerLine.debit), func.sum(LedgerLine.credit))
        .join(JournalEntry, JournalEntry.id == LedgerLine.entry_id)
        .group_by(LedgerLine.account_id)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    return {
        account_id: (money(debit or 0), money(credit or 0))
        for account_id, debit, credit in db.execute(stmt)
    }


def natural_balance(account_type: str, debit: Decimal, credit: Decimal) -> Decimal:
    return debit - credit if account_type in DEBIT_NORMAL else credit - debit


def account_balances(
    db: Session, *, date_from: Optional[date] = None, date_to: Optional[date] = None
) -> list[tuple[Account, Decimal]]:
    totals = ledger_totals(db, date_from=date_from, date_to=date_to)
    accounts = db.scalars(select(Account).order_by(Account.code))
    return [
        (acc, natural_balance(acc.account_type, *totals.get(acc.id, (ZERO, ZERO))))
        for acc in accounts
    ]

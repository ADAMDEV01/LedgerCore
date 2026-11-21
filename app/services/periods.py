"""Accounting-period operations serialized against journal posting."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.enums import PeriodStatus
from app.core.errors import ConflictError
from app.models import AccountingPeriod
from app.services.audit import log_audit


def _locked_period(db: Session, period: str) -> AccountingPeriod:
    """Create the OPEN row if absent, then lock it for this transaction.

    Locking an absent row is not possible with SELECT FOR UPDATE. The conflict-safe insert
    ensures close/reopen/posting all serialize on the same row, including the first operation
    ever performed in a month.
    """
    values = {"period": period, "status": PeriodStatus.OPEN.value}
    dialect = db.get_bind().dialect.name
    statement: Any
    if dialect == "postgresql":
        statement = pg_insert(AccountingPeriod).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(AccountingPeriod).values(**values)
    else:
        raise RuntimeError(f"Accounting-period locking is not implemented for {dialect}")
    db.execute(statement.on_conflict_do_nothing(index_elements=["period"]))
    row = db.scalar(
        select(AccountingPeriod).where(AccountingPeriod.period == period).with_for_update()
    )
    if row is None:
        raise RuntimeError("Accounting period row could not be locked")
    return row


def ensure_period_open(db: Session, period: str) -> None:
    row = _locked_period(db, period)
    if row.status == PeriodStatus.CLOSED.value:
        raise ConflictError(f"Accounting period {period} is closed")


def close_period(db: Session, period: str, actor: str) -> AccountingPeriod:
    row = _locked_period(db, period)
    if row.status == PeriodStatus.CLOSED.value:
        raise ConflictError(f"Accounting period {period} is already closed")
    row.status = PeriodStatus.CLOSED.value
    row.closed_by = actor
    row.closed_at = clock.utcnow()
    db.flush()
    log_audit(db, actor, "PERIOD_CLOSED", f"period:{period}", f"Closed period {period}")
    return row


def reopen_period(db: Session, period: str, actor: str) -> AccountingPeriod:
    row = _locked_period(db, period)
    if row.status != PeriodStatus.CLOSED.value:
        raise ConflictError(f"Accounting period {period} is not closed")
    row.status = PeriodStatus.OPEN.value
    row.closed_by = actor
    row.closed_at = clock.utcnow()
    db.flush()
    log_audit(db, actor, "PERIOD_REOPENED", f"period:{period}", f"Reopened period {period}")
    return row


def list_periods(db: Session) -> list[AccountingPeriod]:
    return list(db.scalars(select(AccountingPeriod).order_by(AccountingPeriod.period.desc())))

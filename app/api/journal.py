from typing import Optional

from fastapi import APIRouter, Header, Query
from sqlalchemy import select

from app.core.constants import PERIOD_PATTERN
from app.core.deps import CfoUser, CurrentUser, DbSession, LedgerWriter
from app.core.enums import EntrySource
from app.models import JournalEntry
from app.schemas import JournalEntryCreate, JournalEntryOut, LedgerLineOut, Page, ReverseRequest
from app.services import idempotency, ledger

router = APIRouter(prefix="/journal-entries", tags=["journal"])


def to_out(entry: JournalEntry, reversed_ids: set[int]) -> JournalEntryOut:
    return JournalEntryOut(
        id=entry.id,
        reference_no=entry.reference_no,
        entry_date=entry.entry_date,
        accounting_period=entry.accounting_period,
        description=entry.description,
        source=entry.source,
        posted_by=entry.posted_by,
        created_at=entry.created_at,
        reverses_entry_id=entry.reverses_entry_id,
        is_reversed=entry.id in reversed_ids,
        lines=[
            LedgerLineOut(
                account_id=line.account_id,
                account_code=line.account.code,
                account_name=line.account.name,
                debit=line.debit,
                credit=line.credit,
            )
            for line in entry.lines
        ],
    )


@router.get("", response_model=Page[JournalEntryOut])
def list_entries(
    db: DbSession,
    _: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
    source: Optional[EntrySource] = None,
) -> Page[JournalEntryOut]:
    total, entries, reversed_ids = ledger.list_entries(
        db, page=page, page_size=page_size, period=period, source=source.value if source else None
    )
    items = [to_out(e, reversed_ids) for e in entries]
    return Page[JournalEntryOut](total=total, page=page, page_size=page_size, items=items)


@router.get("/{entry_id}", response_model=JournalEntryOut)
def get_entry(entry_id: int, db: DbSession, _: CurrentUser) -> JournalEntryOut:
    entry = ledger.get_entry(db, entry_id)
    already_reversed = db.scalar(
        select(JournalEntry.id).where(JournalEntry.reverses_entry_id == entry.id)
    )
    return to_out(entry, {entry.id} if already_reversed else set())


@router.post("", response_model=JournalEntryOut, status_code=201)
def post_entry(
    payload: JournalEntryCreate,
    db: DbSession,
    user: LedgerWriter,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=255),
) -> JournalEntryOut:
    record, replayed = idempotency.begin_request(
        db,
        actor=user.email,
        operation="journal.create",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    if replayed:
        return JournalEntryOut.model_validate(record.response_payload)
    entry = ledger.post_entry(
        db,
        entry_date=payload.entry_date,
        description=payload.description,
        lines=[ledger.LineInput(ln.account_id, ln.debit, ln.credit) for ln in payload.lines],
        actor=user.email,
    )
    output = to_out(entry, set())
    idempotency.complete_request(db, record, output.model_dump(mode="json"))
    db.commit()
    return output


@router.post("/{entry_id}/reverse", response_model=JournalEntryOut, status_code=201)
def reverse_entry(
    entry_id: int,
    payload: ReverseRequest,
    db: DbSession,
    user: CfoUser,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=255),
) -> JournalEntryOut:
    record, replayed = idempotency.begin_request(
        db,
        actor=user.email,
        operation=f"journal.reverse:{entry_id}",
        key=idempotency_key,
        payload={"entry_id": entry_id, **payload.model_dump(mode="json")},
    )
    if replayed:
        return JournalEntryOut.model_validate(record.response_payload)
    reversal = ledger.reverse_entry(db, entry_id, reason=payload.reason, actor=user.email)
    output = to_out(reversal, set())
    idempotency.complete_request(db, record, output.model_dump(mode="json"))
    db.commit()
    return output

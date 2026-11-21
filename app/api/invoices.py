from typing import Optional

from fastapi import APIRouter, Header, Query

from app.core.deps import CfoUser, CurrentUser, DbSession, LedgerWriter
from app.schemas import InvoiceCreate, InvoiceDetailOut, InvoiceOut, Page, PaymentCreate
from app.services import idempotency
from app.services import invoices as svc

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.get("", response_model=Page[InvoiceOut])
def list_invoices(
    db: DbSession,
    _: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    status: Optional[str] = Query(None, pattern=r"^(ISSUED|PARTIALLY_PAID|PAID|VOID|OVERDUE)$"),
) -> Page[InvoiceOut]:
    total, rows = svc.list_invoices(db, page=page, page_size=page_size, status=status)
    return Page[InvoiceOut](
        total=total, page=page, page_size=page_size, items=[svc.to_out(i) for i in rows]
    )


@router.get("/{invoice_id}", response_model=InvoiceDetailOut)
def get_invoice(invoice_id: int, db: DbSession, _: CurrentUser) -> InvoiceDetailOut:
    return svc.to_detail(db, svc.get_invoice(db, invoice_id))


@router.post("", response_model=InvoiceOut, status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    db: DbSession,
    user: LedgerWriter,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=255),
) -> InvoiceOut:
    record, replayed = idempotency.begin_request(
        db,
        actor=user.email,
        operation="invoice.create",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    if replayed:
        return InvoiceOut.model_validate(record.response_payload)
    invoice = svc.create_invoice(
        db,
        client_name=payload.client_name,
        client_email=payload.client_email,
        currency=payload.currency,
        exchange_rate=payload.exchange_rate,
        amount=payload.amount,
        tax_rate=payload.tax_rate,
        due_date=payload.due_date,
        actor=user.email,
    )
    output = svc.to_out(invoice)
    idempotency.complete_request(db, record, output.model_dump(mode="json"))
    db.commit()
    return output


@router.post("/{invoice_id}/payments", response_model=InvoiceDetailOut, status_code=201)
def record_payment(
    invoice_id: int,
    payload: PaymentCreate,
    db: DbSession,
    user: LedgerWriter,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=255),
) -> InvoiceDetailOut:
    record, replayed = idempotency.begin_request(
        db,
        actor=user.email,
        operation=f"invoice.payment:{invoice_id}",
        key=idempotency_key,
        payload={"invoice_id": invoice_id, **payload.model_dump(mode="json")},
    )
    if replayed:
        return InvoiceDetailOut.model_validate(record.response_payload)
    invoice = svc.record_payment(
        db,
        invoice_id,
        amount=payload.amount,
        payment_rate=payload.payment_rate,
        paid_on=payload.paid_on,
        actor=user.email,
    )
    output = svc.to_detail(db, invoice)
    idempotency.complete_request(db, record, output.model_dump(mode="json"))
    db.commit()
    return output


@router.post("/{invoice_id}/void", response_model=InvoiceOut)
def void_invoice(
    invoice_id: int,
    db: DbSession,
    user: CfoUser,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=255),
) -> InvoiceOut:
    record, replayed = idempotency.begin_request(
        db,
        actor=user.email,
        operation=f"invoice.void:{invoice_id}",
        key=idempotency_key,
        payload={"invoice_id": invoice_id},
    )
    if replayed:
        return InvoiceOut.model_validate(record.response_payload)
    invoice = svc.void_invoice(db, invoice_id, actor=user.email)
    output = svc.to_out(invoice)
    idempotency.complete_request(db, record, output.model_dump(mode="json"))
    db.commit()
    return output

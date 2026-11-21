from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.core.deps import CfoUser, CurrentUser, DbSession
from app.core.errors import ConflictError
from app.core.money import ZERO
from app.models import Account
from app.schemas import AccountCreate, AccountOut, Page
from app.services.audit import log_audit
from app.services.ledger import ledger_totals, natural_balance

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=Page[AccountOut])
def list_accounts(
    db: DbSession,
    _: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> Page[AccountOut]:
    total = db.scalar(select(func.count()).select_from(Account)) or 0
    accounts = db.scalars(
        select(Account).order_by(Account.code).limit(page_size).offset((page - 1) * page_size)
    )
    totals = ledger_totals(db)
    items = [
        AccountOut(
            id=a.id,
            code=a.code,
            name=a.name,
            account_type=a.account_type,  # type: ignore[arg-type]
            is_active=a.is_active,
            balance=natural_balance(a.account_type, *totals.get(a.id, (ZERO, ZERO))),
        )
        for a in accounts
    ]
    return Page[AccountOut](total=total, page=page, page_size=page_size, items=items)


@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: DbSession, actor: CfoUser) -> AccountOut:
    if db.scalar(select(Account.id).where(Account.code == payload.code)):
        raise ConflictError(f"Account code {payload.code} already exists")
    account = Account(code=payload.code, name=payload.name, account_type=payload.account_type.value)
    db.add(account)
    db.flush()
    log_audit(db, actor.email, "ACCOUNT_CREATED", account.code, account.name)
    db.commit()
    return AccountOut(
        id=account.id,
        code=account.code,
        name=account.name,
        account_type=payload.account_type,
        is_active=True,
        balance=ZERO,
    )

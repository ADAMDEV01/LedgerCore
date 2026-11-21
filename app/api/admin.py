from fastapi import APIRouter, Path, Query
from sqlalchemy import func, select

from app.core.constants import PERIOD_PATTERN
from app.core.deps import CfoUser, DbSession
from app.models import AuditLog
from app.schemas import AuditLogOut, AuditVerifyOut, Page, PeriodOut
from app.services import audit as audit_svc
from app.services import periods as period_svc

router = APIRouter(tags=["admin"])


@router.get("/audit-logs", response_model=Page[AuditLogOut])
def list_audit_logs(
    db: DbSession,
    _: CfoUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> Page[AuditLogOut]:
    total = db.scalar(select(func.count()).select_from(AuditLog)) or 0
    rows = db.scalars(
        select(AuditLog).order_by(AuditLog.seq.desc()).limit(page_size).offset((page - 1) * page_size)
    )
    return Page[AuditLogOut](
        total=total,
        page=page,
        page_size=page_size,
        items=[AuditLogOut.model_validate(r) for r in rows],
    )


@router.get("/audit-logs/verify", response_model=AuditVerifyOut)
def verify_audit_chain(db: DbSession, _: CfoUser) -> AuditVerifyOut:
    return AuditVerifyOut(**audit_svc.verify_chain(db))  # type: ignore[arg-type]


@router.get("/periods", response_model=list[PeriodOut])
def list_periods(db: DbSession, _: CfoUser) -> list[PeriodOut]:
    return [PeriodOut.model_validate(p) for p in period_svc.list_periods(db)]


@router.post("/periods/{period}/close", response_model=PeriodOut)
def close_period(
    db: DbSession, user: CfoUser, period: str = Path(pattern=PERIOD_PATTERN)
) -> PeriodOut:
    row = period_svc.close_period(db, period, user.email)
    db.commit()
    return PeriodOut.model_validate(row)


@router.post("/periods/{period}/reopen", response_model=PeriodOut)
def reopen_period(
    db: DbSession, user: CfoUser, period: str = Path(pattern=PERIOD_PATTERN)
) -> PeriodOut:
    row = period_svc.reopen_period(db, period, user.email)
    db.commit()
    return PeriodOut.model_validate(row)

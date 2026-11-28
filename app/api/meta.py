from fastapi import APIRouter
from sqlalchemy import select

from app.core import clock
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.models import AccountingPeriod

router = APIRouter(tags=["application settings"])


@router.get("/settings")
def application_settings() -> dict[str, str]:
    """Expose non-sensitive runtime settings needed to keep UI and API consistent."""
    return {"base_currency": settings.BASE_CURRENCY}


@router.get("/accounting-period")
def current_accounting_period(db: DbSession, user: CurrentUser) -> dict[str, str]:
    """Return the signed-in organization's current period; absent rows are open by design."""
    period = clock.today().strftime("%Y-%m")
    row = db.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.organization_id == user.organization_id,
            AccountingPeriod.period == period,
        )
    )
    return {"period": period, "status": row.status if row else "OPEN"}

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.core import clock
from app.core.deps import CurrentUser, DbSession
from app.core.errors import LedgerValidationError
from app.services import reports as svc

router = APIRouter(tags=["reports"])


@router.get("/reports/trial-balance")
def trial_balance(db: DbSession, _: CurrentUser, as_of: Optional[date] = None) -> dict[str, Any]:
    return svc.trial_balance(db, as_of)


@router.get("/reports/trial-balance/export")
def trial_balance_export(db: DbSession, _: CurrentUser, as_of: Optional[date] = None) -> Response:
    body = svc.trial_balance_csv(db, as_of)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trial_balance.csv"'},
    )


@router.get("/reports/income-statement")
def income_statement(
    db: DbSession,
    _: CurrentUser,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict[str, Any]:
    today = clock.today()
    start = date_from or today.replace(day=1)
    end = date_to or today
    if start > end:
        raise LedgerValidationError("date_from must not be after date_to")
    return svc.income_statement(db, start, end)


@router.get("/reports/balance-sheet")
def balance_sheet(db: DbSession, _: CurrentUser, as_of: Optional[date] = None) -> dict[str, Any]:
    return svc.balance_sheet(db, as_of)


@router.get("/dashboard/summary")
def dashboard_summary(
    db: DbSession, _: CurrentUser, months: int = Query(6, ge=1, le=12)
) -> dict[str, Any]:
    summary = svc.dashboard_summary(db)
    summary["monthly"] = svc.monthly_performance(db, months)
    return summary

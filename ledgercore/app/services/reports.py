import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.constants import ACC_AR, ACC_CASH
from app.core.enums import AccountType
from app.core.money import ZERO, fmt, money
from app.models import Account, JournalEntry, LedgerLine
from app.services.invoices import count_overdue
from app.services.ledger import account_balances, ledger_totals


def trial_balance(db: Session, as_of: Optional[date] = None) -> dict[str, Any]:
    totals = ledger_totals(db, date_to=as_of)
    rows: list[dict[str, str]] = []
    sum_debit = sum_credit = ZERO
    for account in db.scalars(select(Account).order_by(Account.code)):
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        net = debit - credit
        bal_debit = net if net > 0 else ZERO
        bal_credit = -net if net < 0 else ZERO
        sum_debit += bal_debit
        sum_credit += bal_credit
        rows.append(
            {
                "code": account.code,
                "name": account.name,
                "account_type": account.account_type,
                "debit": fmt(bal_debit),
                "credit": fmt(bal_credit),
            }
        )
    return {
        "as_of": (as_of or clock.today()).isoformat(),
        "rows": rows,
        "total_debit": fmt(sum_debit),
        "total_credit": fmt(sum_credit),
        "is_balanced": sum_debit == sum_credit,
    }


def _csv_safe(cell: str) -> str:
    """Neutralise spreadsheet formula injection in exported text."""
    return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


def trial_balance_csv(db: Session, as_of: Optional[date] = None) -> str:
    report = trial_balance(db, as_of)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Code", "Account", "Type", "Debit", "Credit"])
    for row in report["rows"]:
        writer.writerow(
            [row["code"], _csv_safe(row["name"]), row["account_type"], row["debit"], row["credit"]]
        )
    writer.writerow(["", "TOTAL", "", report["total_debit"], report["total_credit"]])
    return buffer.getvalue()


def income_statement(db: Session, date_from: date, date_to: date) -> dict[str, Any]:
    totals = ledger_totals(db, date_from=date_from, date_to=date_to)
    revenue: list[dict[str, str]] = []
    expenses: list[dict[str, str]] = []
    total_revenue = total_expenses = ZERO
    for account in db.scalars(select(Account).order_by(Account.code)):
        debit, credit = totals.get(account.id, (ZERO, ZERO))
        if account.account_type == AccountType.REVENUE.value:
            amount = credit - debit
            total_revenue += amount
            target = revenue
        elif account.account_type == AccountType.EXPENSE.value:
            amount = debit - credit
            total_expenses += amount
            target = expenses
        else:
            continue
        if amount != 0:
            target.append({"code": account.code, "name": account.name, "amount": fmt(amount)})
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "revenue": revenue,
        "expenses": expenses,
        "total_revenue": fmt(total_revenue),
        "total_expenses": fmt(total_expenses),
        "net_income": fmt(total_revenue - total_expenses),
    }


def balance_sheet(db: Session, as_of: Optional[date] = None) -> dict[str, Any]:
    sections: dict[str, list[dict[str, str]]] = {"assets": [], "liabilities": [], "equity": []}
    totals = {"assets": ZERO, "liabilities": ZERO, "equity": ZERO}
    earnings = ZERO
    key_for = {
        AccountType.ASSET.value: "assets",
        AccountType.LIABILITY.value: "liabilities",
        AccountType.EQUITY.value: "equity",
    }
    for account, balance in account_balances(db, date_to=as_of):
        if account.account_type == AccountType.REVENUE.value:
            earnings += balance
        elif account.account_type == AccountType.EXPENSE.value:
            earnings -= balance
        else:
            key = key_for[account.account_type]
            totals[key] += balance
            if balance != 0:
                sections[key].append(
                    {"code": account.code, "name": account.name, "amount": fmt(balance)}
                )
    total_equity = totals["equity"] + earnings
    return {
        "as_of": (as_of or clock.today()).isoformat(),
        **sections,
        "current_earnings": fmt(earnings),
        "total_assets": fmt(totals["assets"]),
        "total_liabilities": fmt(totals["liabilities"]),
        "total_equity": fmt(total_equity),
        "is_balanced": totals["assets"] == totals["liabilities"] + total_equity,
    }


def _recent_periods(count: int) -> list[str]:
    today = clock.today()
    year, month = today.year, today.month
    periods: list[str] = []
    for _ in range(count):
        periods.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return periods[::-1]


def monthly_performance(db: Session, months: int = 6) -> list[dict[str, str]]:
    stmt = (
        select(
            JournalEntry.accounting_period,
            Account.account_type,
            func.sum(LedgerLine.debit),
            func.sum(LedgerLine.credit),
        )
        .join(JournalEntry, JournalEntry.id == LedgerLine.entry_id)
        .join(Account, Account.id == LedgerLine.account_id)
        .where(Account.account_type.in_([AccountType.REVENUE.value, AccountType.EXPENSE.value]))
        .group_by(JournalEntry.accounting_period, Account.account_type)
    )
    revenue: dict[str, Decimal] = {}
    expenses: dict[str, Decimal] = {}
    for period, account_type, debit, credit in db.execute(stmt):
        debit, credit = money(debit or 0), money(credit or 0)
        if account_type == AccountType.REVENUE.value:
            revenue[period] = credit - debit
        else:
            expenses[period] = debit - credit
    return [
        {
            "period": p,
            "revenue": fmt(revenue.get(p, ZERO)),
            "expenses": fmt(expenses.get(p, ZERO)),
        }
        for p in _recent_periods(months)
    ]


def dashboard_summary(db: Session) -> dict[str, Any]:
    by_type = {t.value: ZERO for t in AccountType}
    by_code: dict[str, Decimal] = {}
    for account, balance in account_balances(db):
        by_type[account.account_type] += balance
        by_code[account.code] = balance
    net_income = by_type[AccountType.REVENUE.value] - by_type[AccountType.EXPENSE.value]
    balanced = by_type[AccountType.ASSET.value] == (
        by_type[AccountType.LIABILITY.value] + by_type[AccountType.EQUITY.value] + net_income
    )
    return {
        "cash": fmt(by_code.get(ACC_CASH, ZERO)),
        "accounts_receivable": fmt(by_code.get(ACC_AR, ZERO)),
        "total_revenue": fmt(by_type[AccountType.REVENUE.value]),
        "total_expenses": fmt(by_type[AccountType.EXPENSE.value]),
        "net_income": fmt(net_income),
        "overdue_invoices": count_overdue(db),
        "balance_sheet_balanced": balanced,
        "monthly": monthly_performance(db),
    }

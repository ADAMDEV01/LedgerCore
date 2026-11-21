from datetime import date, timedelta
from decimal import Decimal

from tests.helpers import API, acc, post_je


def test_trial_balance_balances_and_respects_as_of(cfo):
    yesterday = date.today() - timedelta(days=1)
    post_je(cfo, "1000", "3000", "1000.00", entry_date=yesterday)
    post_je(cfo, "1000", "4000", "300.00")
    now = cfo.get(f"{API}/reports/trial-balance").json()
    then = cfo.get(f"{API}/reports/trial-balance?as_of={yesterday.isoformat()}").json()
    assert now["is_balanced"] and now["total_debit"] == now["total_credit"] == "1300.00"
    assert then["is_balanced"] and then["total_debit"] == "1000.00"


def test_income_statement_computes_net_income(cfo):
    post_je(cfo, "1000", "4000", "1000.00")
    post_je(cfo, "5000", "1000", "400.00")
    report = cfo.get(f"{API}/reports/income-statement").json()
    assert (report["total_revenue"], report["total_expenses"], report["net_income"]) == ("1000.00", "400.00", "600.00")


def test_income_statement_rejects_inverted_ranges(cfo):
    response = cfo.get(f"{API}/reports/income-statement?date_from=2026-02-01&date_to=2026-01-01")
    assert response.status_code == 422


def test_balance_sheet_equation_holds_including_current_earnings(cfo):
    post_je(cfo, "1000", "3000", "5000.00")
    post_je(cfo, "1000", "4000", "1000.00")
    post_je(cfo, "5000", "1000", "400.00")
    sheet = cfo.get(f"{API}/reports/balance-sheet").json()
    assert sheet["total_assets"] == "5600.00"
    assert sheet["current_earnings"] == "600.00" and sheet["total_equity"] == "5600.00"
    assert sheet["is_balanced"] is True


def test_csv_export_neutralises_formula_injection(cfo):
    created = cfo.post(f"{API}/accounts", json={"code": "7000", "name": "=SUM(1+1)", "account_type": "EXPENSE"})
    assert created.status_code == 201
    response = cfo.get(f"{API}/reports/trial-balance/export")
    assert response.headers["content-type"].startswith("text/csv")
    assert "'=SUM(1+1)" in response.text
    assert response.text.splitlines()[0] == "Code,Account,Type,Debit,Credit"


def test_dashboard_summary_shape_and_values(cfo):
    post_je(cfo, "1000", "4000", "750.00")
    summary = cfo.get(f"{API}/dashboard/summary").json()
    assert summary["cash"] == "750.00" and summary["net_income"] == "750.00"
    assert summary["balance_sheet_balanced"] is True
    assert len(summary["monthly"]) == 6
    assert summary["monthly"][-1]["revenue"] == "750.00"


def test_duplicate_account_codes_are_rejected(cfo):
    assert cfo.post(f"{API}/accounts", json={"code": "1000", "name": "Dup", "account_type": "ASSET"}).status_code == 409
    assert acc("1000")


def test_balances_use_exact_decimal_arithmetic(cfo):
    for _ in range(10):
        post_je(cfo, "1000", "3000", "0.10")
    assert cfo.get(f"{API}/reports/trial-balance").json()["total_debit"] == "1.00"
    assert Decimal("0.10") * 10 == Decimal("1.00")

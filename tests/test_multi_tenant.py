from tests.helpers import API, make_client


def _register(client, company: str, email: str):
    response = client.post(
        f"{API}/auth/register",
        json={
            "company_name": company,
            "full_name": "Workspace Owner",
            "email": email,
            "password": "Strong-Workspace-Pass-2026",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf_token")
    return response.json()["user"]


def test_registration_creates_a_separate_cfo_workspace_with_default_accounts():
    client = make_client()
    user = _register(client, "Northwind Studio", "owner@northwind.test")

    assert user["role"] == "CFO"
    assert client.get(f"{API}/auth/me").json()["email"] == "owner@northwind.test"
    accounts = client.get(f"{API}/accounts?page_size=500").json()["items"]
    assert len(accounts) == 10
    assert {row["code"] for row in accounts} >= {"1000", "1100", "4000"}


def test_companies_can_reuse_account_codes_without_cross_workspace_visibility():
    northwind = make_client()
    contoso = make_client()
    _register(northwind, "Northwind Studio", "owner@northwind.test")
    _register(contoso, "Contoso Labs", "owner@contoso.test")

    for client in (northwind, contoso):
        response = client.post(
            f"{API}/accounts",
            json={"code": "7777", "name": "Workspace-specific account", "account_type": "ASSET"},
        )
        assert response.status_code == 201, response.text

    northwind_codes = {row["code"] for row in northwind.get(f"{API}/accounts?page_size=500").json()["items"]}
    contoso_codes = {row["code"] for row in contoso.get(f"{API}/accounts?page_size=500").json()["items"]}
    assert "7777" in northwind_codes and "7777" in contoso_codes
    assert northwind.get(f"{API}/users").json()[0]["email"] == "owner@northwind.test"
    assert contoso.get(f"{API}/users").json()[0]["email"] == "owner@contoso.test"

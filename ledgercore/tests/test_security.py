from tests.helpers import API


def test_security_headers_are_set_on_the_app_shell(anon):
    response = anon.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"]


def test_api_responses_are_never_cached(cfo):
    assert cfo.get(f"{API}/accounts").headers["cache-control"] == "no-store"


def test_interactive_docs_are_not_blocked_by_the_csp(anon):
    response = anon.get("/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_health_and_readiness(anon):
    assert anon.get("/health").json()["status"] == "ok"
    assert anon.get("/health/ready").json() == {"status": "ready"}


def test_static_frontend_has_no_inline_scripts_or_handlers(anon):
    html = anon.get("/").text
    assert "onclick=" not in html and " style=" not in html
    assert '<script src="/static/app.js"' in html

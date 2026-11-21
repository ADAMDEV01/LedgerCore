"""Browser-level checks for the shipped vanilla-JS application."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "browser-admin@ledgertest.io"
ADMIN_PASSWORD = "Browser-Test-Admin-2026!"
DEMO_EMAIL = "browser-viewer@ledgertest.io"
DEMO_PASSWORD = "Browser-Test-Viewer-2026!"


@pytest.fixture(scope="session")
def app_url(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("ledgercore-browser") / "browser.db"
    env = os.environ.copy()
    env.update(
        {
            "SECRET_KEY": "browser-test-secret-key-not-for-production-123456789",
            "ENVIRONMENT": "test",
            "COOKIE_SECURE": "false",
            "DATABASE_URL": f"sqlite:///{database}",
            "INITIAL_ADMIN_EMAIL": ADMIN_EMAIL,
            "INITIAL_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "DEMO_VIEWER_EMAIL": DEMO_EMAIL,
            "DEMO_VIEWER_PASSWORD": DEMO_PASSWORD,
            "SEED_ON_STARTUP": "true",
            "SEED_DEMO_DATA": "true",
        }
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("LedgerCore server exited before becoming ready")
            try:
                with urllib.request.urlopen(f"{url}/health/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("LedgerCore server did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="session")
def browser() -> Browser:
    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or shutil.which("chromium")
        browser = playwright.chromium.launch(executable_path=executable)
        yield browser
        browser.close()


def sign_in(page: Page, base_url: str, email: str, password: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("heading", name="Dashboard").wait_for()


def test_admin_can_post_a_balanced_entry_from_the_browser(browser: Browser, app_url: str) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    sign_in(page, app_url, ADMIN_EMAIL, ADMIN_PASSWORD)
    screenshots = ROOT / "docs" / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / "dashboard.png"), full_page=True)
    page.get_by_role("button", name="Journal", exact=True).click()
    page.get_by_role("button", name="New entry").click()

    page.locator("select[aria-label='Account']").nth(0).select_option(label="1000 · Operating Cash")
    page.locator("select[aria-label='Account']").nth(1).select_option(label="3000 · Owner's Capital")
    page.get_by_label("Debit").nth(0).fill("75.00")
    page.get_by_label("Credit").nth(1).fill("75.00")
    page.locator("#je-desc").fill("Browser verified journal entry")
    post_button = page.get_by_role("button", name="Post entry")
    assert post_button.is_enabled()
    post_button.click()

    page.get_by_text("Entry posted").wait_for()
    page.get_by_text("Browser verified journal entry").wait_for()
    assert page.locator("#proof-state").inner_text() == "Balanced"
    page.screenshot(path=str(screenshots / "journal-posted.png"), full_page=True)
    page.close()


def test_demo_viewer_is_read_only_and_mobile_layout_fits(browser: Browser, app_url: str) -> None:
    page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    sign_in(page, app_url, DEMO_EMAIL, DEMO_PASSWORD)
    assert page.locator("#role-banner").is_visible()
    page.get_by_role("button", name="Journal", exact=True).click()
    assert page.get_by_role("button", name="New entry").count() == 0
    page.get_by_role("button", name="Invoices", exact=True).click()
    assert page.get_by_role("button", name="New invoice").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.close()


def test_parallel_expired_requests_share_one_refresh_across_tabs(browser: Browser, app_url: str) -> None:
    """Two tabs with the same stale access cookie must perform one refresh rotation."""
    context = browser.new_context()
    first = context.new_page()
    second = context.new_page()
    sign_in(first, app_url, ADMIN_EMAIL, ADMIN_PASSWORD)
    second.goto(app_url, wait_until="domcontentloaded")
    second.get_by_role("heading", name="Dashboard").wait_for()

    # Keep the valid refresh/CSRF cookies but expire the access cookie in the shared jar.
    context.add_cookies([
        {
            "name": "access_token",
            "value": "deliberately-expired-access-token",
            "url": app_url,
            "httpOnly": True,
            "sameSite": "Strict",
        }
    ])

    refresh_requests = 0

    def count_refresh(route):  # type: ignore[no-untyped-def]
        nonlocal refresh_requests
        refresh_requests += 1
        time.sleep(0.2)  # make the overlap deterministic enough to expose duplicate refreshes
        route.continue_()

    context.route("**/api/v1/auth/refresh", count_refresh)
    for page in (first, second):
        page.evaluate(
            """() => {
              // Exercise the SPA API wrapper so the test covers its refresh-and-retry path.
              window.refreshProbe = window.api('/accounts')
                .then(() => 200)
                .catch(() => 0);
              return true;
            }"""
        )
    # Playwright's evaluate awaits the Promise; wait_for_function would use eval and
    # violate the app's intentionally strict script-src CSP.
    assert first.evaluate("window.refreshProbe") == 200
    assert second.evaluate("window.refreshProbe") == 200
    assert refresh_requests == 1
    context.close()

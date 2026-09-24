"""Browser-level E2E tests using Playwright.

These tests exercise the full server-rendered Django UI against a running
Docker Compose stack. They require the stack to be up (docker compose up -d)
with bootstrap_platform already run.

Run locally:
    docker compose up -d
    docker compose exec web python manage.py bootstrap_platform \
        --username admin --email admin@example.com --password e2etestpass123 \
        --project-name Research
    .venv/bin/python e2e/test_ui.py
"""

import sys
import time

try:
    from playwright.sync_api import Page, expect, sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && python -m playwright install chromium")
    sys.exit(1)

BASE_URL = "http://localhost:8000"
ADMIN_USER = "admin"
ADMIN_PASS = "e2etestpass123"


def wait_for_health(timeout: int = 60) -> None:
    """Wait until the web service reports healthy."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=3)
            if resp.status == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"Web service did not become healthy within {timeout}s")


def login(page: Page) -> None:
    """Sign in via the Django login form (idempotent: skips if session exists)."""
    page.goto(f"{BASE_URL}/login/")
    # If already authenticated, Django redirects to the dashboard.
    if page.url != f"{BASE_URL}/login/":
        return
    page.wait_for_selector('input[name="username"]', timeout=10_000)
    page.fill('input[name="username"]', ADMIN_USER)
    page.fill('input[name="password"]', ADMIN_PASS)
    page.click('button[type="submit"]')
    # Wait for the app shell (sidebar nav appears after successful auth)
    page.wait_for_selector("aside nav a", timeout=15_000)


def test_login_and_dashboard(page: Page) -> None:
    """Login succeeds and dashboard shows stats + runs table."""
    login(page)
    # Dashboard heading
    expect(page.locator("main h1, main h2").first).to_contain_text("Dashboard")
    # Runs table or empty state exists
    main = page.locator("main")
    expect(main).to_be_visible()


def test_navigation_all_views(page: Page) -> None:
    """All sidebar nav links navigate to the correct page."""
    login(page)
    views = [
        ("/dashboard/", "Dashboard"),
        ("/audits/new/", "New Audit"),
        ("/scenarios/", "Scenario"),
        ("/models/", "Model"),
        ("/compare/", "Compare"),
    ]
    for url, expected_text in views:
        page.goto(f"{BASE_URL}{url}")
        page.wait_for_timeout(500)
        main = page.locator("main")
        expect(main).to_contain_text(expected_text)


def test_new_audit_form_populated(page: Page) -> None:
    """New Audit view has the form with scenario set and model selectors."""
    login(page)
    page.goto(f"{BASE_URL}/audits/new/")
    page.wait_for_timeout(500)
    # Scenario set selector present
    expect(page.locator('select[name="scenario_set"]')).to_be_visible()
    # Model selectors present
    expect(page.locator('select[name="target_endpoint"]')).to_be_visible()
    expect(page.locator('select[name="auditor_endpoint"]')).to_be_visible()
    expect(page.locator('select[name="judge_endpoint"]')).to_be_visible()
    # Submit button present
    expect(page.locator("button[type='submit']")).to_be_visible()


def test_new_audit_no_profile_section(page: Page) -> None:
    """Audit Profile section is removed from New Audit form."""
    login(page)
    page.goto(f"{BASE_URL}/audits/new/")
    page.wait_for_timeout(500)
    # Profile selector must NOT exist
    expect(page.locator('select[name="profile"]')).to_have_count(0)
    # "Audit Profile" heading must NOT exist
    expect(page.locator("text=Audit Profile")).to_have_count(0)


def test_models_view_no_profiles(page: Page) -> None:
    """Models view no longer shows Audit Profiles section."""
    login(page)
    page.goto(f"{BASE_URL}/models/")
    page.wait_for_timeout(500)
    # "Audit Profiles" heading must NOT exist
    expect(page.locator("text=Audit Profiles")).to_have_count(0)
    # Profile add form must NOT exist
    expect(page.locator('input[name="profile_name"]')).to_have_count(0)


def test_audit_detail_clone_button(page: Page) -> None:
    """Audit detail page shows Clone Audit button (if runs exist)."""
    login(page)
    page.goto(f"{BASE_URL}/dashboard/")
    page.wait_for_timeout(500)
    # Find first audit run link
    rows = page.locator("table tbody tr")
    count = rows.count()
    if count == 0:
        print("SKIP: no audit runs in database")
        return
    # Click first run row (navigates via onclick to /audits/<id>/)
    rows.first.click()
    page.wait_for_timeout(1000)
    # Clone Audit button should be visible
    expect(page.locator("a:has-text('Clone Audit')")).to_be_visible()


def test_clone_prefills_form(page: Page) -> None:
    """Clone Audit pre-fills the New Audit form with original values."""
    login(page)
    page.goto(f"{BASE_URL}/dashboard/")
    page.wait_for_timeout(500)
    rows = page.locator("table tbody tr")
    count = rows.count()
    if count == 0:
        print("SKIP: no audit runs in database")
        return
    # Navigate to first run detail (row onclick)
    rows.first.click()
    page.wait_for_timeout(1000)
    # Click Clone Audit
    page.click("a:has-text('Clone Audit')")
    page.wait_for_timeout(1000)
    # Should be on New Audit page with pre-filled values
    expect(page.locator("main")).to_contain_text("New Audit")
    # Hidden scenario_set_version input should exist (exact version pin)
    expect(page.locator('input[name="scenario_set_version"]')).to_be_attached()


def test_compare_view(page: Page) -> None:
    """Compare view loads and shows selection UI."""
    login(page)
    page.goto(f"{BASE_URL}/compare/")
    page.wait_for_timeout(500)
    main = page.locator("main")
    expect(main).to_contain_text("Compare")


def main() -> int:
    wait_for_health()
    passed = 0
    failed = 0
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        tests = [
            ("login_and_dashboard", test_login_and_dashboard),
            ("navigation_all_views", test_navigation_all_views),
            ("new_audit_form_populated", test_new_audit_form_populated),
            ("new_audit_no_profile_section", test_new_audit_no_profile_section),
            ("models_view_no_profiles", test_models_view_no_profiles),
            ("audit_detail_clone_button", test_audit_detail_clone_button),
            ("clone_prefills_form", test_clone_prefills_form),
            ("compare_view", test_compare_view),
        ]

        for name, fn in tests:
            try:
                page.goto(BASE_URL)
                page.wait_for_timeout(500)
                fn(page)
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
                errors.append((name, str(e)))

        browser.close()

    print(f"\n{'='*50}")
    print(f"E2E UI Results: {passed} passed, {failed} failed, {passed+failed} total")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

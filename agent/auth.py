"""
Session bootstrap.

Both discovery and replay launch a brand-new browser context per run (see
REPORT.md "Architecture" trade-offs), so neither one carries over a login
session automatically. Rather than making authentication part of the
LLM-driven goal for every single capability (which would mean re-discovering
"how to log in" as a step inside every artifact, and re-running it on every
replay), we treat session establishment as infrastructure the capability
runs on top of - conceptually the same role SSO/a service-account token
would play against a real core banking system. The target app's login here
is a fixed demo credential (see target_app/app.py - explicitly not a real
secret), used only to get a session cookie before the recorded/replayed
steps begin.

This keeps `Artifact.steps` focused on the actual capability (search,
open a sub-account, ...) rather than being coupled to one authentication
scheme - a real deployment would swap this function's body for a tenant's
actual SSO/service-account flow without touching artifacts at all.
"""
from __future__ import annotations

DEMO_USERNAME = "operator"
DEMO_PASSWORD = "demo123"


def ensure_authenticated(page, base_url: str, username: str = DEMO_USERNAME,
                          password: str = DEMO_PASSWORD, evidence=None) -> None:
    login_url = base_url.rstrip("/") + "/login" if not base_url.endswith("/login") else base_url
    # strip to origin if a deeper path was passed as base_url
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    login_url = f"{parsed.scheme}://{parsed.netloc}/login"

    page.goto(login_url, timeout=10000)
    page.locator('[name="username"]').fill(username, timeout=5000)
    page.locator('[name="password"]').fill(password, timeout=5000)
    page.locator('input[type="submit"]').click(timeout=5000)
    page.wait_for_load_state("networkidle", timeout=5000)

    if "/login" in page.url:
        raise RuntimeError(f"Authentication failed - still on {page.url} after login attempt")

    if evidence:
        evidence.log("authenticated", {"login_url": login_url, "username": username})

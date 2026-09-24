"""Optional real-browser smoke test, using a disposable database and no providers.

Run with: uv run --with playwright pytest tests/test_dashboard_browser.py -q
Needs local Chrome (or Playwright's installed Chromium).
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest
import uvicorn
from conftest import make_config
from test_decision_log import logged

from jev_router.app import create_app

playwright = pytest.importorskip("playwright.sync_api")


def test_browser_audit(monkeypatch):
    credential = "browser-test-only"
    monkeypatch.setenv("AUDIT_BROWSER_TOKEN", credential)
    app = create_app(make_config(settings={"admin_token_env": "AUDIT_BROWSER_TOKEN"}))
    router = app.state.router
    first = logged(router.store, client="<img src=x onerror=alert(1)>", answers={
        "difficulty": {"type": "score", "score": 2.0, "confidence": 0.9},
        "task": {"type": "choice", "choice": "code", "confidence": 0.85},
        "harm_if_wrong": {"type": "noul", "noul": 0.2},
    })
    second = logged(router.store, client="code-cli", decision_id="decision-2", model="small", effort="low", rule="easy")
    router.sessions.annotate_decision(second, event_type="admission", session_id="session-demo", request_id="request-demo", quality_lane="simple")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/dashboard")
                page.wait_for_function("() => !document.getElementById('error').hidden")
                page.locator("#token").fill(credential)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.wait_for_function("() => document.getElementById('total').textContent === '2'")
                assert page.locator("#token").input_value() == ""
                assert page.locator("#rows img").count() == 0
                page.get_by_role("button", name=f"Inspect event {first}", exact=True).click()
                assert "difficulty" in page.locator("#detail").inner_text()
                assert "Matching configured rule" in page.locator("#detail").inner_text()
                assert page.locator(".judgment-card").count() == 3
                assert page.locator(".judgment-card progress").count() == 3
                assert "difficulty ≥ 1.5" in page.locator(".rule-card").inner_text()
                assert "90%" in page.locator(".judgment-card").first.inner_text()
                assert "code" in page.locator(".judgment-card").nth(1).inner_text()
                assert "20%" in page.locator(".judgment-card").nth(2).inner_text()
                page.locator("#search").fill("code-cli")
                assert page.locator("#rows tr").count() == 1
                page.get_by_role("button", name="Reset filters").click()
                page.get_by_role("button", name="session-demo · 1", exact=True).click()
                assert page.locator("#rows tr").count() == 1
                page.get_by_role("button", name="Reset filters").click()
                page.get_by_role("button", name=f"Inspect event {first}", exact=True).click()
                router.store.update_decision(first, upstream_status=200, stream_state="complete", response_ms=123)
                page.get_by_text("Request & session identifiers", exact=True).click()
                page.wait_for_function("() => document.getElementById('detail').textContent.includes('HTTP 200')")
                assert page.locator("#detail details[open]").filter(has=page.get_by_text("Request & session identifiers", exact=True)).count() == 1
                page.get_by_role("button", name="Pause", exact=True).click()
                logged(router.store, decision_id="decision-3")
                page.wait_for_timeout(2300)
                assert page.locator("#total").inner_text() == "2"
                page.get_by_role("button", name="Resume", exact=True).click()
                page.wait_for_function("() => document.getElementById('total').textContent === '3'")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.get_by_role("button", name="Disconnect & clear").click()
                assert page.locator("#total").inner_text() == "0"
                assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
                assert credential not in page.url
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Request, Route, WebSocket, sync_playwright

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.storage import (
    create_database_engine,
    ingest_snapshot,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CLI_CONSUMPTION_BROWSER_GATE") != "1",
    reason=(
        "the dedicated CI job installs Chromium and enables the offline browser gate"
    ),
)


@pytest.mark.parametrize("share_safe", [False, True], ids=["detailed", "share-safe"])
def test_generated_dashboard_opens_and_interacts_without_network(
    tmp_path: Path,
    rollout_factory,
    share_safe: bool,
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(
        engine,
        CodexAdapter().collect(
            [("offline-machine", home)],
            [("offline-project", "/srv/work")],
        ),
    )
    output = tmp_path / ("share-safe.html" if share_safe else "detailed.html")
    generate_dashboard(engine, output, share_safe=share_safe)
    engine.dispose()

    html = output.read_text(encoding="utf-8")
    for prohibited in (
        "secret value",
        "https://",
        "<script src",
        "<link",
        "@import",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "import(",
    ):
        assert prohibited not in html

    network_requests: list[str] = []
    web_sockets: list[str] = []
    browser_errors: list[str] = []

    def reject_network(route: Route, request: Request) -> None:
        network_requests.append(request.url)
        route.abort()

    def record_request(request: Request) -> None:
        if urlparse(request.url).scheme in {"http", "https"}:
            network_requests.append(request.url)

    def record_web_socket(socket: WebSocket) -> None:
        web_sockets.append(socket.url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("http://**/*", reject_network)
        context.route("https://**/*", reject_network)
        context.on("request", record_request)
        page = context.new_page()
        page.on("websocket", record_web_socket)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                browser_errors.append(message.text) if message.type == "error" else None
            ),
        )

        response = page.goto(output.resolve().as_uri(), wait_until="load")
        assert response is not None
        assert urlparse(response.url).scheme == "file"
        assert response.ok
        page.wait_for_function("document.querySelectorAll('#cards .card').length >= 8")
        assert page.title() == "CLI Consumption"
        assert page.locator("#provider").input_value() == ""
        page.locator("#provider").select_option("codex")
        assert page.locator("#provider").input_value() == "codex"
        assert page.locator("#cards .card").count() >= 8

        page.locator("#period").select_option("custom")
        assert "visible" in (page.locator("#customDates").get_attribute("class") or "")

        first_theme = page.locator("html").get_attribute("data-theme")
        page.locator("#themeToggle").click()
        second_theme = page.locator("html").get_attribute("data-theme")
        assert second_theme in {"light", "dark"}
        assert second_theme != first_theme

        assert page.locator("#privacyBadge").is_visible() is share_safe
        assert page.locator("#conversationCount").text_content()
        assert page.locator("a[href], script[src], link[href]").count() == 0

        context.close()
        browser.close()

    assert network_requests == []
    assert web_sockets == []
    assert browser_errors == []

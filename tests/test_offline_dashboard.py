from __future__ import annotations

import os
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Request, Route, WebSocket, sync_playwright

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.dashboard_layouts import DashboardLayoutV1
from cli_consumption.models import Snapshot, empty_tokens
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

LAYOUT_FIXTURE = Path(__file__).parent / "fixtures" / "dashboard_layout_v1_custom.json"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 768, 390])
def test_offline_uses_the_same_layout_composition_geometry_and_theme(
    tmp_path: Path, theme: Literal["light", "dark"], width: int
) -> None:
    layout = DashboardLayoutV1.model_validate_json(
        LAYOUT_FIXTURE.read_text(encoding="utf-8")
    )
    output = tmp_path / "layout.html"
    engine = create_database_engine(tmp_path / "layout.sqlite")
    generate_dashboard(engine, output, layout=layout, theme=theme)
    engine.dispose()

    expected = [
        {
            "height": str(widget.size.height),
            "type": widget.type,
            "width": str(widget.size.width),
            "x": str(widget.position.x),
            "y": str(widget.position.y),
        }
        for widget in sorted(
            layout.widgets,
            key=lambda widget: (
                widget.position.y,
                widget.position.x,
                widget.id,
            ),
        )
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900})
        network_requests: list[str] = []
        page.on(
            "request",
            lambda request: (
                network_requests.append(request.url)
                if urlparse(request.url).scheme in {"http", "https"}
                else None
            ),
        )
        response = page.goto(output.resolve().as_uri(), wait_until="load")
        assert response is not None and response.ok
        assert page.locator("html").get_attribute("data-theme") == theme
        actual = page.locator("[data-widget-type]").evaluate_all(
            """widgets => widgets.map(widget => ({
                height: widget.getAttribute('data-size-height'),
                type: widget.getAttribute('data-widget-type'),
                width: widget.getAttribute('data-size-width'),
                x: widget.getAttribute('data-position-x'),
                y: widget.getAttribute('data-position-y'),
            }))"""
        )
        browser.close()

    assert actual == expected
    assert network_requests == []
    assert [widget["type"] for widget in actual].index("technical-work-items") < [
        widget["type"] for widget in actual
    ].index("context-pressure")


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
    ingest_snapshot(
        engine,
        Snapshot(
            provider="gemini",
            malformed_records=7,
            conversations=[
                {
                    "id": "gemini:offline-conversation",
                    "provider": "gemini",
                    "external_id": "offline-conversation",
                    "source_machine": "offline-machine",
                    "project": "offline-project",
                    "project_source": "none",
                    "started_at": "2026-08-25T11:00:00Z",
                    "ended_at": "2026-08-25T11:01:00Z",
                    "duration_seconds": 60.0,
                    "source": "synthetic",
                    "models": [],
                    "iterations": 0,
                    "model_calls": 0,
                    "tool_calls": 0,
                    "compactions": 0,
                    "event_count": 1,
                    "content_hash": "1" * 64,
                    **empty_tokens(),
                }
            ],
        ),
    )
    output = tmp_path / f"{'share-safe' if share_safe else 'detailed'}.html"
    generate_dashboard(engine, output, share_safe=share_safe)
    engine.dispose()

    html = output.read_text(encoding="utf-8")
    license_text = (
        Path(__file__)
        .parents[1]
        .joinpath("src", "cli_consumption", "INTER_FONT_LICENSE.txt")
        .read_text(encoding="utf-8")
        .replace("https://", "")
        .replace("http://", "")
    )
    assert '<script id="inter-font-license" type="text/plain">' in html
    assert "globalThis.__CLI_CONSUMPTION_THEME__=null" in html
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "connect-src 'none'" in html
    assert escape(license_text, quote=False) in html
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
        assert page.evaluate(
            """async () => {
                await document.fonts.ready;
                return document.fonts.check('16px "Inter Variable"') &&
                    getComputedStyle(document.body).fontFamily.startsWith(
                        '"Inter Variable"'
                    );
            }"""
        )
        assert page.title() == "CLI Consumption"
        title_box = page.locator(".hero h1").bounding_box()
        eyebrow_box = page.locator(".hero .eyebrow").bounding_box()
        assert title_box is not None
        assert eyebrow_box is not None
        assert eyebrow_box["x"] > title_box["x"] + title_box["width"]
        assert page.locator(".metric-card").first.evaluate(
            """card => {
                const body = getComputedStyle(document.body);
                const style = getComputedStyle(card);
                return style.backgroundColor === body.backgroundColor &&
                    style.borderTopWidth === "0px";
            }"""
        )
        assert page.locator("#provider").input_value() == ""
        assert page.locator("#conversationCount").text_content() == "2 conversations"
        assert page.locator("#table tbody tr").count() == 2
        if not share_safe:
            tools_per_turn = (
                page.locator("section:has(h2:text-is('Cohort comparison')) tbody tr")
                .first.locator("td")
                .nth(4)
            )
            assert tools_per_turn.text_content() is not None
            assert "." in (tools_per_turn.text_content() or "")
        page.locator("#provider").select_option("codex")
        assert page.locator("#provider").input_value() == "codex"
        assert page.locator("#cards .card").count() >= 8
        assert page.locator("#conversationCount").text_content() == "1 conversations"
        assert page.locator("#table tbody tr").count() == 1
        assert (
            page.locator(
                ".metric-card:has(> span:text-is('Malformed records')) strong"
            ).text_content()
            == "0"
        )

        page.locator("#period").select_option("custom")
        assert "visible" in (page.locator("#customDates").get_attribute("class") or "")

        first_theme = page.locator("html").get_attribute("data-theme")
        page.locator("#themeToggle").click()
        second_theme = page.locator("html").get_attribute("data-theme")
        assert second_theme in {"light", "dark"}
        assert second_theme != first_theme

        assert page.locator("#privacyBadge").is_visible() is share_safe
        assert page.locator("a[href], script[src], link[href]").count() == 0

        context.close()
        browser.close()

    assert network_requests == []
    assert web_sockets == []
    assert browser_errors == []

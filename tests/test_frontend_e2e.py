from __future__ import annotations

import os
import shutil
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import pytest
import uvicorn
from playwright.sync_api import Browser, Error, Page, expect, sync_playwright

from evolvable_memory import frontend
from evolvable_memory.api.app import create_app
from evolvable_memory.config import Settings

pytestmark = pytest.mark.browser


@dataclass(frozen=True, slots=True)
class RunningConsole:
    frontend_url: str
    api_url: str


class QuietFrontendRequestHandler(frontend.FrontendRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        del args


def _wait_for_url(url: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.4)
            response.raise_for_status()
            return
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            time.sleep(0.05)
    raise RuntimeError(f"service did not become ready at {url}") from last_error


@pytest.fixture(scope="session")
def running_console(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunningConsole]:
    static_directory = tmp_path_factory.mktemp("frontend-e2e")
    shutil.copytree(frontend._STATIC_DIRECTORY, static_directory, dirs_exist_ok=True)

    handler = partial(QuietFrontendRequestHandler, directory=str(static_directory))
    frontend_server = frontend.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    frontend_port = int(frontend_server.server_address[1])
    frontend_url = f"http://127.0.0.1:{frontend_port}"

    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    api_socket.bind(("127.0.0.1", 0))
    api_port = int(api_socket.getsockname()[1])
    api_url = f"http://127.0.0.1:{api_port}"

    index_path = Path(static_directory) / "index.html"
    index = index_path.read_text(encoding="utf-8")
    configured_index = index.replace(
        '<meta name="api-port" content="38089" />',
        f'<meta name="api-port" content="{api_port}" />',
    )
    if configured_index == index:
        raise RuntimeError("frontend api-port meta tag was not found")
    index_path.write_text(configured_index, encoding="utf-8")

    settings = Settings(
        host="127.0.0.1",
        port=api_port,
        store="memory",
        frontend_url=frontend_url,
        public_api_url=api_url,
        cors_origins=(frontend_url,),
    )
    api_server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings=settings),
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    api_thread = Thread(
        target=api_server.run,
        kwargs={"sockets": [api_socket]},
        name="frontend-e2e-api",
        daemon=True,
    )
    frontend_thread = Thread(
        target=frontend_server.serve_forever,
        name="frontend-e2e-static",
        daemon=True,
    )
    api_thread.start()
    frontend_thread.start()
    try:
        _wait_for_url(f"{api_url}/health")
        _wait_for_url(frontend_url)
        yield RunningConsole(frontend_url=frontend_url, api_url=api_url)
    finally:
        frontend_server.shutdown()
        frontend_server.server_close()
        api_server.should_exit = True
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)


@pytest.fixture(scope="session")
def chromium_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as error:
            message = (
                "Chromium is unavailable. Run `uv run playwright install chromium` "
                "to enable browser E2E tests."
            )
            if os.getenv("EMF_REQUIRE_BROWSER_E2E") == "1":
                pytest.fail(f"{message}\n{error}")
            pytest.skip(message)
        try:
            yield browser
        finally:
            browser.close()


def _open_console(page: Page, console: RunningConsole, *, tour: bool = False) -> None:
    page.goto(
        f"{console.frontend_url}/?tour={'1' if tour else '0'}",
        wait_until="networkidle",
    )
    expect(page.locator("#status-label")).to_have_text("API 在线")


def _boxes_overlap(left: dict[str, float], right: dict[str, float]) -> bool:
    horizontal = min(left["x"] + left["width"], right["x"] + right["width"]) - max(
        left["x"], right["x"]
    )
    vertical = min(left["y"] + left["height"], right["y"] + right["height"]) - max(
        left["y"], right["y"]
    )
    return horizontal > 1 and vertical > 1


@pytest.mark.parametrize(
    ("width", "height"),
    [(390, 844), (900, 900), (1366, 900), (2048, 1152)],
)
def test_quickstart_has_no_horizontal_overflow_or_text_overlap(
    chromium_browser: Browser,
    running_console: RunningConsole,
    width: int,
    height: int,
) -> None:
    context = chromium_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        quickstart = page.locator(".quickstart-panel")
        quickstart.scroll_into_view_if_needed()
        expect(quickstart).to_be_visible()

        dimensions = page.evaluate(
            """() => ({
                documentClientWidth: document.documentElement.clientWidth,
                documentScrollWidth: document.documentElement.scrollWidth,
                panelClientWidth: document.querySelector('.quickstart-panel').clientWidth,
                panelScrollWidth: document.querySelector('.quickstart-panel').scrollWidth,
                stepsClientWidth: document.querySelector('.journey-steps').clientWidth,
                stepsScrollWidth: document.querySelector('.journey-steps').scrollWidth,
            })"""
        )
        assert dimensions["documentScrollWidth"] <= dimensions["documentClientWidth"] + 1
        assert dimensions["panelScrollWidth"] <= dimensions["panelClientWidth"] + 1
        assert dimensions["stepsScrollWidth"] <= dimensions["stepsClientWidth"] + 1

        copies = page.locator(".journey-copy")
        assert copies.count() == 5
        boxes = [copies.nth(index).bounding_box() for index in range(copies.count())]
        assert all(box is not None for box in boxes)
        visible_boxes = [box for box in boxes if box is not None]
        for index, left in enumerate(visible_boxes):
            for right in visible_boxes[index + 1 :]:
                assert not _boxes_overlap(left, right)
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height"),
    [(390, 844), (900, 900), (1366, 900), (2048, 1152)],
)
def test_capture_uses_a_flat_responsive_workspace(
    chromium_browser: Browser,
    running_console: RunningConsole,
    width: int,
    height: int,
) -> None:
    context = chromium_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        if width <= 900:
            page.locator(".mobile-menu").click()
        page.locator('[data-view="capture"]').click()
        expect(page.locator("#view-capture")).to_have_class("view is-active")

        layout = page.evaluate(
            """() => {
                const container = document.querySelector('.page-container');
                const view = document.querySelector('#view-capture');
                const form = document.querySelector('.form-panel');
                const storage = document.querySelector('.storage-card');
                const explainer = document.querySelector('.explainer-card');
                const containerStyle = getComputedStyle(container);
                const formStyle = getComputedStyle(form);
                const storageStyle = getComputedStyle(storage);
                const explainerStyle = getComputedStyle(explainer);
                return {
                    documentClientWidth: document.documentElement.clientWidth,
                    documentScrollWidth: document.documentElement.scrollWidth,
                    containerContentWidth: container.clientWidth
                        - parseFloat(containerStyle.paddingLeft)
                        - parseFloat(containerStyle.paddingRight),
                    viewWidth: view.getBoundingClientRect().width,
                    formBackground: formStyle.backgroundColor,
                    formBorderRadius: formStyle.borderRadius,
                    formBorderRightWidth: formStyle.borderRightWidth,
                    formBoxShadow: formStyle.boxShadow,
                    storageBorderRadius: storageStyle.borderRadius,
                    storageBoxShadow: storageStyle.boxShadow,
                    explainerBorderRadius: explainerStyle.borderRadius,
                    explainerBoxShadow: explainerStyle.boxShadow,
                };
            }"""
        )

        assert layout["documentScrollWidth"] <= layout["documentClientWidth"] + 1
        assert layout["viewWidth"] >= layout["containerContentWidth"] - 2
        assert layout["formBackground"] == "rgba(0, 0, 0, 0)"
        assert layout["formBorderRadius"] == "0px"
        assert layout["formBoxShadow"] == "none"
        assert layout["storageBorderRadius"] == "0px"
        assert layout["storageBoxShadow"] == "none"
        assert layout["explainerBorderRadius"] == "0px"
        assert layout["explainerBoxShadow"] == "none"
        if width <= 900:
            assert layout["formBorderRightWidth"] == "0px"
        else:
            assert layout["formBorderRightWidth"] == "1px"
        if width == 2048:
            assert layout["viewWidth"] > 1600
    finally:
        context.close()


def test_onboarding_dialog_has_accessible_keyboard_flow(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        _open_console(page, running_console, tour=True)
        dialog = page.get_by_role("dialog", name="先确认数据属于谁")
        expect(dialog).to_be_visible()
        expect(page.locator("#onboarding-count")).to_contain_text("第 1 步")
        expect(page.locator("#onboarding-count")).to_contain_text("共 5 步")

        next_button = page.locator("#onboarding-next")
        next_button.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#onboarding-title")).to_have_text("从原始证据写入记忆")
        expect(page.locator("#onboarding-count")).to_contain_text("第 2 步")
        expect(page.locator("#onboarding-count")).to_contain_text("共 5 步")

        page.keyboard.press("Tab")
        assert page.evaluate(
            "document.querySelector('#onboarding-dialog').contains(document.activeElement)"
        )
        page.keyboard.press("Escape")
        expect(page.locator("#onboarding-dialog")).to_be_hidden()
        # The native dialog dispatches `close` just after removing the open state.
        # A short event-loop turn keeps this assertion independent of eval, which
        # the console's Content Security Policy intentionally forbids.
        page.wait_for_timeout(50)
        assert page.evaluate("localStorage.getItem('emf.onboarding.v1')") is None
        assert page.evaluate("sessionStorage.getItem('emf.onboarding.dismissed')") == "true"

        page.locator("#open-onboarding").click()
        page.locator("#onboarding-dots button").nth(4).click()
        expect(page.locator("#onboarding-title")).to_have_text("用真实结果完成学习闭环")
        page.locator("#onboarding-next").click()
        expect(page.locator("#onboarding-dialog")).to_be_hidden()
        assert page.evaluate("localStorage.getItem('emf.onboarding.v1')") == "complete"
        assert page.evaluate("sessionStorage.getItem('emf.onboarding.dismissed')") is None
    finally:
        context.close()


def test_scope_switch_clears_old_results_and_ignores_late_response(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    tenant = f"e2e-{uuid4().hex[:8]}"
    first_subject = f"first-{uuid4().hex[:8]}"
    second_subject = f"second-{uuid4().hex[:8]}"
    stale_value = "STALE-SCOPE-VALUE-MUST-NOT-RENDER"
    try:
        _open_console(page, running_console)
        page.locator("#tenant-id").fill(tenant)
        page.locator("#subject-id").fill(first_subject)
        page.locator("#save-scope").click()
        expect(page.locator("#stat-subject")).to_have_text(first_subject)

        page.locator('[data-view="capture"]').click()
        page.locator('#memory-form input[name="key"]').fill("drink.preference")
        page.locator('#memory-form input[name="value"]').fill("decaf coffee")
        page.locator('#memory-form textarea[name="evidence_text"]').fill(
            "I prefer decaf coffee in the evening"
        )
        page.locator('#memory-form button[type="submit"]').click()
        expect(page.locator("#capture-result")).to_be_visible()

        page.locator('[data-view="recall"]').click()
        page.locator('#recall-form input[name="query"]').fill("decaf coffee")
        page.locator('#recall-form button[type="submit"]').click()
        expect(page.locator(".result-card")).to_have_count(1)
        expect(page.locator("#recall-meta")).to_be_visible()

        page.evaluate(
            """({ subject, staleValue }) => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const url = String(input);
                    if (url.includes('/v1/preferences?') && url.includes(subject)) {
                        const staleItem = [{
                            record_id: '00000000-0000-4000-8000-000000000001',
                            revision_id: '00000000-0000-4000-8000-000000000002',
                            key: 'stale.preference',
                            value: staleValue,
                            context: {},
                            confidence: 0.9,
                            sequence: 1,
                            evidence_count: 1,
                            support_count: 1,
                            valid_from: '2026-01-01T00:00:00Z',
                            recorded_at: '2026-01-01T00:00:00Z',
                        }];
                        return new Promise((resolve) => {
                            window.setTimeout(() => resolve(new Response(
                                JSON.stringify(staleItem),
                                { status: 200, headers: { 'content-type': 'application/json' } },
                            )), 650);
                        });
                    }
                    return originalFetch(input, init);
                };
            }""",
            {"subject": first_subject, "staleValue": stale_value},
        )
        page.locator('[data-view="memories"]').click()
        page.locator("#subject-id").fill(second_subject)
        page.locator("#save-scope").click()

        expect(page.locator("#stat-writes")).to_have_text("0")
        expect(page.locator("#stat-recalls")).to_have_text("0")
        expect(page.locator("#stat-outcomes")).to_have_text("0")
        expect(page.locator("#journey-progress")).to_have_text("已完成 1 / 5")
        expect(page.locator("#recall-meta")).to_be_hidden()
        expect(page.locator(".result-card")).to_have_count(0)
        expect(page.locator("#capture-result")).to_be_hidden()
        expect(page.locator("#memory-count")).to_have_text("0 条记忆")
        expect(page.locator("#memory-form input[name='value']")).to_have_value("")

        page.locator('[data-view="overview"]').click()
        expect(page.locator(".empty-activity")).to_be_visible()

        page.wait_for_timeout(800)
        expect(page.get_by_text(stale_value, exact=True)).to_have_count(0)
        expect(page.locator("#stat-subject")).to_have_text(second_subject)
    finally:
        context.close()


def test_basic_keyboard_navigation(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        page.keyboard.press("Tab")
        expect(page.locator(".skip-link")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()

        memories_navigation = page.locator('.nav-item[data-view="memories"]')
        memories_navigation.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#view-memories")).to_have_class("view is-active")
        expect(memories_navigation).to_have_attribute("aria-current", "page")

        subject = f"keyboard-{uuid4().hex[:8]}"
        page.locator("#subject-id").fill(subject)
        page.locator("#subject-id").press("Enter")
        expect(page.locator("#stat-subject")).to_have_text(subject)

        onboarding = page.locator("#open-onboarding")
        onboarding.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#onboarding-dialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#onboarding-dialog")).to_be_hidden()
    finally:
        context.close()

from __future__ import annotations

import json
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
from playwright.sync_api import Browser, Error, FloatRect, Page, Route, expect, sync_playwright

from conftest import prepare_postgres_database
from evolvable_memory import frontend
from evolvable_memory.api.app import create_app
from evolvable_memory.config import Settings

pytestmark = pytest.mark.browser

_AXE_SOURCE = Path(__file__).parents[1] / "node_modules" / "axe-core" / "axe.min.js"


@dataclass(frozen=True, slots=True)
class RunningConsole:
    frontend_url: str
    api_url: str
    storage: str


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

    storage = os.getenv("EMF_BROWSER_E2E_STORE", "memory")
    if storage not in {"memory", "postgres"}:
        pytest.fail("EMF_BROWSER_E2E_STORE must be memory or postgres")
    database_url = os.getenv("EMF_TEST_DATABASE_URL") if storage == "postgres" else None
    if storage == "postgres":
        if database_url is None:
            pytest.fail("EMF_TEST_DATABASE_URL is required for PostgreSQL browser E2E")
        prepare_postgres_database(database_url)

    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    api_socket.bind(("127.0.0.1", 0))
    api_port = int(api_socket.getsockname()[1])
    api_url = f"http://127.0.0.1:{api_port}"

    handler = partial(
        QuietFrontendRequestHandler,
        directory=str(static_directory),
        public_api_url=api_url,
    )
    frontend_server = frontend.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    frontend_port = int(frontend_server.server_address[1])
    frontend_url = f"http://127.0.0.1:{frontend_port}"

    settings = Settings(
        host="127.0.0.1",
        port=api_port,
        store=storage,
        database_url=database_url,
        database_pool_min_size=1,
        database_pool_max_size=3,
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
        _wait_for_url(f"{api_url}/readyz")
        _wait_for_url(frontend_url)
        health = httpx.get(f"{api_url}/health", timeout=2).json()
        assert health["storage"] == storage
        yield RunningConsole(frontend_url=frontend_url, api_url=api_url, storage=storage)
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


def _boxes_overlap(left: FloatRect, right: FloatRect) -> bool:
    horizontal = min(left["x"] + left["width"], right["x"] + right["width"]) - max(
        left["x"], right["x"]
    )
    vertical = min(left["y"] + left["height"], right["y"] + right["height"]) - max(
        left["y"], right["y"]
    )
    return horizontal > 1 and vertical > 1


def _box_is_inside(
    inner: FloatRect,
    outer: FloatRect,
    *,
    tolerance: float = 1,
) -> bool:
    return (
        inner["x"] >= outer["x"] - tolerance
        and inner["y"] >= outer["y"] - tolerance
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tolerance
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tolerance
    )


def _install_axe(page: Page) -> None:
    if not _AXE_SOURCE.is_file():
        message = "axe-core is unavailable; run `npm ci` to enable accessibility E2E"
        if os.getenv("EMF_REQUIRE_ACCESSIBILITY_E2E") == "1":
            pytest.fail(message)
        pytest.skip(message)
    page.add_script_tag(path=str(_AXE_SOURCE))


def _assert_no_accessibility_violations(page: Page, state: str) -> None:
    results = page.evaluate(
        """async () => await globalThis.axe.run(document, {
            resultTypes: ["violations"],
        })"""
    )
    violations = results["violations"]
    report = [
        {
            "rule": violation["id"],
            "impact": violation["impact"],
            "help": violation["help"],
            "nodes": [
                {
                    "target": node["target"],
                    "failure_summary": node["failureSummary"],
                }
                for node in violation["nodes"]
            ],
        }
        for violation in violations
    ]
    formatted_report = json.dumps(report, ensure_ascii=False, indent=2)
    assert not violations, f"axe-core violations in {state}:\n{formatted_report}"


def test_console_reports_configured_authoritative_storage(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        expected = (
            "PostgreSQL 权威存储" if running_console.storage == "postgres" else "后端进程内存"
        )
        expect(page.locator("#storage-title")).to_have_text(expected)
    finally:
        context.close()


def test_readiness_failure_never_reports_the_service_as_online(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    request_id = "ready-check-1234567890"

    def reject_readiness(route: Route) -> None:
        route.fulfill(
            status=503,
            headers={
                "access-control-allow-origin": running_console.frontend_url,
                "access-control-expose-headers": "X-Request-ID",
                "content-type": "application/json",
                "x-request-id": request_id,
            },
            body=json.dumps({"status": "not_ready", "storage": running_console.storage}),
        )

    page.route("**/readyz", reject_readiness)
    try:
        page.goto(f"{running_console.frontend_url}/?tour=0", wait_until="networkidle")
        expect(page.locator("#status-label")).to_have_text("服务未就绪")
        expect(page.locator("#status-dot")).to_have_class("status-dot is-not-ready")
        expect(page.locator("#status-detail")).to_contain_text("请求号 ready-ch…7890")

        page.unroute("**/readyz", reject_readiness)
        page.locator("#retry-health").click()
        expect(page.locator("#status-label")).to_have_text("API 在线")
        expect(page.locator("#status-dot")).to_have_class("status-dot is-online")
    finally:
        context.close()


@pytest.mark.parametrize(
    ("body_request_id", "header_request_id", "expected_reference"),
    (
        ("body-request-abcdef123456", "ignored-header-123456", "body-req…3456"),
        (None, "header-request-abcdef123456", "header-r…3456"),
    ),
)
def test_api_errors_show_a_short_correlated_request_reference(
    chromium_browser: Browser,
    running_console: RunningConsole,
    body_request_id: str | None,
    header_request_id: str,
    expected_reference: str,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()

    def reject_recall(route: Route) -> None:
        if route.request.method == "OPTIONS":
            route.continue_()
            return
        body: dict[str, str] = {
            "detail": "simulated recall failure",
            "error": "ConflictError",
        }
        if body_request_id is not None:
            body["request_id"] = body_request_id
        route.fulfill(
            status=409,
            headers={
                "access-control-allow-origin": running_console.frontend_url,
                "access-control-expose-headers": "X-Request-ID",
                "content-type": "application/json",
                "x-request-id": header_request_id,
            },
            body=json.dumps(body),
        )

    page.route("**/v1/recall", reject_recall)
    try:
        _open_console(page, running_console)
        page.locator('[data-view="recall"]').click()
        page.locator('#recall-form input[name="query"]').fill("correlated failure")
        page.locator('#recall-form button[type="submit"]').click()
        expect(page.locator(".toast.is-error p")).to_have_text(
            f"simulated recall failure\uff08请求号 {expected_reference}\uff09"
        )
    finally:
        context.close()


def test_suppressed_scope_explains_the_privacy_fence_and_recovery_boundary(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()

    def reject_preferences(route: Route) -> None:
        if route.request.method == "OPTIONS":
            route.continue_()
            return
        route.fulfill(
            status=403,
            headers={
                "access-control-allow-origin": running_console.frontend_url,
                "access-control-expose-headers": "X-Request-ID",
                "content-type": "application/json",
                "x-request-id": "suppressed-scope-request-1234",
            },
            body=json.dumps(
                {
                    "detail": "processing_suppressed",
                    "error": "ProcessingDeniedError",
                    "request_id": "suppressed-scope-request-1234",
                }
            ),
        )

    page.route("**/v1/preferences?**", reject_preferences)
    try:
        _open_console(page, running_console)
        page.locator('[data-view="memories"]').click()
        expect(page.locator("#memory-library .empty-results p")).to_contain_text("已被隐私抑制")
        expect(page.locator("#memory-library .empty-results p")).to_contain_text(
            "已删除的记忆不会恢复"
        )
    finally:
        context.close()


def test_visible_workbench_states_pass_automated_accessibility_audit(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(
        viewport={"width": 1366, "height": 900},
        bypass_csp=True,
    )
    page = context.new_page()
    tenant = f"a11y-{uuid4().hex[:8]}"
    subject = f"subject-{uuid4().hex[:8]}"
    try:
        _open_console(page, running_console)
        _install_axe(page)
        _assert_no_accessibility_violations(page, "overview")

        page.locator("#open-onboarding").click()
        expect(page.locator("#onboarding-dialog")).to_be_visible()
        _assert_no_accessibility_violations(page, "onboarding dialog")
        page.keyboard.press("Escape")

        page.locator("#tenant-id").fill(tenant)
        page.locator("#subject-id").fill(subject)
        page.locator("#save-scope").click()
        page.locator('[data-view="capture"]').click()
        _assert_no_accessibility_violations(page, "capture form")

        page.locator('#memory-form input[name="key"]').fill("drink.preference")
        page.locator('#memory-form input[name="value"]').fill("decaf coffee")
        page.locator('#memory-form textarea[name="evidence_text"]').fill(
            "I prefer decaf coffee in the evening"
        )
        page.locator('#memory-form button[type="submit"]').click()
        expect(page.locator("#capture-result")).to_be_visible()
        _assert_no_accessibility_violations(page, "capture success")

        page.locator('[data-view="memories"]').click()
        expect(page.locator(".memory-card")).to_have_count(1)
        _assert_no_accessibility_violations(page, "memory library")

        page.locator('[data-view="recall"]').click()
        page.locator('#recall-form input[name="query"]').fill("decaf coffee")
        page.locator('#recall-form button[type="submit"]').click()
        expect(page.locator(".result-card")).to_have_count(1)
        _assert_no_accessibility_violations(page, "recall results")

        page.locator(".result-card").first.get_by_role("button", name="修正记忆").click()
        expect(page.locator("#correction-modal")).to_be_visible()
        _assert_no_accessibility_violations(page, "correction dialog")
    finally:
        context.close()


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


@pytest.mark.parametrize("width", (320, 390, 568, 720, 721, 800, 801, 901, 1024))
def test_topbar_actions_stay_inside_the_viewport_without_overlap(
    chromium_browser: Browser,
    running_console: RunningConsole,
    width: int,
) -> None:
    context = chromium_browser.new_context(viewport={"width": width, "height": 844})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        topbar = page.locator(".topbar")
        actions = page.locator(".topbar-actions")
        guide = page.locator("#open-onboarding")
        scope = page.locator(".scope-control")
        editor = page.locator(".scope-editor")
        status = page.locator("#scope-status")
        subject_field = page.locator(".scope-fields label").nth(1)
        save = page.locator("#save-scope")

        expect(guide).to_have_accessible_name("新手引导")
        topbar_box = topbar.bounding_box()
        actions_box = actions.bounding_box()
        guide_box = guide.bounding_box()
        scope_box = scope.bounding_box()
        editor_box = editor.bounding_box()
        status_box = status.bounding_box()
        subject_field_box = subject_field.bounding_box()
        save_box = save.bounding_box()
        assert all(
            box is not None
            for box in (
                topbar_box,
                actions_box,
                guide_box,
                scope_box,
                editor_box,
                status_box,
                subject_field_box,
                save_box,
            )
        )
        assert topbar_box is not None
        assert actions_box is not None
        assert guide_box is not None
        assert scope_box is not None
        assert editor_box is not None
        assert status_box is not None
        assert subject_field_box is not None
        assert save_box is not None

        assert _box_is_inside(actions_box, topbar_box)
        assert _box_is_inside(guide_box, actions_box)
        assert _box_is_inside(scope_box, actions_box)
        assert _box_is_inside(editor_box, scope_box)
        assert _box_is_inside(save_box, scope_box)
        assert not _boxes_overlap(guide_box, scope_box)
        assert not _boxes_overlap(editor_box, save_box)
        assert status_box["x"] == pytest.approx(subject_field_box["x"], abs=1)

        dimensions = page.evaluate(
            """() => ({
                documentClientWidth: document.documentElement.clientWidth,
                documentScrollWidth: document.documentElement.scrollWidth,
            })"""
        )
        assert dimensions["documentScrollWidth"] <= dimensions["documentClientWidth"] + 1

        if width <= 620:
            assert guide_box["width"] == pytest.approx(44, abs=1)
        else:
            assert guide_box["width"] > 44
    finally:
        context.close()


def test_anime_hero_stays_readable_on_mobile(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        geometry = page.evaluate(
            """() => {
                const hero = document.querySelector('.hero-panel').getBoundingClientRect();
                const copy = document.querySelector('.hero-copy').getBoundingClientRect();
                const heading = document.querySelector('.hero-copy h2').getBoundingClientRect();
                return {
                    heroWidth: hero.width,
                    copyWidth: copy.width,
                    headingHeight: heading.height,
                    documentClientWidth: document.documentElement.clientWidth,
                    documentScrollWidth: document.documentElement.scrollWidth,
                };
            }"""
        )

        assert geometry["copyWidth"] >= geometry["heroWidth"] * 0.85
        assert geometry["headingHeight"] < 120
        assert geometry["documentScrollWidth"] <= geometry["documentClientWidth"] + 1
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


@pytest.mark.parametrize(("width", "height"), ((1366, 900), (900, 720)))
def test_onboarding_dialog_keeps_stable_geometry_between_steps(
    chromium_browser: Browser,
    running_console: RunningConsole,
    width: int,
    height: int,
) -> None:
    context = chromium_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        _open_console(page, running_console, tour=True)
        dialog = page.locator("#onboarding-dialog")
        expect(dialog).to_be_visible()

        page.locator("#onboarding-dots button").nth(1).click()
        step_two = dialog.bounding_box()
        page.locator("#onboarding-dots button").nth(2).click()
        step_three = dialog.bounding_box()

        assert step_two is not None and step_three is not None
        assert step_three["width"] == pytest.approx(step_two["width"], abs=1)
        assert step_three["height"] == pytest.approx(step_two["height"], abs=1)
    finally:
        context.close()


def test_scope_control_distinguishes_applied_draft_and_invalid_states(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    subject = f"scope-state-{uuid4().hex[:8]}"
    try:
        _open_console(page, running_console)
        control = page.locator(".scope-control")
        save = page.locator("#save-scope")
        status = page.locator("#scope-status-label")
        subject_input = page.locator("#subject-id")

        expect(page.locator("#scope-control-label")).to_have_text("开发作用域")
        expect(status).to_have_text("已应用")
        expect(page.locator("#scope-save-label")).to_have_text("当前")
        expect(save).to_be_disabled()
        expect(page.locator("#tenant-id")).to_have_attribute("maxlength", "128")
        expect(subject_input).to_have_attribute("maxlength", "128")

        guide_box = page.locator("#open-onboarding").bounding_box()
        control_box = control.bounding_box()
        assert guide_box is not None and control_box is not None
        assert guide_box["height"] == pytest.approx(48, abs=1)
        assert control_box["height"] == pytest.approx(48, abs=1)
        assert control_box["width"] < 350

        subject_input.fill(subject)
        expect(control).to_have_class("scope-control is-dirty")
        expect(status).to_have_text("待应用")
        expect(page.locator("#scope-save-label")).to_have_text("应用")
        expect(save).to_be_enabled()

        save.click()
        expect(page.locator("#stat-subject")).to_have_text(subject)
        expect(control).to_have_class("scope-control")
        expect(status).to_have_text("已应用")
        expect(save).to_be_disabled()

        subject_input.fill("")
        expect(control).to_have_class("scope-control is-invalid")
        expect(status).to_have_text("请补全")
        expect(subject_input).to_have_attribute("aria-invalid", "true")
        expect(save).to_be_enabled()
        save.click()
        expect(subject_input).to_be_focused()
        expect(page.get_by_text("无法应用作用域", exact=True)).to_be_visible()
    finally:
        context.close()


def test_journey_progress_is_restored_per_scope(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    tenant = f"journey-{uuid4().hex[:8]}"
    subject = f"subject-{uuid4().hex[:8]}"
    empty_subject = f"empty-{uuid4().hex[:8]}"
    try:
        _open_console(page, running_console)
        page.locator("#tenant-id").fill(tenant)
        page.locator("#subject-id").fill(subject)
        page.locator("#save-scope").click()

        page.locator('[data-view="capture"]').click()
        page.locator('#memory-form input[name="key"]').fill("drink.preference")
        page.locator('#memory-form input[name="value"]').fill("decaf coffee")
        page.locator('#memory-form textarea[name="evidence_text"]').fill(
            "I prefer decaf coffee in the evening"
        )
        page.locator('#memory-form button[type="submit"]').click()
        expect(page.locator("#capture-result")).to_be_visible()

        page.locator('[data-view="memories"]').click()
        expect(page.locator(".memory-card")).to_have_count(1)
        page.locator('[data-view="recall"]').click()
        page.locator('#recall-form input[name="query"]').fill("decaf coffee")
        page.locator('#recall-form button[type="submit"]').click()
        expect(page.locator(".result-card")).to_have_count(1)
        page.locator(".result-card .positive").click()
        expect(page.locator(".utility-update")).to_be_visible()
        expect(page.locator("#journey-progress")).to_have_text("已完成 5 / 5")

        page.reload(wait_until="networkidle")
        expect(page.locator("#status-label")).to_have_text("API 在线")
        expect(page.locator("#journey-progress")).to_have_text("已完成 5 / 5")

        page.locator("#subject-id").fill(empty_subject)
        page.locator("#save-scope").click()
        expect(page.locator("#memory-count")).to_have_text("0 条记忆")
        expect(page.locator("#journey-progress")).to_have_text("已完成 1 / 5")

        page.locator("#subject-id").fill(subject)
        page.locator("#save-scope").click()
        expect(page.locator("#memory-count")).to_have_text("1 条记忆")
        expect(page.locator("#journey-progress")).to_have_text("已完成 5 / 5")
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


def test_mobile_navigation_has_a_complete_keyboard_focus_lifecycle(
    chromium_browser: Browser,
    running_console: RunningConsole,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        _open_console(page, running_console)
        sidebar = page.locator("#sidebar")
        menu = page.locator("#mobile-menu")
        current_navigation = page.locator('.nav-item[data-view="overview"]')

        expect(sidebar).to_have_attribute("inert", "")
        expect(sidebar).to_have_attribute("aria-hidden", "true")
        page.keyboard.press("Tab")
        expect(page.locator(".skip-link")).to_be_focused()
        page.keyboard.press("Tab")
        expect(menu).to_be_focused()

        page.keyboard.press("Enter")
        expect(menu).to_have_attribute("aria-expanded", "true")
        expect(sidebar).to_have_attribute("aria-hidden", "false")
        assert not page.evaluate("document.querySelector('#sidebar').hasAttribute('inert')")
        expect(current_navigation).to_be_focused()

        page.keyboard.press("Shift+Tab")
        expect(page.locator("#retry-health")).to_be_focused()
        page.keyboard.press("Tab")
        expect(current_navigation).to_be_focused()

        page.keyboard.press("Escape")
        expect(sidebar).to_have_attribute("aria-hidden", "true")
        expect(sidebar).to_have_attribute("inert", "")
        expect(menu).to_be_focused()

        menu.click()
        page.locator("#mobile-scrim").click()
        expect(menu).to_be_focused()
        expect(sidebar).to_have_attribute("inert", "")

        menu.click()
        page.locator('.nav-item[data-view="capture"]').click()
        expect(page.locator("#view-capture")).to_have_class("view is-active")
        expect(menu).to_be_focused()
        expect(sidebar).to_have_attribute("inert", "")

        menu.click()
        page.set_viewport_size({"width": 1000, "height": 844})
        expect(sidebar).to_have_class("sidebar")
        assert not page.evaluate("document.querySelector('#sidebar').hasAttribute('inert')")
        assert not page.evaluate("document.querySelector('#sidebar').hasAttribute('aria-hidden')")
        expect(menu).to_have_attribute("aria-expanded", "false")

        page.set_viewport_size({"width": 390, "height": 844})
        expect(sidebar).to_have_attribute("inert", "")
        expect(sidebar).to_have_attribute("aria-hidden", "true")
    finally:
        context.close()

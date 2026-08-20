"""用真实剧本选择页脚本验收迁移后的核心交互。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect


STORY = {
    "story_id": "numeric_browser_story",
    "title": "雨巷来信",
    "author": "N.E.K.O",
    "language": "zh-CN",
    "revision": 3,
    "display_intro": {
        "background": "多年后，你回到雨季小镇，一封没有寄出的信仍放在旧花店里。",
        "player_identity": "你是回乡整理旧屋的故人。",
        "catgirl_identity": "小葵是守着花店和旧信的店主。",
    },
}


def _fulfill(route: Route, payload: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))


def _install_selector_routes(
    page: Page,
    *,
    session: dict | None = None,
    deleted: dict[str, bool] | None = None,
    start_calls: list[dict] | None = None,
    start_result: dict | None = None,
    resume_result: dict | None = None,
) -> None:
    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            stories = [] if deleted and deleted["value"] else [STORY]
            _fulfill(route, {"ok": True, "stories": stories})
            return
        if path.endswith("/api/theater-numeric/session/active"):
            if session is None:
                _fulfill(route, {"ok": False, "reason": "numeric_session_not_found"}, 404)
            else:
                _fulfill(route, {"ok": True, "session": session, "archive_status": "written"})
            return
        if path.endswith("/api/theater-numeric/session/start"):
            if start_calls is not None:
                start_calls.append(json.loads(request.post_data or "{}"))
            if start_result is None:
                _fulfill(route, {"ok": False, "reason": "unexpected_start"}, 409)
            else:
                _fulfill(route, start_result)
            return
        if path.endswith("/api/theater-numeric/session/resume"):
            if resume_result is None:
                _fulfill(route, {"ok": False, "reason": "unexpected_resume"}, 409)
            else:
                _fulfill(route, resume_result)
            return
        if path.endswith("/delete-preview"):
            _fulfill(route, {"ok": True, "active_catgirl_names": ["小葵"], "session_count": 1})
            return
        if request.method == "DELETE" and path.endswith("/packages/" + STORY["story_id"]):
            if deleted is not None:
                deleted["value"] = True
            _fulfill(route, {"ok": True, "deleted_session_count": 1})
            return
        route.continue_()

    page.route("**/api/theater-numeric/**", handler)


@pytest.mark.frontend
def test_selector_shows_story_summary_roles_and_new_session_actions(mock_page: Page, running_server: str):
    _install_selector_routes(mock_page)
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    expect(mock_page.locator(".theater-story-card")).to_have_count(1)
    expect(mock_page.locator("#theater-empty-state")).to_be_hidden()
    expect(mock_page.locator("#theater-detail-placeholder")).to_be_hidden()
    expect(mock_page.locator("#theater-detail-title")).to_have_text("雨巷来信")
    expect(mock_page.locator("#theater-detail-background")).to_contain_text("没有寄出的信")
    expect(mock_page.locator("#theater-detail-player")).to_contain_text("回乡整理旧屋")
    expect(mock_page.locator("#theater-detail-catgirl")).to_contain_text("小葵")
    expect(mock_page.locator("#theater-start-btn")).to_be_enabled()
    expect(mock_page.locator("#theater-start-btn")).to_have_text("开始")
    expect(mock_page.locator("#theater-start-btn")).to_have_attribute("data-i18n", "theater.start")
    expect(mock_page.locator("#theater-continue-btn")).to_be_disabled()
    expect(mock_page.locator("#theater-session-hint")).to_contain_text("点击“开始”")
    expect(mock_page.locator("#theater-restart-btn")).to_have_count(0)
    expect(mock_page.locator("#theater-delete-btn")).to_be_enabled()
    actions_box = mock_page.locator(".theater-detail-actions").bounding_box()
    intro_box = mock_page.locator(".theater-intro-background").bounding_box()
    assert actions_box is not None and intro_box is not None
    assert actions_box["y"] < intro_box["y"]


@pytest.mark.frontend
def test_active_story_only_enables_continue(mock_page: Page, running_server: str):
    _install_selector_routes(
        mock_page,
        session={"session_id": "active-session", "revision": 4, "status": "active"},
    )
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    expect(mock_page.locator("#theater-start-btn")).to_be_disabled()
    expect(mock_page.locator("#theater-start-btn")).to_have_text("重新开始")
    expect(mock_page.locator("#theater-start-btn")).to_have_attribute("data-i18n", "theater.restartSession")
    expect(mock_page.locator("#theater-continue-btn")).to_be_enabled()
    expect(mock_page.locator("#theater-session-hint")).to_contain_text("点击“继续”")
    expect(mock_page.locator("#theater-restart-btn")).to_have_count(0)


@pytest.mark.frontend
def test_user_exit_story_can_continue_same_session(mock_page: Page, running_server: str):
    _install_selector_routes(
        mock_page,
        session={
            "session_id": "paused-session",
            "revision": 4,
            "status": "ended",
            "ended_reason": "user_exit",
        },
        resume_result={
            "ok": True,
            "resumed": True,
            "session": {
                "session_id": "paused-session",
                "story_package_id": STORY["story_id"],
                "revision": 4,
                "status": "active",
                "ended_reason": None,
            },
        },
    )
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    expect(mock_page.locator("#theater-start-btn")).to_be_enabled()
    expect(mock_page.locator("#theater-start-btn")).to_have_text("重新开始")
    expect(mock_page.locator("#theater-continue-btn")).to_be_enabled()
    expect(mock_page.locator("#theater-session-badge")).to_have_text("已退出")
    expect(mock_page.locator("#theater-session-hint")).to_contain_text("继续原进度")
    with mock_page.expect_request("**/api/theater-numeric/session/resume") as request_info:
        mock_page.locator("#theater-continue-btn").click()
    assert json.loads(request_info.value.post_data or "{}") == {
        "story_id": STORY["story_id"],
        "session_id": "paused-session",
        "base_revision": 4,
    }


@pytest.mark.frontend
def test_ended_story_start_replaces_session_after_confirmation(mock_page: Page, running_server: str):
    _install_selector_routes(
        mock_page,
        session={"session_id": "ended-session", "revision": 4, "status": "ended"},
        start_result={
            "ok": True,
            "session": {
                "session_id": "replacement-session",
                "story_package_id": STORY["story_id"],
                "revision": 0,
                "status": "active",
            },
        },
    )
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    expect(mock_page.locator("#theater-start-btn")).to_be_enabled()
    expect(mock_page.locator("#theater-start-btn")).to_have_text("重新开始")
    expect(mock_page.locator("#theater-continue-btn")).to_be_disabled()
    mock_page.locator("#theater-start-btn").click()

    expect(mock_page.locator("#theater-modal")).to_be_visible()
    expect(mock_page.locator("#theater-modal-title")).to_have_text("开始新的演绎？")
    expect(mock_page.locator("#theater-modal-body")).to_contain_text("替换当前角色的已结束记录")
    expect(mock_page.locator(".theater-modal")).to_be_focused()
    assert mock_page.locator("#theater-modal-title").get_attribute("tabindex") is None
    expect(mock_page.locator("#theater-modal-cancel")).to_have_css("border-top-width", "0px")
    expect(mock_page.locator("#theater-modal-confirm")).to_have_css("color", "rgb(255, 255, 255)")
    mock_page.locator("#theater-modal-cancel").click()
    expect(mock_page.locator("#theater-start-btn")).to_be_focused()
    mock_page.locator("#theater-start-btn").click()
    with mock_page.expect_request("**/api/theater-numeric/session/start") as request_info:
        mock_page.locator("#theater-modal-confirm").click()

    start_payload = json.loads(request_info.value.post_data or "{}")
    assert start_payload["story_id"] == STORY["story_id"]
    assert start_payload["replace_existing"] is True
    assert start_payload["session_id"] != "ended-session"


@pytest.mark.frontend
def test_selector_header_keeps_title_horizontal_on_narrow_viewport(mock_page: Page, running_server: str):
    """窄窗口保留横向标题和可见窗口控制，不把标题挤成竖排。"""  # noqa: DOCSTRING_CJK

    mock_page.set_viewport_size({"width": 375, "height": 720})
    _install_selector_routes(mock_page)
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    title_box = mock_page.locator(".theater-title-copy h2").bounding_box()
    controls_box = mock_page.locator(".theater-window-controls").bounding_box()
    assert title_box is not None and controls_box is not None
    assert title_box["width"] > title_box["height"]
    assert controls_box["x"] + controls_box["width"] <= 375


@pytest.mark.frontend
def test_delete_story_warns_about_active_character_and_removes_card(mock_page: Page, running_server: str):
    deleted = {"value": False}
    _install_selector_routes(mock_page, deleted=deleted)
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")

    mock_page.locator("#theater-delete-btn").click()
    expect(mock_page.locator("#theater-modal-body")).to_contain_text("小葵")
    expect(mock_page.locator("#theater-modal-body")).to_contain_text("还未结束")
    mock_page.locator("#theater-modal-confirm").click()

    expect(mock_page.locator(".theater-story-card")).to_have_count(0)
    expect(mock_page.locator("#theater-selector-status")).to_contain_text("剧本已删除")
    assert deleted["value"] is True


@pytest.mark.frontend
def test_post_end_receipt_prompts_memory_on_selector_and_archives_once(
    mock_page: Page,
    running_server: str,
):
    archive_calls: list[dict] = []
    archive_status = {"value": "written"}
    ended_session = {
        "session_id": "ended-session",
        "revision": 5,
        "status": "ended",
        "ended_reason": "user_exit",
    }

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            _fulfill(route, {"ok": True, "stories": [STORY]})
            return
        if path.endswith("/api/theater-numeric/session/active"):
            _fulfill(route, {
                "ok": True,
                "session": ended_session,
                "end_receipt_id": "theater_end_browser_receipt",
                "archive_request_id": "theater_archive_browser_receipt",
                "archive_status": archive_status["value"],
            })
            return
        if path.endswith("/api/theater-numeric/session/archive"):
            archive_calls.append(json.loads(request.post_data or "{}"))
            _fulfill(route, {"ok": True, "status": "written"})
            return
        route.continue_()

    mock_page.route("**/api/theater-numeric/**", handler)
    mock_page.goto(f"{running_server}/theater", wait_until="domcontentloaded")
    expect(mock_page.locator("#theater-detail-title")).to_have_text("雨巷来信")

    archive_status["value"] = "pending"
    mock_page.evaluate(
        """() => window.postMessage({
            schema: 'neko.theater.interpage.v1',
            action: 'theater:post-end',
            story_id: 'numeric_browser_story',
            session_id: 'ended-session',
            revision: 5,
            end_receipt_id: 'theater_end_browser_receipt'
        }, window.location.origin)"""
    )

    expect(mock_page.locator("#theater-modal")).to_be_visible()
    expect(mock_page.locator("#theater-modal-title")).to_contain_text("记下本次演绎内容")
    expect(mock_page.locator(".theater-modal")).to_be_focused()
    expect(mock_page.locator("#theater-modal-cancel")).to_have_css("border-top-width", "0px")
    mock_page.locator("#theater-modal-confirm").click()

    expect(mock_page.locator("#theater-modal")).to_be_hidden()
    expect(mock_page.locator("#theater-inline-feedback")).to_contain_text("本次演绎已记下")
    assert len(archive_calls) == 1
    assert archive_calls[0]["story_id"] == STORY["story_id"]
    assert archive_calls[0]["session_id"] == "ended-session"
    assert archive_calls[0]["revision"] == 5
    assert archive_calls[0]["end_receipt_id"] == "theater_end_browser_receipt"
    assert archive_calls[0]["archive_request_id"].startswith("theater_archive_")

    archive_status["value"] = "written"
    mock_page.evaluate(
        """() => window.postMessage({
            schema: 'neko.theater.interpage.v1',
            action: 'theater:post-end',
            story_id: 'numeric_browser_story',
            session_id: 'ended-session',
            revision: 5,
            end_receipt_id: 'theater_end_browser_receipt'
        }, window.location.origin)"""
    )
    expect(mock_page.locator("#theater-modal")).to_be_hidden()
    assert len(archive_calls) == 1

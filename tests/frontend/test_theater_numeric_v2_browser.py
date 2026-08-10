"""用真实页面脚本验收 Numeric v2 阅读器布局与回合分组。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect


STORY = {
    "story_id": "numeric_browser_story",
    "title": "雨巷来信",
    "intro": {
        "background": "多年后，你回到雨季小镇。旧花店仍亮着灯，一封没有寄出的信把你们重新带到同一扇门前。",
        "player_identity": "你是回乡整理旧屋的故人。",
        "catgirl_identity": "她是守着花店和旧信的店主。",
    },
}


def _snapshot(
    *,
    revision: int,
    history: list[dict],
    status: str = "active",
    terminal: bool = False,
    ending: dict | None = None,
) -> dict:
    """构造页面需要的公开快照，不暴露隐藏数值。"""  # noqa: DOCSTRING_CJK

    return {
        "ok": True,
        "story_intro": STORY["intro"],
        "scene": {"id": "ending_normal" if terminal else "mainline_01", "terminal": terminal, "ending": ending},
        "suggested_inputs": ["走进花店，问她是否还记得自己", "先看一眼柜台上的旧信"],
        "session": {
            "session_id": "numeric_browser_session",
            "story_package_id": STORY["story_id"],
            "revision": revision,
            "status": status,
            "opening_performance": {
                "narration": "雨丝停在花店檐角，她抬眼看向门口。",
                "dialogue": [{"speaker_id": "active_catgirl", "text": "要进来避一会儿雨吗？"}],
                "suggested_inputs": [],
            },
            "performance_history": history,
        },
    }


def _install_routes(page: Page) -> None:
    """替换 Numeric v2 API，保留真实模板、样式和页面脚本。"""  # noqa: DOCSTRING_CJK

    def fulfill(route: Route, payload: dict) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            fulfill(route, {"ok": True, "stories": [STORY]})
            return
        if path.endswith("/api/theater-numeric/session/start"):
            fulfill(route, _snapshot(revision=0, history=[]))
            return
        if path.endswith("/api/theater-numeric/session/input"):
            body = json.loads(request.post_data or "{}")
            terminal = body["message"] == "到达结局"
            fulfill(route, _snapshot(
                revision=1,
                status="ended" if terminal else "active",
                terminal=terminal,
                ending={"id": "ending_normal", "title": "雨后的重逢", "summary": "你们终于把未寄出的信读完。"} if terminal else None,
                history=[{
                "input_text": body["message"],
                "narration": "她把旧信轻轻推到灯下，纸边留下浅淡的水痕。",
                "dialogue": [{"speaker_id": "active_catgirl", "text": "我记得，只是不知道你还会不会回来。"}],
                "suggested_inputs": [],
                }],
            ))
            return
        route.continue_()

    page.route("**/api/theater-numeric/**", handler)


@pytest.mark.frontend
def test_numeric_v2_page_groups_each_turn_and_keeps_stage_collapsible(mock_page: Page, running_server: str):
    """玩家输入与对应回应属于同一组，建议靠近输入区，舞台可独立折叠。"""  # noqa: DOCSTRING_CJK

    mock_page.set_viewport_size({"width": 1280, "height": 820})
    mock_page.emulate_media(reduced_motion="reduce")
    mock_page.add_init_script("window.localStorage.clear();")
    _install_routes(mock_page)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")

    expect(mock_page.locator("#numeric-theater-story-select option")).to_have_count(1)
    expect(mock_page.locator("#numeric-theater-intro-background")).to_contain_text("一封没有寄出的信")
    stage_box = mock_page.locator(".numeric-theater-stage").bounding_box()
    console_box = mock_page.locator(".numeric-theater-console").bounding_box()
    assert stage_box and console_box and stage_box["x"] < console_box["x"]

    mock_page.locator("#numeric-theater-start-btn").click()
    expect(mock_page.locator(".numeric-theater-response.opening")).to_contain_text("要进来避一会儿雨吗")
    expect(mock_page.locator("#numeric-theater-choice-panel")).to_be_visible()

    mock_page.locator("#numeric-theater-input").fill("我推门进去，问她这些年过得怎么样。")
    mock_page.locator("#numeric-theater-send-btn").click()
    exchange = mock_page.locator(".numeric-theater-exchange")
    expect(exchange).to_have_count(1)
    expect(exchange.locator(".theater-turn.user")).to_contain_text("我推门进去")
    expect(exchange.locator(".numeric-theater-response")).to_contain_text("我记得，只是不知道你还会不会回来")

    choices_box = mock_page.locator("#numeric-theater-choice-panel").bounding_box()
    input_box = mock_page.locator("#numeric-theater-input-form").bounding_box()
    assert choices_box and input_box and choices_box["y"] < input_box["y"]
    assert input_box["y"] + input_box["height"] <= console_box["y"] + console_box["height"]

    mock_page.locator("#numeric-theater-stage-toggle").click()
    expect(mock_page.locator("[data-numeric-theater-app]")).to_have_attribute("data-stage-collapsed", "true")
    collapsed_box = mock_page.locator(".numeric-theater-stage").bounding_box()
    assert collapsed_box and collapsed_box["width"] < stage_box["width"]


@pytest.mark.frontend
def test_numeric_v2_page_renders_terminal_ending(mock_page: Page, running_server: str):
    """终局快照中的作者结局必须在 Numeric 页面可见，并关闭继续输入。"""  # noqa: DOCSTRING_CJK

    mock_page.add_init_script("window.localStorage.clear();")
    _install_routes(mock_page)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")

    mock_page.locator("#numeric-theater-start-btn").click()
    mock_page.locator("#numeric-theater-input").fill("到达结局")
    mock_page.locator("#numeric-theater-send-btn").click()

    ending = mock_page.locator("#numeric-theater-ending-panel")
    expect(ending).to_be_visible()
    expect(ending).to_contain_text("雨后的重逢")
    expect(ending).to_contain_text("你们终于把未寄出的信读完")
    expect(mock_page.locator("#numeric-theater-input")).to_be_disabled()
    expect(mock_page.locator("#numeric-theater-choice-panel")).to_be_hidden()

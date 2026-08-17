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
                "content": [
                    {"type": "narration", "text": "雨丝停在花店檐角，她抬眼看向门口。"},
                    {"type": "dialogue", "speaker_id": "active_catgirl", "text": "要进来避一会儿雨吗？"},
                ],
                "suggested_inputs": [],
            },
            "performance_history": history,
        },
    }


def _install_routes(page: Page, *, start_bodies: list[dict] | None = None) -> None:
    """替换 Numeric v2 API，保留真实模板、样式和页面脚本。"""  # noqa: DOCSTRING_CJK

    current_session_id = {"value": "numeric_browser_session"}

    def fulfill(route: Route, payload: dict) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def current_snapshot(**kwargs) -> dict:
        payload = _snapshot(**kwargs)
        payload["session"]["session_id"] = current_session_id["value"]
        return payload

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            fulfill(route, {"ok": True, "stories": [STORY]})
            return
        if path.endswith("/api/theater-numeric/session/active"):
            fulfill(route, {"ok": False, "reason": "numeric_session_not_found"})
            return
        if path.endswith("/api/theater-numeric/session/start"):
            body = json.loads(request.post_data or "{}")
            if start_bodies is not None:
                start_bodies.append(body)
            current_session_id["value"] = body["session_id"]
            fulfill(route, current_snapshot(revision=0, history=[]))
            return
        if path.endswith("/api/theater-numeric/session/end"):
            fulfill(route, current_snapshot(revision=0, history=[], status="ended"))
            return
        if path.endswith("/api/theater-numeric/session/input"):
            body = json.loads(request.post_data or "{}")
            terminal = body["message"] == "到达结局"
            fulfill(route, current_snapshot(
                revision=1,
                status="ended" if terminal else "active",
                terminal=terminal,
                ending={"id": "ending_normal", "title": "雨后的重逢", "summary": "你们终于把未寄出的信读完。"} if terminal else None,
                history=[{
                "input_text": body["message"],
                "suggested_inputs": [],
                "segments": [
                    {"phase": "source_response", "content": [
                        {"type": "narration", "text": "她先把旧信轻轻推到灯下。"},
                        {"type": "dialogue", "speaker_id": "active_catgirl", "text": "我记得。"},
                        {"type": "narration", "text": "她的指尖在信封边缘停了一瞬。"},
                        {"type": "dialogue", "speaker_id": "active_catgirl", "text": "只是不知道你还会不会回来。"},
                    ]},
                    {"phase": "transition_bridge", "content": [{"type": "narration", "text": "雨声渐渐停了。"}]},
                    {"phase": "target_opening", "content": [
                        {"type": "narration", "text": "第二天清晨，纸边的水痕已经干透。"},
                        {"type": "dialogue", "speaker_id": "active_catgirl", "text": "现在把它读完吧。"},
                    ]},
                ],
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
    expect(mock_page.locator(".numeric-theater-stage .theater-stage-toggle-icon")).to_have_text("←")
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
    expect(exchange.locator(".numeric-theater-response")).to_contain_text("我记得")
    phases = exchange.locator("[data-transition-phase]")
    expect(phases).to_have_count(3)
    expect(phases.nth(0)).to_contain_text("我记得")
    expect(phases.nth(1)).to_contain_text("雨声渐渐停了")
    expect(phases.nth(2)).to_contain_text("第二天清晨")
    assert phases.nth(0).locator(".theater-turn-block, .theater-turn.dialogue").all_inner_texts() == [
        "她先把旧信轻轻推到灯下。",
        "「我记得。」",
        "她的指尖在信封边缘停了一瞬。",
        "「只是不知道你还会不会回来。」",
    ]

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


@pytest.mark.frontend
def test_numeric_v2_page_replaces_changed_catgirl_session_and_falls_back_from_stale_story(
    mock_page: Page, running_server: str
):
    """角色变化和失效本地 Story 指针都能回到可启动状态。"""  # noqa: DOCSTRING_CJK

    start_bodies: list[dict] = []
    mock_page.add_init_script(
        "window.localStorage.setItem('neko.theater.numeric.v2.session.v2', 'old_session');"
        "window.localStorage.setItem('neko.theater.numeric.v2.story.v2', 'deleted_story');"
    )

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "stories": [STORY]}, ensure_ascii=False),
            )
            return
        if path.endswith("/api/theater-numeric/session/active"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"ok": False, "reason": "catgirl_changed_requires_new_session"},
                    ensure_ascii=False,
                ),
            )
            return
        if path.endswith("/api/theater-numeric/session/start"):
            start_bodies.append(json.loads(request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_snapshot(revision=0, history=[]), ensure_ascii=False),
            )
            return
        route.continue_()

    mock_page.route("**/api/theater-numeric/**", handler)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")
    expect(mock_page.locator("#numeric-theater-story-select")).to_have_value(STORY["story_id"])
    mock_page.locator("#numeric-theater-start-btn").click()
    expect(mock_page.locator(".numeric-theater-response.opening")).to_be_visible()
    assert start_bodies[0]["story_id"] == STORY["story_id"]
    assert start_bodies[0]["replace_existing"] is True


@pytest.mark.frontend
def test_numeric_v2_page_restores_new_catgirls_existing_session_after_switch(
    mock_page: Page, running_server: str
):
    """演绎中切换角色后，页面自动恢复新角色自己的剧本进度。"""  # noqa: DOCSTRING_CJK

    start_bodies: list[dict] = []
    active_calls = {"count": 0}
    mock_page.add_init_script("window.localStorage.clear();")

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "stories": [STORY]}, ensure_ascii=False),
            )
            return
        if path.endswith("/api/theater-numeric/session/active"):
            active_calls["count"] += 1
            if active_calls["count"] == 1:
                payload = {"ok": False, "reason": "numeric_session_not_found"}
            else:
                payload = _snapshot(revision=0, history=[])
                payload["session"]["session_id"] = "new_catgirl_existing_session"
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
            return
        if path.endswith("/api/theater-numeric/session/start"):
            start_bodies.append(json.loads(request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_snapshot(revision=0, history=[]), ensure_ascii=False),
            )
            return
        if path.endswith("/api/theater-numeric/session/input"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"ok": False, "reason": "catgirl_changed_requires_new_session"},
                    ensure_ascii=False,
                ),
            )
            return
        route.continue_()

    mock_page.route("**/api/theater-numeric/**", handler)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")
    mock_page.locator("#numeric-theater-start-btn").click()
    mock_page.locator("#numeric-theater-input").fill("这一轮遇到角色切换。")
    mock_page.locator("#numeric-theater-send-btn").click()

    expect(mock_page.locator("#numeric-theater-start-btn")).to_be_disabled()
    expect(mock_page.locator("#numeric-theater-input")).to_be_enabled()
    assert len(start_bodies) == 1
    assert mock_page.evaluate(
        "window.localStorage.getItem('neko.theater.numeric.v2.session.v2')"
    ) == "new_catgirl_existing_session"


@pytest.mark.frontend
def test_numeric_v2_page_warns_for_unfinished_roles_before_deleting_story(
    mock_page: Page,
    running_server: str,
):
    """删除剧本前展示未结束角色名，确认后刷新为空列表。"""  # noqa: DOCSTRING_CJK

    deleted = {"value": False}
    active_names = {"value": []}
    mock_page.add_init_script("window.localStorage.clear();")

    def handler(route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater-numeric/stories"):
            stories = [] if deleted["value"] else [STORY]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "stories": stories}, ensure_ascii=False),
            )
            return
        if path.endswith("/api/theater-numeric/session/active"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": False, "reason": "numeric_session_not_found"}),
            )
            return
        if path.endswith("/delete-preview"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "active_catgirl_names": active_names["value"],
                        "session_count": 1,
                    },
                    ensure_ascii=False,
                ),
            )
            return
        if request.method == "DELETE" and path.endswith("/packages/" + STORY["story_id"]):
            deleted["value"] = True
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "deleted_session_count": 1}),
            )
            return
        route.continue_()

    mock_page.route("**/api/theater-numeric/**", handler)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")

    def cancel_plain_delete(dialog) -> None:
        assert dialog.message == "是否确认删除？"
        dialog.dismiss()

    mock_page.once("dialog", cancel_plain_delete)
    mock_page.locator("#numeric-theater-delete-btn").click()
    expect(mock_page.locator("#numeric-theater-delete-btn")).to_be_enabled()
    assert deleted["value"] is False

    active_names["value"] = ["小葵"]

    def accept_delete(dialog) -> None:
        assert "小葵" in dialog.message
        assert "还未结束" in dialog.message
        dialog.accept()

    mock_page.once("dialog", accept_delete)
    mock_page.locator("#numeric-theater-delete-btn").click()

    expect(mock_page.locator("#numeric-theater-story-select option")).to_have_count(0)
    expect(mock_page.locator("#numeric-theater-status")).to_contain_text("剧本已删除")
    assert deleted["value"] is True


@pytest.mark.frontend
def test_numeric_v2_page_can_start_after_end_and_restart_after_terminal(
    mock_page: Page, running_server: str
):
    """结束后的开始和终局后的重新开始都创建可继续输入的新 Session。"""  # noqa: DOCSTRING_CJK

    start_bodies: list[dict] = []
    mock_page.add_init_script("window.localStorage.clear();")
    _install_routes(mock_page, start_bodies=start_bodies)
    mock_page.goto(f"{running_server}/theater-numeric", wait_until="domcontentloaded")

    mock_page.locator("#numeric-theater-start-btn").click()
    mock_page.locator("#numeric-theater-end-btn").click()
    expect(mock_page.locator("#numeric-theater-log")).to_be_empty()
    expect(mock_page.locator("#numeric-theater-start-btn")).to_be_enabled()
    mock_page.locator("#numeric-theater-start-btn").click()

    assert len(start_bodies) == 2
    assert start_bodies[1]["replace_existing"] is True
    assert start_bodies[1]["session_id"] != start_bodies[0]["session_id"]
    expect(mock_page.locator("#numeric-theater-input")).to_be_enabled()
    mock_page.locator("#numeric-theater-input").fill("继续推进")
    mock_page.locator("#numeric-theater-send-btn").click()
    expect(mock_page.locator(".numeric-theater-exchange")).to_contain_text("我记得")

    mock_page.locator("#numeric-theater-input").fill("到达结局")
    mock_page.locator("#numeric-theater-send-btn").click()
    expect(mock_page.locator("#numeric-theater-restart-btn")).to_be_visible()
    mock_page.locator("#numeric-theater-restart-btn").click()

    assert len(start_bodies) == 3
    assert start_bodies[2]["replace_existing"] is True
    assert start_bodies[2]["session_id"] != start_bodies[1]["session_id"]
    expect(mock_page.locator("#numeric-theater-input")).to_be_enabled()

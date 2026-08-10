"""用真实页面脚本验收独立自由模式页面和 RP-Hub 临时角色卡入口。"""  # noqa: DOCSTRING_CJK

import json

import pytest
from playwright.sync_api import Page, Route, expect


STORY_CARD = {
    "id": "browser_free_story",
    "title": "自由模式浏览器回归",
    "background": "一间用于回归测试的安静房间。",
    "initial_scene": {
        "scene_id": "scene_browser_setup",
        "title": "测试房间",
        "text": "窗边的灯保持稳定亮起。",
    },
    "scenario_card": {
        "player_role": "测试玩家",
        "catgirl_role": "测试猫娘",
        "primary_goal": "确认自由模式边界",
    },
}


def _payload(*, revision: int, history: list[dict], ending: dict | None = None) -> dict:
    """构造自由页面只需要的公开快照，不携带旧剧本字段。"""  # noqa: DOCSTRING_CJK
    return {
        "ok": True,
        "mode": "free",
        "session_id": "theater_browser_free",
        "story_id": STORY_CARD["id"],
        "state_revision": revision,
        "free_text": history[-1]["text"] if history and history[-1].get("role") == "narrator" else "",
        "free_role_card": {
            "name": "测试猫娘",
            "description": "当前自由临时角色卡",
            "story_title": "临时自由卡",
            "scenario_title": "自由开场",
            "scenario": "自由测试场景",
            "player_address": "测试玩家",
            "player_role": "测试玩家",
        },
        "free_history": history,
        "ending": ending or {"should_offer_ending": False, "should_end_session": False},
        "can_resume": True,
        "session_lifecycle": "active",
        "stale": False,
    }


def _install_free_routes(
    page: Page,
    *,
    start_response: dict | None = None,
    start_unknown_response: bool = False,
    input_unknown_response: bool = False,
) -> dict[str, object]:
    """替换自由 API，保留真实页面脚本、DOM 和幂等重试逻辑。"""  # noqa: DOCSTRING_CJK
    state: dict[str, object] = {
        "fallback_active": False,
        "free_failed": False,
        "start_bodies": [],
        "input_bodies": [],
    }
    opening = _payload(
        revision=0,
        history=[
            {"role": "narrator", "text": "自由模式开场。"},
            {"role": "narrator", "text": "你可以直接描述行动。"},
        ],
    )
    turn = _payload(
        revision=1,
        history=[
            {"role": "narrator", "text": "自由模式开场。"},
            {"role": "user", "text": "我把记录卡递给她。"},
            {"role": "narrator", "text": "我看见了，这条线索很有用。"},
        ],
    )

    def fulfill(route: Route, payload: dict) -> None:
        """统一返回 JSON，测试重点保持在自由页面协议。"""  # noqa: DOCSTRING_CJK
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    def handler(route: Route) -> None:
        """只响应自由模式 API，旧 Story v3 Session 地址不会再出现。"""  # noqa: DOCSTRING_CJK
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/theater/stories"):
            fulfill(route, {"ok": True, "stories": [STORY_CARD]})
            return
        if path.endswith("/api/theater/free/session/active"):
            fulfill(route, turn if state["fallback_active"] else {"ok": False, "reason": "session_not_found"})
            return
        if path.endswith("/api/theater/free/session/state"):
            fulfill(route, turn)
            return
        if path.endswith("/api/theater/free/session/start"):
            body = json.loads(request.post_data or "{}")
            state["start_bodies"].append(body)
            if start_unknown_response and len(state["start_bodies"]) == 1:
                route.fulfill(status=200, content_type="text/plain", body="not-json")
                return
            fulfill(route, start_response or opening)
            return
        if path.endswith("/api/theater/free/session/input"):
            body = json.loads(request.post_data or "{}")
            state["input_bodies"].append(body)
            if "user_exit" in (request.post_data or ""):
                exited = dict(turn)
                exited.update({
                    "ending": {"should_offer_ending": False, "should_end_session": True, "reason": "user_exit", "ending_type": "none"},
                    "can_resume": False,
                    "session_lifecycle": "ended",
                })
                fulfill(route, exited)
                return
            if input_unknown_response and len(state["input_bodies"]) == 1:
                route.fulfill(status=200, content_type="text/plain", body="not-json")
                return
            if state["free_failed"]:
                fulfill(route, {"ok": False, "reason": "free_actor_unavailable"})
            else:
                state["free_failed"] = True
                fulfill(route, turn)
            return
        route.continue_()

    page.route("**/api/theater/**", handler)
    return state


def _open_free_page(page: Page, running_server: str) -> None:
    """打开自由页面并等待背景列表加载完成。"""  # noqa: DOCSTRING_CJK
    page.add_init_script("window.localStorage.clear();")
    page.goto(f"{running_server}/theater", wait_until="domcontentloaded")
    expect(page.locator("#theater-story-select option").first).to_be_attached(timeout=10000)


@pytest.mark.frontend
def test_free_page_restores_history_and_keeps_failed_draft(mock_page: Page, running_server: str):
    """自由页面刷新后恢复独立历史，模型失败时保留玩家草稿。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page)
    mock_page.emulate_media(reduced_motion="reduce")
    _open_free_page(mock_page, running_server)
    mock_page.locator("#theater-start-btn").click()
    expect(mock_page.locator("#theater-mode-badge")).to_have_text("自由模式")
    mock_page.locator("#theater-input").fill("我把记录卡递给她。")
    mock_page.locator("#theater-send-btn").click()
    expect(mock_page.locator("#theater-log")).to_contain_text("我把记录卡递给她。")

    state["fallback_active"] = True
    mock_page.reload(wait_until="domcontentloaded")
    expect(mock_page.locator("#theater-log")).to_contain_text("我看见了，这条线索很有用。")
    expect(mock_page.locator("#theater-story-intro")).to_contain_text("临时自由卡")

    mock_page.locator("#theater-input").fill("这次模型失败时也要保留我这句话。")
    mock_page.locator("#theater-send-btn").click()
    expect(mock_page.locator("#theater-input")).to_have_value("这次模型失败时也要保留我这句话。")
    expect(mock_page.locator("#theater-status")).to_contain_text("暂时不可用")


@pytest.mark.frontend
def test_free_page_reuses_turn_id_after_unknown_response(mock_page: Page, running_server: str):
    """自由回合响应丢失后，原样重试复用同一个客户端回合 ID。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page, input_unknown_response=True)
    mock_page.emulate_media(reduced_motion="reduce")
    _open_free_page(mock_page, running_server)
    mock_page.locator("#theater-start-btn").click()
    message = "这条输入的响应虽然丢失，但服务端可能已经处理。"
    mock_page.locator("#theater-input").fill(message)
    mock_page.locator("#theater-send-btn").click()
    expect(mock_page.locator("#theater-input")).to_have_value(message)
    mock_page.locator("#theater-send-btn").click()
    expect(mock_page.locator("#theater-log")).to_contain_text("我看见了，这条线索很有用。")
    assert len(state["input_bodies"]) == 2
    assert state["input_bodies"][0]["client_turn_id"] == state["input_bodies"][1]["client_turn_id"]


@pytest.mark.frontend
def test_free_page_imports_temporary_rp_hub_card(mock_page: Page, running_server: str):
    """自由开场前可暂时上传 RP-Hub JSON，且只发送到自由接口。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page)
    _open_free_page(mock_page, running_server)
    role_card = {"data": {"name": "顾映荷", "description": "原角色简介", "personality": "原角色性格", "first_mes": "顾映荷看向三师兄。", "scenario": "架空江湖", "player_address": "三师兄", "player_role": "三师兄"}}
    mock_page.locator("#theater-role-card-file").set_input_files({"name": "rp-hub-card.json", "mimeType": "application/json", "buffer": json.dumps(role_card, ensure_ascii=False).encode("utf-8")})
    expect(mock_page.locator("#theater-role-card-name")).to_contain_text("rp-hub-card.json")
    mock_page.locator("#theater-start-btn").click()
    # 等待真实页面完成异步开场请求后再读取拦截器记录，避免把网络竞态误报成协议失败。
    expect(mock_page.locator("#theater-log")).to_contain_text("你可以直接描述行动。")
    assert state["start_bodies"][0]["role_card"] == role_card


@pytest.mark.frontend
def test_free_page_reuses_start_id_after_unknown_response(mock_page: Page, running_server: str):
    """自由开场响应丢失后再次点击会复用同一个启动 ID。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page, start_unknown_response=True)
    _open_free_page(mock_page, running_server)
    role_card = {"data": {"name": "顾映荷", "description": "原角色简介", "personality": "原角色性格", "first_mes": "顾映荷看向三师兄。", "scenario": "架空江湖", "player_address": "三师兄", "player_role": "三师兄"}}
    mock_page.locator("#theater-role-card-file").set_input_files({"name": "retry-rp-hub-card.json", "mimeType": "application/json", "buffer": json.dumps(role_card, ensure_ascii=False).encode("utf-8")})
    mock_page.locator("#theater-start-btn").click()
    expect(mock_page.locator("#theater-role-card-name")).to_contain_text("retry-rp-hub-card.json")
    mock_page.locator("#theater-start-btn").click()
    assert len(state["start_bodies"]) == 2
    assert state["start_bodies"][0]["client_start_id"] == state["start_bodies"][1]["client_start_id"]
    assert state["start_bodies"][0]["role_card"] == state["start_bodies"][1]["role_card"] == role_card


@pytest.mark.frontend
def test_free_page_rejects_invalid_role_card_without_sending_it(mock_page: Page, running_server: str):
    """坏 JSON 角色卡不进入自由开场请求。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page)
    _open_free_page(mock_page, running_server)
    mock_page.locator("#theater-role-card-file").set_input_files({"name": "broken-role-card.json", "mimeType": "application/json", "buffer": b"{not-json"})
    expect(mock_page.locator("#theater-role-card-name")).to_be_hidden()
    expect(mock_page.locator("#theater-status")).to_contain_text("角色卡 JSON 无法读取")
    mock_page.locator("#theater-start-btn").click()
    # 客户端拒绝角色卡后仍允许无卡开场；这里等待开场完成再检查请求体没有角色卡。
    expect(mock_page.locator("#theater-log")).to_contain_text("你可以直接描述行动。")
    assert "role_card" not in state["start_bodies"][0]


@pytest.mark.frontend
def test_free_page_handles_server_role_card_rejection(mock_page: Page, running_server: str):
    """服务端角色卡合同失败显示角色卡错误，而不是模型错误。"""  # noqa: DOCSTRING_CJK
    state = _install_free_routes(mock_page, start_response={"ok": False, "reason": "free_role_card_invalid"})
    _open_free_page(mock_page, running_server)
    role_card = {"data": {"name": "顾映荷", "description": "x" * 24001, "personality": "原角色性格", "first_mes": "顾映荷看向三师兄。", "scenario": "架空江湖", "player_address": "三师兄", "player_role": "三师兄"}}
    mock_page.locator("#theater-role-card-file").set_input_files({"name": "too-long-role-card.json", "mimeType": "application/json", "buffer": json.dumps(role_card, ensure_ascii=False).encode("utf-8")})
    mock_page.locator("#theater-start-btn").click()
    expect(mock_page.locator("#theater-status")).to_contain_text("角色卡 JSON 无法读取")
    assert state["start_bodies"][0]["role_card"] == role_card

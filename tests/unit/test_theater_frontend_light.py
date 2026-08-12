"""验证自由页面不再携带 Story v3 剧本模式分支。"""  # noqa: DOCSTRING_CJK

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _sources() -> tuple[str, str]:
    """读取自由页面模板和唯一前端脚本。"""  # noqa: DOCSTRING_CJK
    return (
        (ROOT / "templates" / "theater.html").read_text(encoding="utf-8"),
        (ROOT / "static" / "js" / "theater.js").read_text(encoding="utf-8"),
    )


def _home_source() -> str:
    return (ROOT / "templates" / "theater_home.html").read_text(encoding="utf-8")


def test_theater_mode_selection_copy_hides_internal_implementation_terms():
    """模式选择页只介绍两种演绎方式，不暴露内部实现术语。"""  # noqa: DOCSTRING_CJK
    html = _home_source()
    assert "数值" not in html
    assert "RP-Hub" not in html
    assert "项目优势" not in html
    assert "homeAdvantages" not in html
    assert 'href="/theater"' not in html
    assert 'aria-disabled="true"' in html
    assert 'data-i18n="theater.homeFreeUnavailable"' in html
    assert "💬 自由对话" in html

    for locale in ("en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"):
        payload = json.loads((ROOT / "static" / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        theater = payload["theater"]
        assert "RP-Hub" not in theater["homeFreeDescription"]
        assert "homeAdvantages" not in theater
        assert "homeAdvantagesText" not in theater
        assert theater["homeFreeUnavailable"]
        assert "homeGameplay" not in theater
        assert "homeGameplayText" not in theater


def test_theater_page_is_free_only_and_keeps_role_card_entry():
    """默认 theater 页面只呈现自由模式和临时 RP-Hub 角色卡入口。"""  # noqa: DOCSTRING_CJK
    html, script = _sources()
    assert 'id="theater-mode-badge"' in html
    assert 'data-i18n="theater.freeMode"' in html
    assert 'id="theater-role-card-file"' in html
    assert "RP-Hub" in html and "RP-Hub" in script
    assert 'id="theater-mode-select"' not in html
    assert 'id="theater-delete-story-btn"' not in html
    assert "/api/theater/free/session/start" in script
    assert "/api/theater/free/session/input" in script
    assert "free_history" in script
    assert "/api/theater/session/" not in script


def test_theater_page_uses_free_idempotency_and_does_not_submit_choice_contract():
    """自由页面保留幂等和 revision，提交协议不再发送 Choice 字段。"""  # noqa: DOCSTRING_CJK
    _html, script = _sources()
    assert "client_start_id" in script
    assert "client_turn_id" in script
    assert "pendingExit" in script
    assert "getPendingExitId" in script
    assert "base_revision: state.stateRevision" in script
    assert "input_kind: 'free_input'" in script
    assert "choice_id" not in script
    assert "modeApiFor" not in script
    assert "neko.theater.free.activeSession.v1" in script
    assert "REQUEST_TIMEOUT_MS = 120000" in script


def test_theater_page_keeps_identity_card_and_stage_toggle():
    """自由页面仍展示来源背景、猫娘身份和可折叠舞台。"""  # noqa: DOCSTRING_CJK
    html, script = _sources()
    for element_id in (
        "theater-story-intro",
        "theater-story-intro-background",
        "theater-player-role-row",
        "theater-catgirl-role-row",
        "theater-story-goal-row",
        "theater-stage-toggle",
    ):
        assert f'id="{element_id}"' in html
    assert "function renderStoryIntro(story, roleCard)" in script
    assert "function initStageToggle()" in script


def test_theater_page_reuses_numeric_reader_layout():
    """自由页面复用剧本模式的阅读器布局，但保留自由模式自己的脚本入口。"""  # noqa: DOCSTRING_CJK
    html, _script = _sources()
    assert "/static/css/theater_numeric_v2.css" in html
    assert "numeric-theater-shell" in html
    assert "numeric-theater-stage" in html
    assert "numeric-theater-console" in html
    assert "numeric-theater-workspace" in html
    assert ">←</span>" in html


def test_theater_locales_remain_valid_json():
    """自由页面依赖的八份 locale 必须仍然可以解析。"""  # noqa: DOCSTRING_CJK
    for locale in ("en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"):
        payload = json.loads((ROOT / "static" / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        assert payload["theater"]["freeMode"]
        assert payload["theater"]["roleCardImport"]


def test_theater_popup_entry_opens_mode_selection():
    """三套头像菜单的小剧场入口都先进入统一模式选择页。"""  # noqa: DOCSTRING_CJK
    popup_config = (ROOT / "static" / "avatar" / "avatar-ui-popup-config.js").read_text(encoding="utf-8")
    assert popup_config.count("url: '/theater-home'") == 3
    assert "url: '/theater-numeric'" not in popup_config


def test_numeric_restart_allocates_a_new_session_id():
    script = (ROOT / "static" / "js" / "theater_numeric_v2.js").read_text(encoding="utf-8")
    assert "const sessionId = createId('numeric_web_session_');" in script
    assert "state.sessionId || remembered.sessionId" not in script

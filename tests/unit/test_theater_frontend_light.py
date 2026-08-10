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
    assert "base_revision: state.stateRevision" in script
    assert "input_kind: 'free_input'" in script
    assert "choice_id" not in script
    assert "modeApiFor" not in script


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


def test_theater_locales_remain_valid_json():
    """自由页面依赖的八份 locale 必须仍然可以解析。"""  # noqa: DOCSTRING_CJK
    for locale in ("en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"):
        payload = json.loads((ROOT / "static" / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        assert payload["theater"]["freeMode"]
        assert payload["theater"]["roleCardImport"]

"""验证小剧场迁入 N.E.K.O 胶囊后的静态前端合同。"""  # noqa: DOCSTRING_CJK

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_theater_page_is_numeric_story_selector_only():
    html = _source("templates/theater.html")
    script = _source("static/js/theater_selector.js")

    assert "data-theater-selector-app" in html
    assert "/static/css/theater.css" not in html
    assert "/static/css/theater_selector.css" in html
    assert "/static/js/theater_selector.js" in html
    for element_id in (
        "theater-story-list",
        "theater-detail-background",
        "theater-detail-player",
        "theater-detail-catgirl",
        "theater-start-btn",
        "theater-continue-btn",
        "theater-delete-btn",
    ):
        assert f'id="{element_id}"' in html

    assert "/api/theater/free/" not in script
    assert "theater-restart-btn" not in html
    assert "theater-restart-btn" not in script
    assert "RP-Hub" not in html
    assert "free_history" not in script


def test_selector_hidden_states_and_placeholder_stay_compact():
    css = _source("static/css/theater_selector.css")
    # 样式源码允许为可维护性换行；断言只锁定最终 CSS 语义。
    compact_css = "".join(css.split())

    assert ".theater-selector-shell[hidden]{display:none!important}" in compact_css
    assert ".theater-detail-placeholder{min-height:96px" in compact_css
    assert ".theater-detail-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in compact_css


def test_selector_uses_two_stage_handoff_and_start_replacement():
    script = _source("static/js/theater_selector.js")

    assert "theater:launch-request" in script
    assert "theater:launch-ready" in script
    assert "theater:selector-ready" in script
    assert "theater:post-end" in script
    assert "开始新的演绎？" in script
    assert "async function beginSession()" in script
    assert "replace_existing: replaceExisting === true" in script
    assert "persistent: true" in script


def test_capsule_runtime_separates_narration_and_dialogue_tts():
    runtime = _source("static/app/app-theater-runtime.js")
    buttons = _source("static/app/app-buttons.js")

    assert "/session/speak-block" in runtime
    assert "if (block.type === 'dialogue')" in runtime
    assert "typeBlock(historyId, block, token)" in runtime
    assert "playDialogue(group, item.block, item.blockIndex, revision, token)" in runtime
    assert "if (speechPromise && !await speechPromise) return" in runtime
    assert "theaterPresentation" in runtime
    assert "window.nekoTheaterRuntime" in runtime
    assert "var theaterRuntime = window.nekoTheaterRuntime" in buttons
    assert "theaterRuntime.isActive()" in buttons
    assert "handleComposerSubmit" in buttons


def test_capsule_runtime_projects_narration_display_kind_from_performance_phase():
    """动作或场景由 performance 位置确定，前端不能读取正文关键词猜测。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")
    assert "if (sceneNarration) blocks.push" in runtime
    assert "sceneNarration === LEGACY_EMPTY_TRANSITION_BRIDGE" in runtime
    schema = _source("frontend/react-neko-chat/src/message-schema.ts")
    app = _source("frontend/react-neko-chat/src/App.tsx")

    assert "function narrationDisplayKind(phase)" in runtime
    assert "phase === 'ordinary' || phase === 'source_response'" in runtime
    assert "performance.transition_delivered ? 'transition_bridge' : 'ordinary'" in runtime
    assert "performanceHistoryGroups(session.opening_performance, 'opening')" in runtime
    assert "displayKind: z.enum(['action', 'scene']).optional()" in schema
    assert "function formatTheaterBlockText(" in app
    assert "`（${normalized}）`" in app


def test_capsule_runtime_streams_ordinary_performance_into_one_history_bubble():
    """普通微动作与对白共用猫娘消息，场景旁白仍使用独立气泡。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")
    schema = _source("frontend/react-neko-chat/src/message-schema.ts")
    app = _source("frontend/react-neko-chat/src/App.tsx")

    assert "function performanceHistoryGroups(performance, fallbackPhase)" in runtime
    assert "function mixedPerformanceBlocks(value, phase)" in runtime
    assert "block.displayText = rawText" in runtime
    assert "group.preserveSpacing ? '' : '\\n'" in runtime
    assert "block.type === 'narration' && block.displayKind === 'scene'" in runtime
    assert "groups.push({ type: 'narration'" in runtime
    assert "TYPEWRITER_INTERVAL_MS" in runtime
    assert "group.type === 'narration' ? 'scene' : undefined" in runtime
    assert "'streaming'" in runtime
    assert "completedEntry.status = 'sent'" in runtime
    assert "status: z.enum(['streaming', 'sent']).optional()" in schema
    assert "const compactMessagePreview = theaterActive" in app
    assert "? null" in app
    assert "status: entry.status" in app


def test_capsule_runtime_shows_player_action_before_waiting_for_actor():
    """推荐输入和手动输入都应先进入历史，再等待模型返回。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")
    submit_start = runtime.index("async function submit(text)")
    optimistic_history = runtime.index(
        "state.history.push(historyEntry(optimisticHistoryId",
        submit_start,
    )
    actor_request = runtime.index("result = await requestJson(api.input", submit_start)

    assert optimistic_history < actor_request
    assert "state.history = state.history.filter(function (entry)" in runtime
    assert "historyEntry(optimisticHistoryId, 'player_action', message, state.playerName)" in runtime
    assert "group.type === 'dialogue' ? state.catgirlName : undefined" in runtime
    assert "participants.player_name" in runtime
    assert "participants.catgirl_name" in runtime


def test_capsule_runtime_confirms_end_without_clearing_on_cancel():
    runtime = _source("static/app/app-theater-runtime.js")
    geometry = _source("static/app/app-react-chat-window/geometry-and-messages.js")
    css = _source("static/css/index.css")
    compact_css = "".join(css.split())

    assert "typeof window.showConfirm === 'function'" in runtime
    assert "if (!confirmed || !state.active) return false" in runtime
    assert "cancelText: t('common.cancel', '取消')" in runtime
    assert "skin: 'theater'" in runtime
    assert "onResolve: function (confirmed)" in runtime
    assert "preparedSelector = openSelector()" in runtime
    assert "returnToSelector(receipt, 'user-ended', preparedSelector)" in runtime
    assert "restoreSelectorWindow(selectorTarget)" in runtime
    assert "state.errorMessage = t('theater.endFailed'" in runtime
    assert "return returnToSelector(state.pendingEnd, 'natural-ending-return')" in runtime
    assert "modal-dialog-theater" in geometry
    assert ".modal-overlay.modal-overlay-theater{background:transparent!important" in compact_css
    assert ".modal-dialog-theater.modal-btn{min-width:112px;min-height:44px" in compact_css


def test_theater_assets_are_scoped_to_selector_and_main_chat_hosts():
    selector = _source("templates/theater.html")
    index = _source("templates/index.html")
    chat = _source("templates/chat.html")

    assert "/static/js/theater_selector.js" in selector
    assert "/static/app/app-theater-runtime.js" not in selector
    assert "/static/app/app-theater-runtime.js" in index
    assert "/static/app/app-theater-runtime.js" in chat
    assert "/static/js/theater_selector.js" not in index
    assert "/static/js/theater_selector.js" not in chat


def test_theater_locales_remain_valid_and_aligned():
    locales = ("en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW")
    theater_keys = []
    for locale in locales:
        payload = json.loads(_source(f"static/locales/{locale}.json"))
        theater = payload["theater"]
        theater_keys.append(set(theater))
        for key in (
            "selectorTitle",
            "continueSession",
            "startAgainConfirmTitle",
            "startAgainConfirmBody",
            "rememberPerformanceTitle",
            "memorySaving",
            "performanceHistory",
        ):
            assert theater[key]
    assert all(keys == theater_keys[0] for keys in theater_keys[1:])


def test_theater_popup_entry_opens_story_selector():
    popup_config = _source("static/avatar/avatar-ui-popup-config.js")
    assert popup_config.count("url: '/theater'") == 3
    assert "url: '/theater-home'" not in popup_config
    assert "url: '/theater-numeric'" not in popup_config

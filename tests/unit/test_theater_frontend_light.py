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
        "theater-end-btn",
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
    assert ".theater-detail-actions{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in compact_css


def test_selector_uses_two_stage_handoff_and_start_replacement():
    script = _source("static/js/theater_selector.js")

    assert "theater:launch-request" in script
    assert "theater:launch-ready" in script
    assert "theater:selector-ready" in script
    assert "theater:post-end" in script
    assert "theater:external-end" in script
    assert "async function endSession()" in script
    assert "开始新的演绎？" in script
    assert "async function beginSession()" in script
    assert "replace_existing: replaceExisting === true" in script
    assert "persistent: true" in script


def test_selector_binds_delayed_confirmations_to_original_selection():
    """排队确认框只能操作弹出时对应的剧本和 Session。"""  # noqa: DOCSTRING_CJK

    script = _source("static/js/theater_selector.js")
    end_block = script[
        script.index("async function endSession()"):
        script.index("async function beginSession()")
    ]
    begin_block = script[
        script.index("async function beginSession()"):
        script.index("async function deleteStory()")
    ]
    forget_block = script[
        script.index("async function forgetStoryMemory()"):
        script.index("async function importStory(")
    ]

    assert "selectedSessionMatches(targetStoryId, targetSessionId)" in end_block
    assert "story_id: targetStoryId" in end_block
    assert "selectedSessionMatches(targetStoryId, targetSessionId)" in begin_block
    assert "sessionKind() === targetSessionKind" in begin_block
    assert "if (!confirmed || state.storyId !== targetStoryId) return;" in forget_block
    assert "body: { story_id: targetStoryId }" in forget_block


def test_capsule_runtime_separates_narration_and_dialogue_tts():
    runtime = _source("static/app/app-theater-runtime.js")
    buttons = _source("static/app/app-buttons.js")
    host_api = _source("static/app/app-react-chat-window/resize-drag-and-api.js")
    host_messages = _source(
        "static/app/app-react-chat-window/message-bundle-actions-and-prompts.js"
    )

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
    assert "setOnTheaterSubmit" in runtime
    assert "setOnTheaterSubmit" in host_api
    assert "I.handleTheaterSubmit" in host_messages
    theater_branch = buttons[buttons.index("if (theaterRuntime &&"):]
    assert theater_branch.index("if (hasExtraImages)") < theater_branch.index(
        "theaterRuntime.handleComposerSubmit(text)"
    )
    assert "if (hasScreenshots || hasExtraImages)" not in theater_branch[:600]


def test_capsule_runtime_restores_the_pre_theater_chat_surface():
    """小剧场退出时必须恢复进入前的聊天界面形态。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")

    assert "chatSurfaceModeRestore: null" in runtime
    assert "chatHost.getChatSurfaceMode()" in runtime
    assert "chatHost.setChatSurfaceMode(mode)" in runtime
    render = runtime.index("function render()")
    render_capture = runtime.index("captureChatSurfaceMode(chatHost);", render)
    force_compact = runtime.index("chatSurfaceMode: 'compact'", render)
    launch = runtime.index("async function performLaunch(message, launchToken)")
    capture = runtime.index("captureChatSurfaceMode(chatHost);", launch)
    launch_render = runtime.index("render();", capture)
    clear = runtime.index("function clear(reason)")
    restore = runtime.index("restoreChatSurfaceMode(chatHost);", clear)

    assert render < render_capture < force_compact < launch < capture < launch_render < clear < restore


def test_selector_does_not_publish_stale_story_after_archive_load():
    """异步归档返回后必须再次复验当前选择，再更新详情和地址栏。"""  # noqa: DOCSTRING_CJK

    script = _source("static/js/theater_selector.js")
    selection = script.index("async function selectStory(")
    archive_await = script.index("await loadMemoryArchives(storyId);", selection)
    recheck = script.index("if (state.storyId !== storyId) return;", archive_await)
    replace_url = script.index("window.history.replaceState", recheck)

    assert archive_await < recheck < replace_url


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
    assert "const compactMessagePreview = theaterActive\n    ? null" in app
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


def test_capsule_runtime_discards_turn_response_after_session_switch():
    """旧 Session 的迟到响应不能覆盖新剧本的胶囊状态。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")
    submit_start = runtime.index("async function submit(text)")
    request = runtime.index("result = await requestJson(api.input", submit_start)
    stale_guard = runtime.index("state.storyId !== submittedStoryId", request)
    apply_snapshot = runtime.index("applySnapshot(result);", stale_guard)

    assert "var submittedSessionId = state.sessionId" in runtime[submit_start:request]
    assert "state.pendingTurn.id !== submittedTurnId" in runtime[request:apply_snapshot]
    assert request < stale_guard < apply_snapshot


def test_capsule_runtime_stops_old_audio_before_loading_replacement_session():
    """替换 Session 的快照即使加载失败，也不能继续播放旧演绎语音。"""  # noqa: DOCSTRING_CJK

    runtime = _source("static/app/app-theater-runtime.js")
    launch_start = runtime.index("async function performLaunch(message, launchToken)")
    switch_guard = runtime.index(
        "if (state.active && (state.storyId !== nextStoryId || state.sessionId !== nextSessionId))",
        launch_start,
    )
    clear_audio = runtime.index("claimAudioPlayback();", switch_guard)
    loading = runtime.index(
        "state.active = true; state.phase = 'loading'",
        switch_guard,
    )

    assert switch_guard < clear_audio < loading


def test_capsule_runtime_confirms_end_without_clearing_on_cancel():
    runtime = _source("static/app/app-theater-runtime.js")
    geometry = _source("static/app/app-react-chat-window/geometry-and-messages.js")
    css = _source("static/css/index.css")
    compact_css = "".join(css.split())

    assert "typeof window.showConfirm === 'function'" in runtime
    assert "if (!confirmed || !isCurrentEndRequest()) return false" in runtime
    assert "if (!isCurrentEndRequest()) return false" in runtime
    assert "cancelText: t('common.cancel', '取消')" in runtime
    assert "skin: 'theater'" in runtime
    assert "onResolve: function (confirmed)" in runtime
    assert "preparedSelector = openSelector()" in runtime
    assert "returnToSelector(receipt, 'user-ended', preparedSelector)" in runtime
    assert "restoreSelectorWindow(selectorTarget)" in runtime
    assert "state.errorMessage = t('theater.endFailed'" in runtime
    assert "return returnToSelector(state.pendingEnd, 'natural-ending-return')" in runtime
    assert "message.action === 'theater:external-end'" in runtime
    assert "modal-dialog-theater" in geometry
    assert ".modal-overlay.modal-overlay-theater{background:transparent!important" in compact_css
    assert ".modal-dialog-theater.modal-btn{min-width:112px;min-height:44px" in compact_css


def test_theater_assets_are_scoped_to_selector_and_main_chat_hosts():
    selector = _source("templates/theater.html")
    index = _source("templates/index.html")
    chat = _source("templates/chat.html")
    transport_path = "/static/js/theater_transport.js"

    assert "/static/js/theater_selector.js" in selector
    assert "/static/app/app-theater-runtime.js" not in selector
    assert "/static/app/app-theater-runtime.js" in index
    assert "/static/app/app-theater-runtime.js" in chat
    assert "/static/js/theater_selector.js" not in index
    assert "/static/js/theater_selector.js" not in chat
    # 三个宿主都必须先加载共享协议，避免业务脚本因依赖缺失而在页面初始化阶段退出。
    assert selector.index(transport_path) < selector.index("/static/js/theater_selector.js")
    assert index.index(transport_path) < index.index("/static/app/app-theater-runtime.js")
    assert chat.index(transport_path) < chat.index("/static/app/app-theater-runtime.js")


def test_theater_transport_owns_shared_request_and_message_protocol():
    """协议号、CSRF 重试和请求 ID 只能在共享层实现一次。"""  # noqa: DOCSTRING_CJK

    transport = _source("static/js/theater_transport.js")
    selector = _source("static/js/theater_selector.js")
    runtime = _source("static/app/app-theater-runtime.js")

    assert "window.nekoTheaterTransport = Object.freeze" in transport
    assert "async function requestJson" in transport
    assert "async function mutationHeaders" in transport
    assert "function createMessage" in transport
    assert "TURN_TIMEOUT_MS = 60000" in transport
    assert "START_TIMEOUT_MS = 45000" in transport
    assert "path === '/api/theater-numeric/session/input'" in transport
    assert "path === '/api/theater-numeric/session/start'" in transport
    assert "function createId" not in selector
    assert "async function requestJson" not in selector
    assert "function createId" not in runtime
    assert "async function requestJson" not in runtime


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

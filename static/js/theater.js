(function () {
    'use strict';

    // /theater 只承载自由模式；Numeric v2 剧本使用独立页面和 API。
    const api = {
        stories: '/api/theater/stories',
        start: '/api/theater/free/session/start',
        input: '/api/theater/free/session/input',
        state: '/api/theater/free/session/state',
        active: '/api/theater/free/session/active',
    };
    const ACTIVE_SESSION_STORAGE_KEY = 'neko.theater.free.activeSession.v1';
    const FREE_ACTOR_UNAVAILABLE_REASON = 'free_actor_unavailable';
    const FREE_ROLE_CARD_INVALID_REASON = 'free_role_card_invalid';
    const TYPEWRITER_CHARACTER_DELAY_MS = 18;
    const REQUEST_TIMEOUT_MS = 120000;
    const state = {
        sessionId: '',
        storyId: '',
        stories: [],
        stateRevision: null,
        busy: false,
        inputClosed: false,
        // 当前暂时兼容 RP-Hub JSON；最终格式确定后只替换服务端适配层和这里的上传入口。
        freeRoleCard: null,
        freeRoleCardName: '',
        playback: { active: false, skipRequested: false, epoch: 0 },
        pendingTurn: null,
        pendingExit: null,
        pendingStart: null,
        generationLoadingRow: null,
    };

    function $(id) { return document.getElementById(id); }

    function t(key, fallback) {
        if (typeof window.t === 'function') {
            const value = window.t(key);
            if (value && value !== key) return value;
        }
        return fallback;
    }

    function setStatus(key, fallback) {
        const status = $('theater-status');
        if (!status) return;
        status.setAttribute('data-i18n', key);
        status.textContent = t(key, fallback);
    }

    async function getMutationHeaders() {
        const helper = window.nekoLocalMutationSecurity;
        if (!helper || typeof helper.getMutationHeaders !== 'function') return {};
        try { return await helper.getMutationHeaders(); } catch (_) { return {}; }
    }

    async function requestJson(url, options) {
        const requestOptions = options || {};
        const method = requestOptions.method || 'GET';
        const body = requestOptions.body || null;
        const serializedBody = requestOptions.body ? JSON.stringify(requestOptions.body) : undefined;
        const canRetry = method === 'GET' || Boolean(body && (body.client_turn_id || body.client_start_id));
        async function send() {
            const headers = { 'Content-Type': 'application/json' };
            if (method !== 'GET') Object.assign(headers, await getMutationHeaders());
            const controller = new AbortController();
            const timer = window.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
            try {
                return await fetch(url, {
                    method: method,
                    headers: headers,
                    body: serializedBody,
                    signal: controller.signal,
                });
            } finally {
                window.clearTimeout(timer);
            }
        }
        let response;
        try { response = await send(); } catch (error) {
            if (!canRetry) throw error;
            response = await send();
        }
        if (canRetry && [502, 503, 504].includes(response.status)) response = await send();
        if (response.status === 403 && method !== 'GET' && window.nekoLocalMutationSecurity &&
            typeof window.nekoLocalMutationSecurity.refreshToken === 'function') {
            await window.nekoLocalMutationSecurity.refreshToken();
            response = await send();
        }
        return response.json();
    }

    function rememberSession(sessionId) {
        try {
            if (sessionId) window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, sessionId);
        } catch (_) { /* 本地存储不可用时仍以服务端 active 为准。 */ }
    }

    function rememberedSession() {
        try { return String(window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY) || '').trim(); }
        catch (_) { return ''; }
    }

    function forgetSession() {
        try { window.localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY); }
        catch (_) { /* 清理失败不会改变服务端 Session 生命周期。 */ }
    }

    function initStageToggle() {
        const shell = document.querySelector('[data-theater-app]');
        const button = $('theater-stage-toggle');
        const label = $('theater-stage-toggle-label');
        if (!shell || !button || !label) return;
        function renderToggle(collapsed) {
            shell.dataset.stageCollapsed = collapsed ? 'true' : 'false';
            button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            const key = collapsed ? 'theater.expandStage' : 'theater.collapseStage';
            const text = t(key, collapsed ? '展开舞台' : '折叠舞台');
            label.textContent = text;
            label.setAttribute('data-i18n', key);
            button.title = text;
            button.setAttribute('data-i18n-title', key);
        }
        button.addEventListener('click', function () {
            renderToggle(shell.dataset.stageCollapsed !== 'true');
        });
        window.addEventListener('localechange', function () {
            renderToggle(shell.dataset.stageCollapsed === 'true');
        });
        renderToggle(false);
    }

    function renderRoleCardImport() {
        const button = $('theater-role-card-btn');
        const fileInput = $('theater-role-card-file');
        const name = $('theater-role-card-name');
        if (!button || !fileInput || !name) return;
        const available = !state.sessionId;
        button.hidden = !available;
        button.disabled = state.busy || state.playback.active || !available;
        fileInput.disabled = !available;
        name.hidden = !available || !state.freeRoleCardName;
        name.textContent = state.freeRoleCardName
            ? t('theater.roleCardSelected', '已选择角色卡：') + state.freeRoleCardName
            : '';
    }

    async function importFreeRoleCard(file) {
        if (!file) return;
        try {
            const parsed = JSON.parse(await file.text());
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('role_card_not_object');
            // 当前只把 RP-Hub JSON 原样交给服务端适配层；未来格式变化不进入自由 Session 核心。
            state.freeRoleCard = parsed;
            state.freeRoleCardName = String(file.name || 'role-card.json');
            renderRoleCardImport();
            setStatus('theater.ready', '角色卡已读取，可以开始自由演绎。');
        } catch (_) {
            state.freeRoleCard = null;
            state.freeRoleCardName = '';
            renderRoleCardImport();
            setStatus('theater.roleCardInvalid', '角色卡 JSON 无法读取，请选择有效的 RP-Hub 导出文件。');
        }
    }

    function initRoleCardImport() {
        const button = $('theater-role-card-btn');
        const fileInput = $('theater-role-card-file');
        if (!button || !fileInput) return;
        button.addEventListener('click', function () { if (!button.disabled) fileInput.click(); });
        fileInput.addEventListener('change', function () {
            void importFreeRoleCard(fileInput.files && fileInput.files[0]);
            fileInput.value = '';
        });
        renderRoleCardImport();
    }

    function setBusy(busy) {
        state.busy = busy;
        const active = Boolean(state.sessionId);
        const locked = busy || state.playback.active;
        const storyReady = Boolean(state.storyId && state.stories.some(function (story) {
            return story && String(story.id || '') === state.storyId;
        }));
        $('theater-story-select').disabled = locked || active;
        $('theater-start-btn').disabled = locked || active || !storyReady;
        $('theater-end-btn').disabled = locked || !active;
        $('theater-input').disabled = locked || !active || state.inputClosed;
        $('theater-send-btn').disabled = locked || !active || state.inputClosed;
        renderRoleCardImport();
    }

    function formatTurnText(role, text) {
        let normalized = String(text || '').trim();
        if (!normalized) return '';
        const pairs = { '“': '”', '"': '"', '「': '」', '『': '』' };
        const closing = pairs[normalized[0]];
        if (closing && normalized.endsWith(closing)) normalized = normalized.slice(1, -1).trim();
        if (!normalized) return '';
        // 自由模式与 Numeric v2 共用展示约定；服务端原文和 TTS 不被前端标记污染。
        if (role === 'user' || role === 'assistant' || role === 'dialogue') {
            return '「' + normalized + '」';
        }
        return normalized;
    }

    function appendTurnRow(role, parent) {
        const roleClass = { user: 'user', narrator: 'narration', assistant: 'dialogue', dialogue: 'dialogue' }[role] || 'narration';
        const row = document.createElement('span');
        row.className = 'theater-turn ' + roleClass;
        (parent || $('theater-log')).appendChild(row);
        return row;
    }

    function createNovelBlock() {
        const block = document.createElement('div');
        block.className = 'theater-turn-block novel';
        $('theater-log').appendChild(block);
        return block;
    }

    function appendTurn(role, text) {
        const displayText = formatTurnText(role, text);
        if (!displayText) return null;
        const block = document.createElement('div');
        block.className = role === 'user' ? 'theater-interaction-line user' : 'theater-turn-block novel';
        $('theater-log').appendChild(block);
        const row = appendTurnRow(role, block);
        row.textContent = displayText;
        scrollPerformanceLog();
        return block;
    }

    function scrollPerformanceLog() {
        const log = $('theater-log');
        if (log) log.scrollTop = log.scrollHeight;
    }

    function setPlaybackActive(active) {
        state.playback.active = active;
        const controls = $('theater-playback-controls');
        if (controls) controls.hidden = !active;
        const log = $('theater-log');
        if (log) log.setAttribute('aria-busy', active ? 'true' : 'false');
        setBusy(state.busy);
    }

    function cancelPlayback() {
        state.playback.epoch += 1;
        state.playback.skipRequested = false;
        setPlaybackActive(false);
    }

    function clearPerformanceLog() {
        cancelPlayback();
        $('theater-log').textContent = '';
        state.generationLoadingRow = null;
    }

    function waitForPlaybackTick() {
        return new Promise(function (resolve) { window.setTimeout(resolve, TYPEWRITER_CHARACTER_DELAY_MS); });
    }

    async function playCommittedText(text) {
        if (!text) return true;
        const epoch = state.playback.epoch + 1;
        state.playback.epoch = epoch;
        state.playback.skipRequested = false;
        setPlaybackActive(true);
        const block = createNovelBlock();
        const row = appendTurnRow('narrator', block);
        const displayText = formatTurnText('narrator', text);
        row.dataset.typing = 'true';
        try {
            const characters = Array.from(displayText);
            for (let index = 0; index < characters.length; index += 1) {
                if (epoch !== state.playback.epoch) return false;
                if (state.playback.skipRequested) { row.textContent = displayText; break; }
                row.textContent += characters[index];
                scrollPerformanceLog();
                await waitForPlaybackTick();
            }
            row.textContent = displayText;
            return true;
        } finally {
            if (epoch === state.playback.epoch) setPlaybackActive(false);
        }
    }

    function initPlaybackControls() {
        const button = $('theater-skip-playback-btn');
        if (button) button.addEventListener('click', function () { state.playback.skipRequested = true; });
    }

    function setGenerationLoading(active) {
        if (state.generationLoadingRow) {
            state.generationLoadingRow.remove();
            state.generationLoadingRow = null;
        }
        if (!active) return;
        const block = document.createElement('div');
        block.className = 'theater-turn-block novel theater-generation-block';
        const row = document.createElement('div');
        row.className = 'theater-turn narration theater-generation-loading';
        row.setAttribute('role', 'status');
        row.textContent = t('theater.generating', '片刻之后');
        block.appendChild(row);
        $('theater-log').appendChild(block);
        state.generationLoadingRow = block;
        scrollPerformanceLog();
    }

    function renderStoryOptions(stories) {
        const select = $('theater-story-select');
        select.textContent = '';
        stories.forEach(function (story) {
            const option = document.createElement('option');
            option.value = String(story.id || '');
            option.textContent = String(story.title || story.id || '');
            select.appendChild(option);
        });
        select.value = state.storyId;
    }

    function renderRole(rowId, valueId, value) {
        const normalized = String(value || '').trim();
        $(valueId).textContent = normalized;
        $(rowId).hidden = !normalized;
    }

    function renderStoryIntro(story, roleCard) {
        const intro = $('theater-story-intro');
        if (!story) {
            renderRole('theater-player-role-row', 'theater-player-role', '');
            renderRole('theater-catgirl-role-row', 'theater-catgirl-role', '');
            renderRole('theater-story-goal-row', 'theater-story-goal', '');
            intro.hidden = true;
            return;
        }
        const card = story.scenario_card || {};
        const temporary = roleCard && typeof roleCard === 'object' ? roleCard : null;
        $('theater-story-intro-title').textContent = String(
            (temporary && (temporary.story_title || temporary.scenario_title)) || story.title || ''
        );
        $('theater-story-intro-background').textContent = String(
            (temporary && temporary.scenario) || story.background || ''
        );
        renderRole('theater-player-role-row', 'theater-player-role', temporary ? (temporary.player_role || temporary.player_address) : card.player_role);
        renderRole('theater-catgirl-role-row', 'theater-catgirl-role', temporary ? [temporary.name, temporary.description].filter(Boolean).join(' — ') : card.catgirl_role);
        renderRole('theater-story-goal-row', 'theater-story-goal', temporary ? '' : card.primary_goal);
        intro.hidden = false;
    }

    function previewSelectedStory() {
        const story = state.stories.find(function (item) { return String(item.id || '') === state.storyId; });
        clearPerformanceLog();
        const scene = story && story.initial_scene;
        if (scene && scene.text) appendTurn('narrator', scene.text);
        renderStoryIntro(story, null);
    }

    function renderFreeHistory(history) {
        clearPerformanceLog();
        (Array.isArray(history) ? history : []).forEach(function (turn) {
            if (turn && turn.role) appendTurn(turn.role, turn.text);
        });
        scrollPerformanceLog();
    }

    function renderEnding(ending) {
        const panel = $('theater-ending-panel');
        if (!ending || !ending.should_end_session) { panel.hidden = true; return; }
        $('theater-ending-text').textContent = ending.reason === 'user_exit'
            ? t('theater.userExitEnded', '你已离开小剧场，本次离场不算剧情结局。')
            : t('theater.endingEnded', '故事已经落幕。');
        panel.hidden = false;
    }

    async function applyPayload(payload, options) {
        const restoring = Boolean(options && options.restoring);
        setGenerationLoading(false);
        state.sessionId = String(payload.session_id || state.sessionId || '');
        state.storyId = String(payload.story_id || state.storyId || '');
        state.stateRevision = Number.isInteger(payload.state_revision) ? payload.state_revision : null;
        state.inputClosed = !payload.can_resume;
        if (state.storyId) $('theater-story-select').value = state.storyId;
        renderStoryIntro(state.stories.find(function (story) { return String(story.id || '') === state.storyId; }), payload.free_role_card);
        if (restoring && Array.isArray(payload.free_history)) {
            renderFreeHistory(payload.free_history);
        } else if (payload.free_text && !(payload.ending && payload.ending.reason === 'user_exit')) {
            await playCommittedText(String(payload.free_text).trim());
        }
        renderEnding(payload.ending);
        if (payload.can_resume) {
            rememberSession(state.sessionId);
            setStatus('theater.running', '进行中');
        } else {
            forgetSession();
            setStatus('theater.ended', '已结束');
            state.sessionId = '';
            state.stateRevision = null;
            state.inputClosed = false;
        }
        setBusy(state.busy);
    }

    function createId(prefix) {
        const value = window.crypto && typeof window.crypto.randomUUID === 'function'
            ? window.crypto.randomUUID()
            : Math.random().toString(36).slice(2) + Date.now().toString(36);
        return prefix + value;
    }

    function getPendingTurnId(signature) {
        if (state.pendingTurn && state.pendingTurn.sessionId === state.sessionId && state.pendingTurn.signature === signature) return state.pendingTurn.id;
        const id = createId('turn_web_');
        state.pendingTurn = { id: id, sessionId: state.sessionId, signature: signature };
        return id;
    }

    function clearPendingTurn(id) {
        if (state.pendingTurn && state.pendingTurn.id === id) state.pendingTurn = null;
    }

    function getPendingExitId() {
        if (state.pendingExit && state.pendingExit.sessionId === state.sessionId) return state.pendingExit.id;
        const id = createId('exit_web_');
        state.pendingExit = { id: id, sessionId: state.sessionId };
        return id;
    }

    function clearPendingExit(id) {
        if (state.pendingExit && state.pendingExit.id === id) state.pendingExit = null;
    }

    function getPendingStartId() {
        const signature = JSON.stringify({ storyId: state.storyId, roleCard: state.freeRoleCard });
        if (state.pendingStart && state.pendingStart.signature === signature) return state.pendingStart.id;
        const id = createId('start_web_');
        state.pendingStart = { id: id, signature: signature };
        return id;
    }

    function clearPendingStart(id) {
        if (state.pendingStart && state.pendingStart.id === id) state.pendingStart = null;
    }

    async function restoreActiveSession(preferredSessionId) {
        const preferred = String(preferredSessionId || rememberedSession() || '').trim();
        let result = preferred ? await requestJson(api.state + '?session_id=' + encodeURIComponent(preferred)) : null;
        if (!result || !result.ok || !result.can_resume) {
            if (preferred) forgetSession();
            result = await requestJson(api.active);
        }
        if (result && result.ok && result.can_resume) {
            await applyPayload(result, { restoring: true });
            return true;
        }
        if (preferred) forgetSession();
        return false;
    }

    async function recoverUnavailableSession(result) {
        const reasons = new Set([
            'stale_session',
            'session_character_mismatch',
            'session_ended',
            'session_not_found',
            'session_invalid',
            'session_story_revision_mismatch',
        ]);
        if (!result || !reasons.has(String(result.reason || ''))) return false;
        forgetSession();
        state.sessionId = '';
        state.stateRevision = null;
        state.inputClosed = false;
        clearPerformanceLog();
        previewSelectedStory();
        setStatus('theater.ready', '准备中');
        return true;
    }

    async function recoverRevisionConflict(result, pendingText) {
        if (!result || result.reason !== 'state_revision_conflict' || !result.retryable) return false;
        await restoreActiveSession(state.sessionId);
        if (pendingText) $('theater-input').value = pendingText;
        setStatus('theater.sessionUpdated', '场景已更新，请重新发送。');
        return true;
    }

    async function loadStories() {
        try {
            const result = await requestJson(api.stories);
            if (!result || !result.ok || !Array.isArray(result.stories)) throw new Error('stories');
            state.stories = result.stories;
            state.storyId = result.stories.length ? String(result.stories[0].id || '') : '';
            renderStoryOptions(state.stories);
            if (!await restoreActiveSession('')) {
                previewSelectedStory();
                setStatus(state.stories.length ? 'theater.ready' : 'theater.noStories', state.stories.length ? '准备中' : '暂无可用背景');
            }
            setBusy(false);
        } catch (_) {
            setStatus('theater.failed', '加载失败');
        }
    }

    function showUnavailable() {
        setStatus('theater.actorUnavailable', '猫娘演绎暂时不可用，请重试。');
    }

    async function startSession() {
        if (state.busy || state.playback.active) return;
        clearPerformanceLog();
        setGenerationLoading(true);
        setBusy(true);
        const clientStartId = getPendingStartId();
        try {
            const body = { story_id: state.storyId, client_start_id: clientStartId };
            if (state.freeRoleCard) body.role_card = state.freeRoleCard;
            const result = await requestJson(api.start, { method: 'POST', body: body });
            if (!result || !result.ok) {
                if (result && result.reason === FREE_ROLE_CARD_INVALID_REASON) {
                    state.freeRoleCard = null;
                    state.freeRoleCardName = '';
                    clearPendingStart(clientStartId);
                    renderRoleCardImport();
                    setStatus('theater.roleCardInvalid', '角色卡未通过校验，请选择有效的 RP-Hub 导出文件。');
                    return;
                }
                if (result && result.reason === FREE_ACTOR_UNAVAILABLE_REASON) { showUnavailable(); return; }
                throw new Error('start');
            }
            state.freeRoleCard = null;
            state.freeRoleCardName = '';
            state.inputClosed = false;
            await applyPayload(result, { opening: true });
            clearPendingStart(clientStartId);
        } catch (_) {
            previewSelectedStory();
            setStatus('theater.failed', '启动失败');
        } finally {
            setGenerationLoading(false);
            setBusy(false);
        }
    }

    async function submitInput() {
        if (state.busy || state.playback.active || !state.sessionId || state.inputClosed) return;
        const input = $('theater-input');
        const message = input.value.trim();
        if (!message) return;
        input.value = '';
        const signature = JSON.stringify({ message: message });
        const clientTurnId = getPendingTurnId(signature);
        const optimistic = appendTurn('user', message);
        setGenerationLoading(true);
        setBusy(true);
        try {
            const result = await requestJson(api.input, {
                method: 'POST',
                body: {
                    session_id: state.sessionId,
                    client_turn_id: clientTurnId,
                    base_revision: state.stateRevision,
                    input_kind: 'free_input',
                    message: message,
                },
            });
            if (!result || !result.ok) {
                if (result && (result.reason === FREE_ACTOR_UNAVAILABLE_REASON || result.reason === FREE_ROLE_CARD_INVALID_REASON)) {
                    if (optimistic) optimistic.remove();
                    input.value = message;
                    showUnavailable();
                    return;
                }
                if (await recoverRevisionConflict(result, message)) { if (optimistic) optimistic.remove(); return; }
                if (await recoverUnavailableSession(result)) { if (optimistic) optimistic.remove(); input.value = message; return; }
                throw new Error('input');
            }
            await applyPayload(result);
            clearPendingTurn(clientTurnId);
        } catch (_) {
            if (optimistic) optimistic.remove();
            input.value = message;
            setStatus('theater.failed', '提交失败');
        } finally {
            setGenerationLoading(false);
            setBusy(false);
        }
    }

    async function endSession() {
        if (state.busy || state.playback.active || !state.sessionId) return;
        const optimistic = appendTurn('user', t('theater.leaveAction', '离开小剧场'));
        const clientTurnId = getPendingExitId();
        setBusy(true);
        try {
            const result = await requestJson(api.input, {
                method: 'POST',
                body: {
                    session_id: state.sessionId,
                    input_kind: 'user_exit',
                    client_turn_id: clientTurnId,
                    base_revision: state.stateRevision,
                },
            });
            if (!result || !result.ok) {
                if (await recoverUnavailableSession(result)) {
                    if (optimistic) optimistic.remove();
                    return;
                }
                throw new Error('exit');
            }
            await applyPayload(result);
            clearPendingExit(clientTurnId);
        } catch (_) {
            if (optimistic) optimistic.remove();
            setStatus('theater.failed', '离场失败');
        } finally {
            setBusy(false);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        initStageToggle();
        initPlaybackControls();
        initRoleCardImport();
        $('theater-story-select').addEventListener('change', function () {
            state.storyId = this.value;
            previewSelectedStory();
            setBusy(false);
        });
        $('theater-start-btn').addEventListener('click', startSession);
        $('theater-end-btn').addEventListener('click', endSession);
        $('theater-input-form').addEventListener('submit', function (event) {
            event.preventDefault();
            void submitInput();
        });
        setBusy(false);
        void loadStories();
    });
})();

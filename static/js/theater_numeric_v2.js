(function () {
    'use strict';

    // Numeric v2 使用独立 API、独立本地 Session 指针和独立 DOM，不读取旧 theater.js 状态。
    const api = {
        stories: '/api/theater-numeric/stories',
        importStory: '/api/theater-numeric/packages/import',
        packages: '/api/theater-numeric/packages',
        start: '/api/theater-numeric/session/start',
        input: '/api/theater-numeric/session/input',
        end: '/api/theater-numeric/session/end',
        active: '/api/theater-numeric/session/active',
        session: '/api/theater-numeric/session',
    };
    const SESSION_STORAGE_KEY = 'neko.theater.numeric.v2.session.v2';
    const STORY_STORAGE_KEY = 'neko.theater.numeric.v2.story.v2';
    const state = {
        storyId: '',
        sessionId: '',
        revision: null,
        stories: [],
        scene: null,
        busy: false,
        inputClosed: false,
        pendingTurn: null,
        replaceExisting: false,
    };

    function $(id) {
        return document.getElementById(id);
    }

    function t(key, fallback, options) {
        if (typeof window.t === 'function') {
            const value = window.t(key, options);
            if (value && value !== key) return value;
        }
        return fallback;
    }

    function setStatus(key, fallback) {
        const node = $('numeric-theater-status');
        if (!node) return;
        node.textContent = t(key, fallback);
        node.setAttribute('data-i18n', key);
    }

    function createId(prefix) {
        const random = window.crypto && typeof window.crypto.randomUUID === 'function'
            ? window.crypto.randomUUID()
            : Math.random().toString(36).slice(2) + Date.now().toString(36);
        return prefix + random;
    }

    function getPendingTurnId(message) {
        const signature = JSON.stringify({ sessionId: state.sessionId, message: message });
        if (state.pendingTurn && state.pendingTurn.signature === signature) return state.pendingTurn.id;
        const id = createId('numeric_web_turn_');
        state.pendingTurn = { id: id, signature: signature };
        return id;
    }

    function clearPendingTurn(id) {
        if (state.pendingTurn && state.pendingTurn.id === id) state.pendingTurn = null;
    }

    function rememberSession() {
        try {
            if (state.sessionId) {
                window.localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
                window.localStorage.setItem(STORY_STORAGE_KEY, state.storyId);
            }
        } catch (_) {
            // 禁用本地存储时仍可在当前页面继续使用服务端 Session。
        }
    }

    function rememberedSession() {
        try {
            return {
                sessionId: String(window.localStorage.getItem(SESSION_STORAGE_KEY) || '').trim(),
                storyId: String(window.localStorage.getItem(STORY_STORAGE_KEY) || '').trim(),
            };
        } catch (_) {
            return { sessionId: '', storyId: '' };
        }
    }

    function forgetSession() {
        try {
            window.localStorage.removeItem(SESSION_STORAGE_KEY);
            window.localStorage.removeItem(STORY_STORAGE_KEY);
        } catch (_) {
            // 本地指针清理失败不会改变服务端 Session 的正式状态。
        }
    }

    async function getMutationHeaders() {
        const helper = window.nekoLocalMutationSecurity;
        if (!helper || typeof helper.getMutationHeaders !== 'function') return {};
        try {
            return await helper.getMutationHeaders();
        } catch (_) {
            return {};
        }
    }

    async function requestJson(url, options) {
        const requestOptions = options || {};
        const method = requestOptions.method || 'GET';
        const body = requestOptions.body || null;
        const serializedBody = body ? JSON.stringify(body) : undefined;

        async function send() {
            const headers = { 'Content-Type': 'application/json' };
            if (method !== 'GET') Object.assign(headers, await getMutationHeaders());
            return fetch(url, {
                method,
                headers,
                body: serializedBody,
            });
        }

        let response = await send();
        // Token 过期时只刷新安全头并重发同一个稳定请求体，不生成新的回合 ID。
        if (response.status === 403 && method !== 'GET') {
            const helper = window.nekoLocalMutationSecurity;
            if (helper && typeof helper.refreshToken === 'function') {
                await helper.refreshToken();
                response = await send();
            }
        }
        return response.json();
    }

    function setBusy(busy) {
        state.busy = busy;
        const hasSession = Boolean(state.sessionId);
        const storySelect = $('numeric-theater-story-select');
        const startButton = $('numeric-theater-start-btn');
        const restartButton = $('numeric-theater-restart-btn');
        const endButton = $('numeric-theater-end-btn');
        const deleteButton = $('numeric-theater-delete-btn');
        const importButton = $('numeric-theater-import-btn');
        const importInput = $('numeric-theater-import-input');
        const input = $('numeric-theater-input');
        const sendButton = $('numeric-theater-send-btn');
        if (storySelect) storySelect.disabled = busy || (hasSession && !state.inputClosed);
        if (startButton) startButton.disabled = busy || hasSession || !state.storyId;
        if (restartButton) {
            // 重新开始只在终局显示，并用新 Session ID 原子替换当前角色的旧槽位。
            restartButton.hidden = !state.inputClosed;
            restartButton.disabled = busy || !state.inputClosed;
        }
        // 结束按钮只结束当前 Numeric Session，不删除剧本包和历史快照。
        if (endButton) endButton.disabled = busy || !hasSession || state.inputClosed;
        if (deleteButton) deleteButton.disabled = busy || !state.storyId;
        if (importButton) importButton.disabled = busy;
        if (importInput) importInput.disabled = busy;
        if (input) input.disabled = busy || !hasSession || state.inputClosed;
        if (sendButton) sendButton.disabled = busy || !hasSession || state.inputClosed;
        document.querySelectorAll('#numeric-theater-choices button').forEach(function (button) {
            button.disabled = busy || !hasSession || state.inputClosed;
        });
    }

    function renderStories() {
        const select = $('numeric-theater-story-select');
        if (!select) return;
        select.textContent = '';
        state.stories.forEach(function (story) {
            const option = document.createElement('option');
            option.value = String(story.story_id || '');
            option.textContent = String(story.title || story.story_id || '');
            select.appendChild(option);
        });
        if (state.storyId) select.value = state.storyId;
        const selected = state.stories.find(function (story) {
            return String(story.story_id || '') === state.storyId;
        });
        renderStoryIntro(selected && selected.intro);
    }

    function setImportFeedback(key, fallback) {
        const node = $('numeric-theater-import-feedback');
        if (!node) return;
        node.hidden = false;
        node.textContent = t(key, fallback);
        node.setAttribute('data-i18n', key);
    }

    async function fetchStories() {
        const result = await requestJson(api.stories);
        if (!result || !result.ok || !Array.isArray(result.stories)) {
            throw new Error('numeric_stories_invalid');
        }
        return result.stories;
    }

    function renderScene(scene, suggestedInputs) {
        state.scene = scene || null;
        // v2 不显示节点标题或场景卡；侧栏只展示 Actor 生成的非正式输入建议。
        renderSuggestions(suggestedInputs);
        renderEnding(scene && scene.terminal ? scene.ending : null);
    }

    function renderEnding(ending) {
        const panel = $('numeric-theater-ending-panel');
        const text = $('numeric-theater-ending-text');
        if (!panel || !text) return;
        if (!ending || typeof ending !== 'object') {
            text.textContent = '';
            panel.hidden = true;
            return;
        }
        const title = String(ending.title || '').trim();
        const summary = String(ending.summary || '').trim();
        text.textContent = [title, summary].filter(Boolean).join('：');
        panel.hidden = !text.textContent;
    }

    function renderIntroRole(rowId, valueId, value) {
        const row = $(rowId);
        const target = $(valueId);
        const text = String(value || '').trim();
        if (target) target.textContent = text;
        if (row) row.hidden = !text;
    }

    function renderStoryIntro(storyIntro) {
        const panel = $('numeric-theater-intro');
        if (!panel) return;
        const intro = storyIntro && typeof storyIntro === 'object' ? storyIntro : null;
        const background = intro ? String(intro.background || '').trim() : '';
        const playerIdentity = intro ? String(intro.player_identity || '').trim() : '';
        const catgirlIdentity = intro ? String(intro.catgirl_identity || '').trim() : '';
        if (!background && !playerIdentity && !catgirlIdentity) {
            panel.hidden = true;
            return;
        }
        const story = state.stories.find(function (item) {
            return String(item.story_id || '') === state.storyId;
        });
        $('numeric-theater-intro-title').textContent = String(
            (story && (story.title || story.story_id)) || state.storyId || ''
        );
        $('numeric-theater-intro-background').textContent = background;
        renderIntroRole('numeric-theater-player-role-row', 'numeric-theater-player-role', playerIdentity);
        renderIntroRole('numeric-theater-catgirl-role-row', 'numeric-theater-catgirl-role', catgirlIdentity);
        panel.hidden = false;
    }

    function renderSuggestions(suggestions) {
        const container = $('numeric-theater-choices');
        const panel = $('numeric-theater-choice-panel');
        const workspace = document.querySelector('.numeric-theater-workspace');
        if (!container) return;
        const visibleSuggestions = state.inputClosed
            ? []
            : (Array.isArray(suggestions) ? suggestions : []);
        container.textContent = '';
        // 推荐输入不属于作者路线合同；没有建议时直接隐藏侧栏。
        if (panel) panel.hidden = visibleSuggestions.length === 0;
        if (workspace) workspace.dataset.hasChoices = visibleSuggestions.length ? 'true' : 'false';
        visibleSuggestions.forEach(function (suggestion) {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = String(suggestion || '');
            button.addEventListener('click', function () {
                submitTurn(button.textContent);
            });
            container.appendChild(button);
        });
        setBusy(state.busy);
    }

    function formatTurnText(role, text) {
        let normalized = String(text || '').trim();
        if (!normalized) return '';
        const pairs = { '“': '”', '"': '"', '「': '」', '『': '』' };
        const closing = pairs[normalized[0]];
        if (closing && normalized.endsWith(closing)) normalized = normalized.slice(1, -1).trim();
        if (!normalized) return '';
        // 展示层统一补中文对白标记；服务端和 TTS 仍保留不带标记的原文。
        if (role === 'user' || role === 'dialogue') return '「' + normalized + '」';
        return normalized;
    }

    function appendLine(role, text, target) {
        const displayText = formatTurnText(role, text);
        if (!displayText) return;
        const row = document.createElement('div');
        row.className = 'theater-turn ' + role;
        row.textContent = displayText;
        (target || $('numeric-theater-log')).appendChild(row);
    }

    function appendNarration(text, target) {
        const value = String(text || '').trim();
        if (!value) return;
        const block = document.createElement('div');
        block.className = 'theater-turn-block novel';
        block.textContent = value;
        (target || $('numeric-theater-log')).appendChild(block);
    }

    function renderPerformance(performance, target, opening) {
        if (!performance || typeof performance !== 'object') return;
        // 每次模型回应使用独立容器，把同一回合的旁白与对白固定在一起，避免视觉上串到相邻回合。
        const response = document.createElement('article');
        response.className = 'numeric-theater-response' + (opening ? ' opening' : '');
        function appendContent(container, parent) {
            const content = Array.isArray(container && container.content) ? container.content : [];
            if (content.length) {
                content.forEach(function (block) {
                    if (block && block.type === 'narration') {
                        appendNarration(block.text, parent);
                    } else if (block && block.type === 'dialogue') {
                        appendLine('dialogue', String(block.text || ''), parent);
                    }
                });
                return;
            }
            // 修复前的记录没有 content，继续按旧字段恢复，不重写历史顺序。
            appendNarration(container && container.narration, parent);
            (Array.isArray(container && container.dialogue) ? container.dialogue : []).forEach(function (line) {
                appendLine('dialogue', String(line && line.text || ''), parent);
            });
        }
        const segments = Array.isArray(performance.segments) ? performance.segments : [];
        if (segments.length) {
            // 换场按来源回应、过渡桥、目标开场的正式顺序回放，刷新后不能退化成扁平文本。
            segments.forEach(function (segment) {
                const phase = document.createElement('div');
                phase.className = 'numeric-theater-transition-segment';
                phase.dataset.transitionPhase = String(segment && segment.phase || '');
                appendContent(segment, phase);
                if (phase.childNodes.length) response.appendChild(phase);
            });
        } else {
            appendContent(performance, response);
        }
        if (response.childNodes.length) (target || $('numeric-theater-log')).appendChild(response);
    }

    function renderHistory(openingPerformance, history) {
        const log = $('numeric-theater-log');
        if (!log) return;
        log.textContent = '';
        // 开场和每轮正文都来自已持久化的表现记录，刷新时按原顺序完整回放。
        renderPerformance(openingPerformance, log, true);
        (Array.isArray(history) ? history : []).forEach(function (record) {
            const exchange = document.createElement('section');
            exchange.className = 'numeric-theater-exchange';
            const playerInput = String(record && record.input_text || '').trim();
            if (playerInput) appendLine('user', playerInput, exchange);
            renderPerformance(record, exchange, false);
            if (exchange.childNodes.length) log.appendChild(exchange);
        });
        log.scrollTop = log.scrollHeight;
    }

    function applySnapshot(payload) {
        const session = payload && payload.session;
        if (!session) throw new Error('numeric_session_snapshot_missing');
        // 成功快照已经完成当前请求，必须释放 busy，才能让输入和推荐项恢复可用。
        state.busy = false;
        state.sessionId = String(session.session_id || '');
        state.storyId = String(session.story_package_id || state.storyId || '');
        state.revision = Number.isInteger(session.revision) ? session.revision : null;
        state.inputClosed = String(session.status || '') === 'ended'
            || Boolean(payload.scene && payload.scene.terminal);
        rememberSession();
        renderStoryIntro(payload.story_intro);
        renderScene(payload.scene || null, payload.suggested_inputs);
        renderHistory(session.opening_performance, session.performance_history);
        setStatus(
            state.inputClosed ? 'theater.ended' : 'theater.running',
            state.inputClosed ? '已结束' : '演出中'
        );
        setBusy(state.busy);
    }

    async function refreshCurrentSession() {
        // 版本冲突后只刷新服务端快照，不触碰输入框，确保玩家草稿可以基于新 revision 重试。
        if (!state.sessionId || !state.storyId) return false;
        try {
            const result = await requestJson(
                api.session + '/' + encodeURIComponent(state.sessionId)
                + '?story_id=' + encodeURIComponent(state.storyId)
            );
            if (!result || !result.ok) throw new Error('numeric_session_refresh_failed');
            applySnapshot(result);
            return true;
        } catch (_) {
            setStatus('theater.failed', '刷新演出状态失败，请手动刷新后重试。');
            setBusy(false);
            return false;
        }
    }

    async function restoreActiveSessionForStory() {
        if (state.busy || state.sessionId || !state.storyId) return false;
        setBusy(true);
        try {
            const result = await requestJson(
                api.active + '?story_id=' + encodeURIComponent(state.storyId)
            );
            if (result && result.ok) {
                applySnapshot(result);
                return true;
            }
            if (result && result.reason === 'catgirl_changed_requires_new_session') {
                state.replaceExisting = true;
            }
        } catch (_) {
            setStatus('theater.failed', '刷新演出状态失败，请手动刷新后重试。');
        }
        setBusy(false);
        return false;
    }

    function recoverChangedCatgirlSession(result) {
        if (!result || result.reason !== 'catgirl_changed_requires_new_session') return false;
        forgetSession();
        state.sessionId = '';
        state.revision = null;
        state.inputClosed = false;
        state.pendingTurn = null;
        state.replaceExisting = false;
        state.scene = null;
        const log = $('numeric-theater-log');
        if (log) log.textContent = '';
        renderScene(null);
        const selected = state.stories.find(function (story) {
            return String(story.story_id || '') === state.storyId;
        });
        renderStoryIntro(selected && selected.intro);
        setStatus('theater.ready', '准备中');
        return true;
    }

    async function loadStories() {
        try {
            state.stories = await fetchStories();
            const remembered = rememberedSession();
            const rememberedStory = state.stories.find(function (story) {
                return String(story.story_id || '') === remembered.storyId;
            });
            if (remembered.storyId && !rememberedStory) forgetSession();
            state.storyId = rememberedStory
                ? remembered.storyId
                : String(state.stories[0]?.story_id || '');
            renderStories();
            if (remembered.sessionId && rememberedStory && state.storyId) {
                const restored = await requestJson(
                    api.session + '/' + encodeURIComponent(remembered.sessionId)
                    + '?story_id=' + encodeURIComponent(state.storyId)
                );
                if (restored && restored.ok) {
                    applySnapshot(restored);
                    return;
                }
                forgetSession();
            }
            if (await restoreActiveSessionForStory()) return;
            setStatus('theater.ready', '准备中');
            setBusy(false);
        } catch (_) {
            setStatus('theater.failed', '加载失败');
            setBusy(false);
        }
    }

    async function importStoryFile(file) {
        if (!file || state.busy) return;
        const input = $('numeric-theater-import-input');
        setBusy(true);
        setImportFeedback('theater.importingStory', '正在导入剧本...');
        try {
            // 导入只接受完整 JSON 包；服务端仍会再次编译和校验，前端不承担合同判定。
            const payload = JSON.parse(await file.text());
            if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
                throw new Error('numeric_story_contract_invalid');
            }
            const result = await requestJson(api.importStory, {
                method: 'POST',
                body: payload,
            });
            if (!result || !result.ok) {
                if (result && result.reason === 'numeric_story_exists') {
                    setImportFeedback('theater.importStoryConflict', '这个剧本已经存在，未覆盖原文件。');
                } else if (
                    result
                    && (result.reason === 'numeric_story_contract_invalid'
                        || result.reason === 'numeric_v2_contract_invalid')
                ) {
                    setImportFeedback('theater.importStoryInvalid', '剧本格式未通过 Numeric v2 校验。');
                } else {
                    setImportFeedback('theater.importStoryFailed', '剧本导入失败，请重试。');
                }
                return;
            }
            const importedStoryId = String(result.package?.story_id || '');
            try {
                state.stories = await fetchStories();
                // 有进行中的 Session 时只刷新列表，不切换当前演出，避免导入污染状态链路。
                if (!state.sessionId && importedStoryId) {
                    state.storyId = importedStoryId;
                    try {
                        window.localStorage.setItem(STORY_STORAGE_KEY, state.storyId);
                    } catch (_) {
                        // 列表刷新成功时，本地指针写入失败不应阻断当前页面继续使用。
                    }
                }
                renderStories();
                setImportFeedback('theater.importStorySuccess', '剧本导入成功。');
            } catch (_) {
                setImportFeedback(
                    'theater.importStoryRefreshFailed',
                    '剧本已导入，但列表刷新失败，请重新加载页面。'
                );
            }
        } catch (_) {
            setImportFeedback('theater.importStoryInvalid', '无法读取有效的剧本 JSON。');
        } finally {
            if (input) input.value = '';
            setBusy(false);
        }
    }

    async function startSession(options) {
        const replaceExisting = Boolean(
            (options && options.replaceExisting) || state.replaceExisting
        );
        if (state.busy || (!replaceExisting && state.sessionId) || !state.storyId) return;
        setBusy(true);
        try {
            // 新 ID 让旧页面失效；服务端会原子替换同一“剧本 × 角色”槽位。
            const sessionId = createId('numeric_web_session_');
            const result = await requestJson(api.start, {
                method: 'POST',
                body: {
                    story_id: state.storyId,
                    session_id: sessionId,
                    replace_existing: replaceExisting,
                },
            });
            if (!result || !result.ok) throw new Error(result && result.reason || 'numeric_start_failed');
            state.replaceExisting = false;
            applySnapshot(result);
        } catch (_) {
            setStatus('theater.failed', '启动失败');
            setBusy(false);
        }
    }

    async function restartSession() {
        if (state.busy || !state.inputClosed) return;
        setStatus('theater.ready', '准备重新开始');
        // 重新开始创建新的进行中 Session，并替换当前角色的已结束记录。
        await startSession({ replaceExisting: true });
    }

    async function deleteStory() {
        if (state.busy || !state.storyId) return;
        const storyId = state.storyId;
        setBusy(true);
        try {
            const preview = await requestJson(
                api.packages + '/' + encodeURIComponent(storyId) + '/delete-preview'
            );
            if (!preview || !preview.ok) throw new Error('numeric_story_delete_preview_failed');
            const activeNames = Array.isArray(preview.active_catgirl_names)
                ? preview.active_catgirl_names.filter(Boolean)
                : [];
            const message = activeNames.length
                ? t(
                    'theater.deleteStoryActiveConfirm',
                    '跟' + activeNames.join('、') + '的演绎还未结束，是否确认删除？',
                    { names: activeNames.join('、') }
                )
                : t('theater.deleteStoryConfirm', '是否确认删除？');
            if (!window.confirm(message)) {
                setBusy(false);
                return;
            }
            const result = await requestJson(
                api.packages + '/' + encodeURIComponent(storyId),
                { method: 'DELETE' }
            );
            if (!result || !result.ok) throw new Error('numeric_story_delete_failed');
            forgetSession();
            state.sessionId = '';
            state.revision = null;
            state.inputClosed = false;
            state.pendingTurn = null;
            state.replaceExisting = false;
            state.scene = null;
            const log = $('numeric-theater-log');
            if (log) log.textContent = '';
            renderScene(null);
            state.stories = await fetchStories();
            state.storyId = String(state.stories[0]?.story_id || '');
            renderStories();
            setStatus('theater.storyDeleted', '剧本已删除');
            setBusy(false);
        } catch (_) {
            setStatus('theater.storyDeleteFailed', '删除剧本失败，请重试。');
            setBusy(false);
        }
    }

    async function endSession() {
        if (state.busy || !state.sessionId || state.inputClosed) return;
        setBusy(true);
        try {
            const result = await requestJson(api.end, {
                method: 'POST',
                body: {
                    story_id: state.storyId,
                    session_id: state.sessionId,
                    base_revision: state.revision,
                },
            });
            if (!result || !result.ok) {
                if (recoverChangedCatgirlSession(result)) {
                    setBusy(false);
                    await restoreActiveSessionForStory();
                    return;
                }
                throw new Error(result && result.reason || 'numeric_end_failed');
            }
            applySnapshot(result);
            // 结束后的唯一记录仍保留在服务端，重启 N.E.K.O 后仍能恢复到终局。
            state.sessionId = '';
            state.revision = null;
            state.inputClosed = true;
            // 结束后普通“开始”和“重新开始”语义一致：都必须创建新 Session，
            // 不能让后端恢复刚结束的只读快照。
            state.replaceExisting = true;
            const log = $('numeric-theater-log');
            if (log) log.textContent = '';
            setStatus('theater.ended', '已结束');
            setBusy(false);
        } catch (_) {
            setStatus('theater.failed', '结束当前剧本失败，请重试。');
            setBusy(false);
        }
    }

    async function submitTurn(suggestedMessage) {
        if (state.busy || !state.sessionId || state.inputClosed) return;
        const input = $('numeric-theater-input');
        const message = String(suggestedMessage || input.value || '').trim();
        if (!message) return;
        input.value = '';
        const clientTurnId = getPendingTurnId(message);
        const body = {
            story_id: state.storyId,
            session_id: state.sessionId,
            client_turn_id: clientTurnId,
            base_revision: state.revision,
            message: message,
        };
        setBusy(true);
        try {
            const result = await requestJson(api.input, { method: 'POST', body });
            if (!result || !result.ok) {
                if (recoverChangedCatgirlSession(result)) {
                    input.value = message;
                    setBusy(false);
                    await restoreActiveSessionForStory();
                    return;
                } else if (result && result.reason === 'numeric_base_revision_mismatch') {
                    // 先恢复原始输入，再同步最新快照；玩家不需要手动复制刚才的内容。
                    input.value = message;
                    const refreshed = await refreshCurrentSession();
                    if (refreshed) {
                        setStatus('theater.numericSessionUpdated', '演出状态已更新，已保留你的输入，请确认后重试。');
                    }
                    setBusy(false);
                    return;
                } else if (
                    result
                    && (result.reason === 'numeric_v2_evaluator_unavailable'
                        || result.reason === 'numeric_v2_evaluator_failed')
                ) {
                    // 数值判定失败时不提交半回合，并允许玩家原文直接重试。
                    setStatus(
                        'theater.inputInterpreterUnavailable',
                        '输入理解暂时不可用，本轮没有推进，请重试。'
                    );
                } else if (result && String(result.reason || '').includes('actor')) {
                    setStatus('theater.actorUnavailable', '猫娘演绎暂时不可用，请重试。');
                } else {
                    setStatus('theater.failed', '提交失败');
                }
                input.value = message;
                setBusy(false);
                return;
            }
            applySnapshot(result);
            clearPendingTurn(clientTurnId);
        } catch (_) {
            input.value = message;
            setStatus('theater.failed', '提交失败');
            setBusy(false);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        const shell = document.querySelector('[data-numeric-theater-app]');
        const stageToggle = $('numeric-theater-stage-toggle');
        const stageToggleLabel = $('numeric-theater-stage-toggle-label');
        function renderStageToggle(collapsed) {
            if (!shell || !stageToggle || !stageToggleLabel) return;
            shell.dataset.stageCollapsed = collapsed ? 'true' : 'false';
            stageToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            const key = collapsed ? 'theater.expandStage' : 'theater.collapseStage';
            const text = t(key, collapsed ? '展开舞台' : '折叠舞台');
            stageToggleLabel.textContent = text;
            stageToggleLabel.setAttribute('data-i18n', key);
            stageToggle.title = text;
            stageToggle.setAttribute('data-i18n-title', key);
        }
        // Numeric v2 的折叠只作用于当前剧本页，不写入自由模式的页面状态。
        if (stageToggle) stageToggle.addEventListener('click', function () {
            renderStageToggle(!shell || shell.dataset.stageCollapsed !== 'true');
        });
        window.addEventListener('localechange', function () {
            renderStageToggle(Boolean(shell && shell.dataset.stageCollapsed === 'true'));
        });
        renderStageToggle(false);
        $('numeric-theater-story-select').addEventListener('change', function () {
            if (state.busy || (state.sessionId && !state.inputClosed)) {
                this.value = state.storyId;
                return;
            }
            forgetSession();
            state.storyId = this.value;
            state.sessionId = '';
            state.revision = null;
            state.inputClosed = false;
            state.scene = null;
            state.replaceExisting = false;
            const log = $('numeric-theater-log');
            if (log) log.textContent = '';
            renderScene(null);
            const selected = state.stories.find(function (story) {
                return String(story.story_id || '') === state.storyId;
            });
            renderStoryIntro(selected && selected.intro);
            try {
                window.localStorage.setItem(STORY_STORAGE_KEY, state.storyId);
            } catch (_) {
                // 故事选择只影响当前页面，无法写入本地存储时不阻断使用。
            }
            restoreActiveSessionForStory();
        });
        $('numeric-theater-start-btn').addEventListener('click', startSession);
        $('numeric-theater-end-btn').addEventListener('click', endSession);
        $('numeric-theater-restart-btn').addEventListener('click', restartSession);
        $('numeric-theater-delete-btn').addEventListener('click', deleteStory);
        $('numeric-theater-import-btn').addEventListener('click', function () {
            $('numeric-theater-import-input').click();
        });
        $('numeric-theater-import-input').addEventListener('change', function () {
            importStoryFile(this.files && this.files[0]);
        });
        $('numeric-theater-input-form').addEventListener('submit', function (event) {
            event.preventDefault();
            submitTurn();
        });
        setBusy(false);
        loadStories();
    });
})();

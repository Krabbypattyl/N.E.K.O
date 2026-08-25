/** Numeric v2 小剧场在 N.E.K.O 本体中的页面级编排器。 */
(function () {
    'use strict';

    var api = {
        session: '/api/theater-numeric/session',
        input: '/api/theater-numeric/session/input',
        end: '/api/theater-numeric/session/end',
        speakBlock: '/api/theater-numeric/session/speak-block'
    };
    var POINTER_KEY = 'neko.theater.numeric.v2.capsule-pointer.v1';
    // 本体运行时只消费共享传输协议；胶囊状态、回放和跨窗口目标仍由本模块负责。
    var transport = window.nekoTheaterTransport;
    if (!transport) throw new Error('numeric_theater_transport_unavailable');
    var MESSAGE_SCHEMA = transport.MESSAGE_SCHEMA;
    var createId = transport.createId;
    var requestJson = transport.requestJson;
    // 旧 Session 可能已保存过去的空桥段占位句；只在转场桥段中精确隐藏，不改写正式演绎记录。
    var LEGACY_EMPTY_TRANSITION_BRIDGE = '时间向前流转，现场随之转换。';
    var state = {
        active: false, phase: 'inactive', storyId: '', storyTitle: '', sessionId: '', revision: 0,
        playerName: '', catgirlName: '',
        sessionStatus: '', scene: null, history: [], currentBlock: null, suggestedInputs: [],
        queueToken: 0, pendingTurn: null, pendingEnd: null, channel: null, hostReadyTimer: 0,
        draftRestore: null, ordinaryDraftRestore: null, presentationSeq: 0, composerVisibilityRestore: null,
        errorMessage: ''
    };
    var launchRequests = Object.create(null);
    var launchRequestOrder = [];
    var launchReplyTargets = Object.create(null);
    var endConfirmationPending = false;

    function t(key, fallback) {
        if (typeof window.t === 'function') {
            var value = window.t(key);
            if (value && value !== key) return value;
        }
        return fallback;
    }
    function postMessage(message) {
        var payload = transport.createMessage('theater-runtime', message);
        if (state.channel) { try { state.channel.postMessage(payload); } catch (_) {} }
        return payload;
    }
    function postDirect(target, message) {
        if (!target || typeof target.postMessage !== 'function') return;
        try { target.postMessage(message, window.location.origin); } catch (_) {}
    }
    function rememberPointer() {
        try {
            if (!state.active) { window.localStorage.removeItem(POINTER_KEY); return; }
            window.localStorage.setItem(POINTER_KEY, JSON.stringify({ story_id: state.storyId, session_id: state.sessionId }));
        } catch (_) {}
    }
    function readPointer() {
        try {
            var value = JSON.parse(window.localStorage.getItem(POINTER_KEY) || 'null');
            return value && value.story_id && value.session_id ? value : null;
        } catch (_) { return null; }
    }
    function host() { return window.reactChatWindowHost || null; }
    function captureOrdinaryDraft(chatHost) {
        if (state.active) return;
        state.ordinaryDraftRestore = null;
        var snapshot = chatHost && typeof chatHost.getState === 'function' ? chatHost.getState() : {};
        // 猫娘本地聊天有自己的草稿状态，不能误当作普通聊天草稿保存。
        if (snapshot.viewProps && snapshot.viewProps.catLocalTextOnly) return;
        var input = document.querySelector('#react-chat-window-root .composer-input');
        if (!input || typeof input.value !== 'string') return;
        // 主页面初始化模型时可能重挂 React；退出小剧场时用该快照恢复普通聊天草稿。
        state.ordinaryDraftRestore = { id: createId('theater_ordinary_draft_'), text: input.value };
    }
    function claimComposerVisibility(chatHost) {
        if (!state.active || state.composerVisibilityRestore || !chatHost) return;
        var snapshot = typeof chatHost.getState === 'function' ? chatHost.getState() : {};
        state.composerVisibilityRestore = {
            composerHidden: !!snapshot.composerHiddenRequested,
            goodbyeComposerHidden: !!snapshot.goodbyeComposerHidden
        };
        if (typeof chatHost.setComposerHidden === 'function') chatHost.setComposerHidden(false);
        if (typeof chatHost.setGoodbyeComposerHidden === 'function') chatHost.setGoodbyeComposerHidden(false);
    }
    function restoreComposerVisibility(chatHost) {
        var snapshot = state.composerVisibilityRestore;
        state.composerVisibilityRestore = null;
        if (!snapshot || !chatHost) return;
        if (typeof chatHost.setComposerHidden === 'function') chatHost.setComposerHidden(snapshot.composerHidden);
        if (typeof chatHost.setGoodbyeComposerHidden === 'function') chatHost.setGoodbyeComposerHidden(snapshot.goodbyeComposerHidden);
    }
    function claimAudioPlayback() {
        var audio = window.appAudioPlayback;
        if (audio && typeof audio.clearAudioQueueWithoutDecoderReset === 'function') {
            audio.clearAudioQueueWithoutDecoderReset();
        }
    }
    var TYPEWRITER_INTERVAL_MS = 32;
    function historyEntry(id, type, text, author, displayKind, status) {
        return {
            id: id,
            type: type,
            text: String(text || '').trim(),
            author: author || undefined,
            displayKind: displayKind || undefined,
            status: status || undefined
        };
    }
    function narrationDisplayKind(phase) {
        // 普通互动和来源回应使用括号微动作；开场与换场桥保留独立场景旁白。
        return phase === 'ordinary' || phase === 'source_response' ? 'action' : 'scene';
    }
    function presentationBlock(type, text, phase) {
        var block = { type: type, text: text };
        if (type === 'narration') block.displayKind = narrationDisplayKind(phase);
        return block;
    }
    function mixedPerformanceBlocks(value, phase) {
        // 新合同只让模型输出一个混合字符串；这里按括号确定性拆分，供逐字展示和 TTS 复用。
        var source = String(value || '').trim();
        if (!source) return [];
        var pairs = { '（': '）', '(': ')' };
        var closers = { '）': true, ')': true };
        var blocks = [];
        var segmentStart = 0;
        var actionStart = -1;
        var expectedClose = '';
        function append(type, rawText, text) {
            if (!String(text || '').trim()) return;
            var block = presentationBlock(type, String(text).trim(), phase);
            // displayText 保留模型原始穿插形式；动作始终属于猫娘气泡，不继承 opening 的场景样式。
            block.displayText = rawText;
            block.preserveSpacing = true;
            if (type === 'narration') block.displayKind = 'action';
            blocks.push(block);
        }
        for (var index = 0; index < source.length; index += 1) {
            var char = source[index];
            if (expectedClose) {
                if (Object.prototype.hasOwnProperty.call(pairs, char)) return [];
                if (closers[char]) {
                    if (char !== expectedClose) return [];
                    append('narration', source.slice(actionStart, index + 1), source.slice(segmentStart, index));
                    expectedClose = '';
                    segmentStart = index + 1;
                }
                continue;
            }
            if (Object.prototype.hasOwnProperty.call(pairs, char)) {
                append('dialogue', source.slice(segmentStart, index), source.slice(segmentStart, index));
                actionStart = index;
                segmentStart = index + 1;
                expectedClose = pairs[char];
                continue;
            }
            if (closers[char]) return [];
        }
        if (expectedClose) return [];
        append('dialogue', source.slice(segmentStart), source.slice(segmentStart));
        return blocks;
    }
    function formatPresentationBlock(block) {
        if (block && Object.prototype.hasOwnProperty.call(block, 'displayText')) return String(block.displayText || '');
        var text = String(block && block.text || '').trim();
        if (!text || block.type !== 'narration' || block.displayKind !== 'action') return text;
        var wrapped = (text.startsWith('（') && text.endsWith('）'))
            || (text.startsWith('(') && text.endsWith(')'));
        return wrapped ? text : '（' + text + '）';
    }
    function contentBlocks(performance, fallbackPhase) {
        if (!performance || typeof performance !== 'object') return [];
        var containers = Array.isArray(performance.segments) ? performance.segments : [performance];
        var blocks = [];
        containers.forEach(function (container) {
            var phase = String(container && container.phase || fallbackPhase || '').trim();
            // 旧的换场记录没有 segments 时，宁可保留独立旁白，也不能把整段换场包装成微动作。
            if (!phase) phase = performance.transition_delivered ? 'transition_bridge' : 'ordinary';
            if (container && (Object.prototype.hasOwnProperty.call(container, 'scene_narration')
                || Object.prototype.hasOwnProperty.call(container, 'performance'))) {
                var sceneNarration = String(container.scene_narration || '').trim();
                if (phase === 'transition_bridge' && sceneNarration === LEGACY_EMPTY_TRANSITION_BRIDGE) {
                    sceneNarration = '';
                }
                if (sceneNarration) blocks.push(presentationBlock('narration', sceneNarration, 'scene'));
                mixedPerformanceBlocks(container.performance, phase).forEach(function (block) { blocks.push(block); });
                return;
            }
            var raw = Array.isArray(container && container.content) ? container.content : null;
            if (raw) {
                raw.forEach(function (block) {
                    var type = block && block.type;
                    var text = String(block && block.text || '').trim();
                    if (text && (type === 'narration' || (type === 'dialogue' && block.speaker_id === 'active_catgirl'))) {
                        blocks.push(presentationBlock(type, text, phase));
                    }
                });
                return;
            }
            var narration = String(container && container.narration || '').trim();
            if (narration) blocks.push(presentationBlock('narration', narration, phase));
            (Array.isArray(container && container.dialogue) ? container.dialogue : []).forEach(function (line) {
                var text = String(line && line.text || '').trim();
                if (text && line.speaker_id === 'active_catgirl') blocks.push(presentationBlock('dialogue', text, phase));
            });
        });
        return blocks;
    }
    function performanceHistoryGroups(performance, fallbackPhase) {
        var groups = [];
        contentBlocks(performance, fallbackPhase).forEach(function (block, blockIndex) {
            // 开场和换场场景旁白沿用独立旁白气泡；场景内微动作才与对白合并。
            if (block.type === 'narration' && block.displayKind === 'scene') {
                groups.push({ type: 'narration', blocks: [{ block: block, blockIndex: blockIndex }] });
                return;
            }
            var current = groups[groups.length - 1];
            if (!current || current.type !== 'dialogue') {
                current = { type: 'dialogue', blocks: [] };
                groups.push(current);
            }
            current.blocks.push({ block: block, blockIndex: blockIndex });
            if (block.preserveSpacing) current.preserveSpacing = true;
        });
        return groups;
    }
    function historyGroupText(group) {
        return group.blocks.map(function (item) { return formatPresentationBlock(item.block); })
            .filter(Boolean)
            .join(group.preserveSpacing ? '' : '\n');
    }
    function buildCommittedHistory(snapshot) {
        var session = snapshot.session || {};
        var result = [];
        performanceHistoryGroups(session.opening_performance, 'opening').forEach(function (group, groupIndex) {
            var openingText = historyGroupText(group);
            if (!openingText) return;
            result.push(historyEntry(
                'opening-performance-' + groupIndex,
                group.type,
                openingText,
                group.type === 'dialogue' ? state.catgirlName : undefined,
                group.type === 'narration' ? 'scene' : undefined
            ));
        });
        (Array.isArray(session.performance_history) ? session.performance_history : []).forEach(function (record, recordIndex) {
            var revision = Number(record.revision || recordIndex + 1);
            var input = String(record.input_text || '').trim();
            if (input) result.push(historyEntry('player-' + revision, 'player_action', input, state.playerName));
            performanceHistoryGroups(record, 'ordinary').forEach(function (group, groupIndex) {
                var performanceText = historyGroupText(group);
                if (!performanceText) return;
                result.push(historyEntry(
                    'performance-' + revision + '-' + groupIndex,
                    group.type,
                    performanceText,
                    group.type === 'dialogue' ? state.catgirlName : undefined,
                    group.type === 'narration' ? 'scene' : undefined
                ));
            });
        });
        if (snapshot.scene && snapshot.scene.terminal && snapshot.scene.ending) {
            var ending = snapshot.scene.ending;
            result.push(historyEntry('ending-' + session.session_id, 'ending', [ending.title, ending.summary].filter(Boolean).join('：')));
        }
        return result;
    }
    function presentation() {
        return {
            active: state.active,
            phase: state.phase,
            storyTitle: state.storyTitle,
            currentBlock: state.currentBlock,
            history: state.history.slice(),
            suggestedInputs: state.phase === 'awaiting_player' ? state.suggestedInputs.slice(0, 3) : [],
            busy: ['loading', 'evaluating', 'ending', 'returning_selector'].indexOf(state.phase) >= 0,
            sessionEnded: state.sessionStatus === 'ended',
            errorMessage: state.errorMessage,
            draftRestore: state.draftRestore,
            ordinaryDraftRestore: state.ordinaryDraftRestore,
            presentationSeq: ++state.presentationSeq
        };
    }
    function render() {
        var chatHost = host();
        if (!chatHost || typeof chatHost.setViewProps !== 'function') return false;
        claimComposerVisibility(chatHost);
        var compactState = state.active && state.phase === 'awaiting_player' ? 'input' : 'default';
        chatHost.setViewProps({
            theaterPresentation: presentation(),
            chatSurfaceMode: 'compact',
            compactChatState: compactState,
            composerDisabled: state.active && state.phase !== 'awaiting_player'
        });
        if (state.active && typeof chatHost.openWindow === 'function') chatHost.openWindow();
        return true;
    }
    function bindHostCallbacks() {
        var chatHost = host();
        if (!chatHost) return false;
        if (typeof chatHost.setOnTheaterSuggestedInputSelect === 'function') {
            // 推荐输入直接进入 Runtime 提交流程，不借用输入框草稿或普通 Galgame 回填链路。
            chatHost.setOnTheaterSuggestedInputSelect(function (text) { void submit(text); });
        }
        if (typeof chatHost.setOnTheaterEnd === 'function') chatHost.setOnTheaterEnd(function () { runtime.requestEnd(); });
        render();
        return true;
    }
    function waitForHost() {
        if (bindHostCallbacks()) return Promise.resolve(true);
        return new Promise(function (resolve) {
            var attempts = 80;
            window.clearInterval(state.hostReadyTimer);
            state.hostReadyTimer = window.setInterval(function () {
                attempts -= 1;
                if (bindHostCallbacks() || attempts <= 0) {
                    window.clearInterval(state.hostReadyTimer); state.hostReadyTimer = 0; resolve(attempts > 0);
                }
            }, 100);
        });
    }
    function applySnapshot(snapshot) {
        var session = snapshot.session || {};
        var participants = snapshot.participants || {};
        state.storyId = String(session.story_package_id || state.storyId);
        state.sessionId = String(session.session_id || state.sessionId);
        state.revision = Number(session.revision || 0);
        state.sessionStatus = String(session.status || 'active');
        // 玩家和猫娘署名都由服务端当前绑定提供，恢复旧记录时也不回退成通用占位名。
        state.playerName = String(participants.player_name || t('theater.player', 'Player'));
        state.catgirlName = String(participants.catgirl_name || 'Neko');
        state.scene = snapshot.scene || null;
        state.storyTitle = String(snapshot.story_title || state.storyTitle || state.storyId);
        state.suggestedInputs = Array.isArray(snapshot.suggested_inputs) ? snapshot.suggested_inputs.map(String) : [];
    }
    function readingDelay(text) { return Math.min(5000, Math.max(1100, Array.from(String(text || '')).length * 55)); }
    function wait(ms, token) {
        return new Promise(function (resolve) {
            window.setTimeout(function () { resolve(token === state.queueToken); }, ms);
        });
    }
    function waitForSpeech(speechId, timeoutMs, token) {
        return new Promise(function (resolve) {
            var done = false;
            function finish() {
                if (done) return; done = true;
                window.removeEventListener('neko-assistant-speech-end', onEnd);
                window.removeEventListener('neko-assistant-speech-unavailable', onEnd);
                window.removeEventListener('neko-assistant-speech-cancel', onEnd);
                resolve(token === state.queueToken);
            }
            function onEnd(event) {
                var turnId = event && event.detail && event.detail.turnId;
                if (!speechId || !turnId || String(turnId) === String(speechId)) finish();
            }
            window.addEventListener('neko-assistant-speech-end', onEnd);
            window.addEventListener('neko-assistant-speech-unavailable', onEnd);
            window.addEventListener('neko-assistant-speech-cancel', onEnd);
            window.setTimeout(finish, timeoutMs);
        });
    }
    async function typeBlock(historyId, block, token) {
        var entry = state.history.find(function (candidate) { return candidate.id === historyId; });
        if (!entry) return false;
        var text = formatPresentationBlock(block);
        var separator = entry.text && !block.preserveSpacing ? '\n' : '';
        var characters = Array.from(separator + text);
        for (var index = 0; index < characters.length; index += 1) {
            if (token !== state.queueToken) return false;
            entry.text += characters[index];
            render();
            if (!await wait(TYPEWRITER_INTERVAL_MS, token)) return false;
        }
        return token === state.queueToken;
    }
    async function playDialogue(group, block, blockIndex, revision, token) {
        var alive = true;
        if (block.type === 'dialogue') {
            var dialogueItems = group.blocks.filter(function (item) {
                return item.block.type === 'dialogue';
            });
            var dialogueBlockIndexes = dialogueItems.map(function (item) { return item.blockIndex; });
            var dialogueText = dialogueItems.map(function (item) { return item.block.text; }).join(' ');
            var result;
            try {
                result = await requestJson(api.speakBlock, { method: 'POST', body: {
                    story_id: state.storyId, session_id: state.sessionId, revision: revision, block_index: blockIndex,
                    dialogue_block_indexes: dialogueBlockIndexes,
                    playback_request_id: 'theater_speech_' + state.sessionId + '_' + revision + '_' + blockIndex
                }});
            } catch (_) {
                // TTS 是表现层旁路；请求失败时按阅读时长继续，不能中断正文播放或锁住输入。
                result = { ok: false };
            }
            if (result.ok && result.speech_id && (result.audio_queued || result.audio_sent)) alive = await waitForSpeech(result.speech_id, Math.max(5000, readingDelay(dialogueText) * 3), token);
            else alive = await wait(readingDelay(dialogueText), token);
        }
        return alive && token === state.queueToken;
    }
    async function playPerformance(performance, revision, options) {
        var token = ++state.queueToken;
        var groups = performanceHistoryGroups(performance, options && options.displayPhase || 'ordinary');
        var nextSuggestedInputs = state.suggestedInputs.slice();
        state.phase = 'performing'; state.currentBlock = null; state.suggestedInputs = []; render();
        if (options && options.playerInput && !options.playerAlreadyShown) {
            state.history.push(historyEntry('player-' + revision, 'player_action', options.playerInput, state.playerName));
        }
        var historyBaseId = options && options.historyId || 'performance-' + revision;
        for (var groupIndex = 0; groupIndex < groups.length; groupIndex += 1) {
            var group = groups[groupIndex];
            var historyId = historyBaseId + '-' + groupIndex;
            state.history.push(historyEntry(
                historyId,
                group.type,
                '',
                group.type === 'dialogue' ? state.catgirlName : undefined,
                group.type === 'narration' ? 'scene' : undefined,
                'streaming'
            ));
            render();
            var speechPromise = null;
            for (var itemIndex = 0; itemIndex < group.blocks.length; itemIndex += 1) {
                var item = group.blocks[itemIndex];
                // 同一演绎段只在首个对白块发起一次合并 TTS；动作与后续对白仍按原顺序逐字显示。
                if (!speechPromise && item.block.type === 'dialogue') {
                    speechPromise = playDialogue(group, item.block, item.blockIndex, revision, token);
                }
                if (!await typeBlock(historyId, item.block, token)) return;
            }
            if (speechPromise && !await speechPromise) return;
            var completedEntry = state.history.find(function (entry) { return entry.id === historyId; });
            if (completedEntry) completedEntry.status = 'sent';
            render();
        }
        if (state.sessionStatus === 'ended') {
            if (state.scene && state.scene.ending) state.history.push(historyEntry('ending-' + state.sessionId, 'ending', [state.scene.ending.title, state.scene.ending.summary].filter(Boolean).join('：')));
            state.phase = 'ended';
        } else {
            state.phase = 'awaiting_player';
            state.suggestedInputs = nextSuggestedInputs;
        }
        render();
    }
    async function performLaunch(message) {
        captureOrdinaryDraft(host());
        state.active = true; state.phase = 'loading'; state.storyId = String(message.story_id); state.sessionId = String(message.session_id); render();
        var snapshot = await requestJson(api.session + '/' + encodeURIComponent(state.sessionId) + '?story_id=' + encodeURIComponent(state.storyId));
        if (!snapshot.ok || !snapshot.session || Number(snapshot.session.revision) !== Number(message.revision)) {
            clear('launch-validation-failed');
            return false;
        }
        applySnapshot(snapshot);
        state.history = buildCommittedHistory(snapshot);
        state.active = true;
        rememberPointer();
        await waitForHost();
        claimAudioPlayback();
        var readyMessage = postMessage({ action: 'theater:launch-ready', launch_id: message.launch_id, story_id: state.storyId, session_id: state.sessionId });
        postDirect(launchReplyTargets[message.launch_id], readyMessage);
        delete launchReplyTargets[message.launch_id];
        if (message.launch_action === 'start' || message.launch_action === 'restart') {
            state.history = [];
            await playPerformance(snapshot.session.opening_performance, 0, {
                displayPhase: 'opening',
                historyId: 'opening-performance'
            });
        } else {
            state.phase = state.sessionStatus === 'ended' ? 'ended' : 'awaiting_player';
            state.currentBlock = null;
            render();
        }
        return true;
    }
    function launch(message) {
        var launchId = String(message.launch_id || '');
        if (launchRequests[launchId]) return launchRequests[launchId];
        var request = performLaunch(message).catch(function () {
            clear('launch-request-failed');
            return false;
        });
        launchRequests[launchId] = request;
        launchRequestOrder.push(launchId);
        if (launchRequestOrder.length > 64) delete launchRequests[launchRequestOrder.shift()];
        return request;
    }
    async function submit(text) {
        var message = String(text || '').trim();
        if (!state.active || state.phase !== 'awaiting_player' || !message) return false;
        var signature = state.sessionId + '\u001f' + state.revision + '\u001f' + message;
        if (!state.pendingTurn || state.pendingTurn.signature !== signature) state.pendingTurn = { signature: signature, id: createId('theater_turn_') };
        var optimisticHistoryId = 'player-pending-' + state.pendingTurn.id;
        // 玩家行动先进入历史区，让推荐输入和手动提交都立即得到可见反馈。
        if (!state.history.some(function (entry) { return entry.id === optimisticHistoryId; })) {
            state.history.push(historyEntry(optimisticHistoryId, 'player_action', message, state.playerName));
        }
        state.phase = 'evaluating'; state.suggestedInputs = []; state.draftRestore = null; state.errorMessage = ''; render();
        var result;
        try {
            result = await requestJson(api.input, { method: 'POST', body: {
                story_id: state.storyId, session_id: state.sessionId, client_turn_id: state.pendingTurn.id,
                base_revision: state.revision, message: message
            }});
        } catch (_) {
            result = { ok: false, reason: 'numeric_input_request_failed' };
        }
        if (!result.ok) {
            // 未提交的玩家行动不能伪装成已发生事实；失败时撤回气泡并恢复原输入。
            state.history = state.history.filter(function (entry) { return entry.id !== optimisticHistoryId; });
            state.phase = 'awaiting_player';
            state.draftRestore = { id: createId('theater_draft_restore_'), text: message };
            if (result.reason === 'numeric_base_revision_mismatch') {
                var refreshed = await requestJson(api.session + '/' + encodeURIComponent(state.sessionId) + '?story_id=' + encodeURIComponent(state.storyId));
                if (refreshed.ok) { applySnapshot(refreshed); state.history = buildCommittedHistory(refreshed); }
            }
            render();
            return false;
        }
        state.pendingTurn = null;
        applySnapshot(result);
        if (result.end_receipt_id) state.pendingEnd = {
            story_id: state.storyId,
            session_id: state.sessionId,
            revision: state.revision,
            end_receipt_id: result.end_receipt_id,
            archive_request_id: result.archive_request_id || ''
        };
        if (result.idempotent_replay === true) {
            // 上一次请求可能已在服务端提交但响应丢失；幂等重放只返回权威快照，
            // 不会再次返回 performance。必须用快照重建历史，不能留下乐观玩家气泡或漏掉猫娘回复。
            state.history = buildCommittedHistory(result);
            state.currentBlock = null;
            state.draftRestore = null;
            state.phase = state.sessionStatus === 'ended' ? 'ended' : 'awaiting_player';
            render();
            return true;
        }
        try {
            await playPerformance(result.performance, state.revision, {
                playerInput: message,
                playerAlreadyShown: true
            });
        } catch (_) {
            state.currentBlock = null;
            state.phase = state.sessionStatus === 'ended' ? 'ended' : 'awaiting_player';
            state.errorMessage = t('theater.performanceFailed', '演绎播放中断，请继续输入或重新打开小剧场。');
            render();
            return false;
        }
        return true;
    }
    function clear(reason) {
        if (state.active && state.phase !== 'loading') claimAudioPlayback();
        state.queueToken += 1;
        state.active = false; state.phase = 'inactive'; state.currentBlock = null; state.history = []; state.suggestedInputs = [];
        state.playerName = ''; state.catgirlName = '';
        state.pendingTurn = null; state.draftRestore = null;
        rememberPointer();
        var chatHost = host();
        if (chatHost && typeof chatHost.setViewProps === 'function') {
            chatHost.setViewProps({
                theaterPresentation: {
                    active: false,
                    phase: 'inactive',
                    history: [],
                    suggestedInputs: [],
                    ordinaryDraftRestore: state.ordinaryDraftRestore
                },
                composerDisabled: false
            });
        }
        restoreComposerVisibility(chatHost);
        window.dispatchEvent(new CustomEvent('neko:theater-cleared', { detail: { reason: reason || 'clear' } }));
    }
    function openSelector(receipt) {
        state.pendingEnd = receipt || state.pendingEnd;
        var url = '/theater?story_id=' + encodeURIComponent(state.storyId);
        try {
            if (typeof window.openOrFocusWindow === 'function') {
                return window.openOrFocusWindow(url, 'neko_theater', 'width=1100,height=760,menubar=no,toolbar=no,location=no,status=no', { navigateOnReuse: true });
            }
            return window.open(url, 'neko_theater');
        } catch (_) {
            return null;
        }
    }
    function restoreSelectorWindow(target) {
        if (!target || target.closed) return false;
        try {
            if (typeof window.requestOpenedWindowRestore === 'function') {
                window.requestOpenedWindowRestore(target);
            } else {
                postDirect(target, { type: 'neko:restore-window' });
            }
        } catch (_) {}
        try {
            if (typeof target.focus === 'function') target.focus();
        } catch (_) {}
        return true;
    }
    function returnToSelector(receipt, clearReason, preparedSelector) {
        state.pendingEnd = receipt || state.pendingEnd;
        state.sessionStatus = 'ended';
        state.phase = 'returning_selector';
        state.errorMessage = '';
        render();
        var selectorTarget = preparedSelector || openSelector(state.pendingEnd);
        if (!selectorTarget) {
            // 已退出 Session 只能从选剧页继续；本体保留只读历史和返回按钮作为恢复入口。
            state.phase = 'ended';
            state.errorMessage = t(
                'theater.selectorReturnFailed',
                '已退出演绎，但剧本页面打开失败。请点击“返回剧本页”重试。'
            );
            render();
            return false;
        }
        // 预先打开的选剧页可能早于结束接口返回完成加载，需要在拿到回执后再主动补发一次。
        sendPendingEnd(selectorTarget);
        // 确认弹窗关闭和结束请求都会把焦点留回本体；提交成功后必须再次恢复选剧页。
        restoreSelectorWindow(selectorTarget);
        clear(clearReason);
        return true;
    }
    function sendPendingEnd(target) {
        if (!state.pendingEnd) return;
        var message = postMessage(Object.assign({ action: 'theater:post-end', message_id: createId('theater_post_end_') }, state.pendingEnd));
        postDirect(target, message);
    }
    async function confirmEnd(onConfirmed) {
        var message = t('theater.endConfirm', '确定结束当前演绎吗？');
        if (typeof window.showConfirm === 'function') {
            return window.showConfirm(
                message,
                t('theater.endPerformance', '结束演绎'),
                {
                    okText: t('common.confirm', '确认'),
                    cancelText: t('common.cancel', '取消'),
                    danger: true,
                    skin: 'theater',
                    onResolve: function (confirmed) {
                        if (confirmed && typeof onConfirmed === 'function') onConfirmed();
                    }
                }
            );
        }
        // 极早启动阶段统一弹窗尚未加载时保留原生确认，不能静默结束演绎。
        return window.confirm(message);
    }
    async function requestEnd() {
        if (!state.active || endConfirmationPending) return false;
        if (state.sessionStatus === 'ended' || state.phase === 'ended') {
            return returnToSelector(state.pendingEnd, 'natural-ending-return');
        }
        endConfirmationPending = true;
        var confirmed = false;
        var preparedSelector = null;
        try {
            confirmed = await confirmEnd(function () {
                // 必须在确认按钮的原始点击事件里取得窗口句柄；等待结束接口后再打开会被桌面窗口策略拦截。
                preparedSelector = openSelector();
            });
        } finally {
            endConfirmationPending = false;
        }
        // 取消只关闭确认框，Session、输入和演绎历史都保持原样。
        if (!confirmed || !state.active) return false;
        state.phase = 'ending'; state.errorMessage = ''; state.queueToken += 1; render();
        var result;
        try {
            result = await requestJson(api.end, { method: 'POST', body: { story_id: state.storyId, session_id: state.sessionId, base_revision: state.revision } });
        } catch (_) {
            result = { ok: false };
        }
        if (!result.ok) {
            state.phase = 'awaiting_player';
            state.errorMessage = t('theater.endFailed', '结束演绎失败，请检查网络后重试。');
            render();
            return false;
        }
        var receipt = {
            story_id: state.storyId,
            session_id: state.sessionId,
            revision: result.session.revision,
            end_receipt_id: result.end_receipt_id,
            archive_request_id: result.archive_request_id || ''
        };
        state.revision = result.session.revision;
        return returnToSelector(receipt, 'user-ended', preparedSelector);
    }
    async function restorePointer() {
        var pointer = readPointer();
        if (!pointer) return;
        var snapshot;
        try {
            snapshot = await requestJson(api.session + '/' + encodeURIComponent(pointer.session_id) + '?story_id=' + encodeURIComponent(pointer.story_id));
        } catch (_) {
            // 暂时性网络失败保留指针供下次恢复，但不让启动 Promise 产生未处理拒绝。
            return;
        }
        if (!snapshot.ok || !snapshot.session) { try { window.localStorage.removeItem(POINTER_KEY); } catch (_) {} return; }
        applySnapshot(snapshot);
        if (snapshot.end_receipt_id) state.pendingEnd = {
            story_id: state.storyId,
            session_id: state.sessionId,
            revision: state.revision,
            end_receipt_id: snapshot.end_receipt_id,
            archive_request_id: snapshot.archive_request_id || ''
        };
        state.active = true; state.phase = state.sessionStatus === 'ended' ? 'ended' : 'awaiting_player'; state.history = buildCommittedHistory(snapshot); state.currentBlock = null;
        await waitForHost(); render();
    }
    function handleCrossWindowMessage(event) {
        if (event && event.origin && event.origin !== window.location.origin) return;
        var message = event && event.data;
        if (!message || typeof message !== 'object') return;
        if (String(message.action || '').indexOf('theater:') === 0 && message.schema !== MESSAGE_SCHEMA) return;
        if (message.action === 'theater:launch-request' && message.launch_id && message.story_id && message.session_id && Number.isInteger(message.revision)) {
            if (event.source && event.source !== window) launchReplyTargets[message.launch_id] = event.source;
            launch(message);
        }
        else if (message.action === 'theater:selector-ready') sendPendingEnd(event.source);
        else if (
            message.action === 'theater:external-end'
            && state.active
            && message.story_id === state.storyId
            && message.session_id === state.sessionId
        ) clear('selector-ended');
        else if (message.action === 'catgirl_switched' && state.active) clear('catgirl-switched');
    }

    var runtime = {
        isActive: function () { return state.active; },
        handleComposerSubmit: function (text) {
            if (!state.active) return false;
            submit(text).catch(function () {
                state.phase = 'awaiting_player';
                state.errorMessage = t('theater.inputFailed', '演绎提交失败，请重试。');
                render();
            });
            return true;
        },
        requestEnd: requestEnd,
        clear: clear,
        getState: function () { return Object.assign({}, state, { history: state.history.slice() }); }
    };
    window.nekoTheaterRuntime = runtime;

    if (typeof BroadcastChannel !== 'undefined') {
        try { state.channel = new BroadcastChannel('neko_page_channel'); state.channel.addEventListener('message', handleCrossWindowMessage); } catch (_) { state.channel = null; }
    }
    window.addEventListener('message', handleCrossWindowMessage);
    window.addEventListener('localechange', function () {
        if (!state.active) return;
        // 语言切换会让聊天宿主重建基础 props；等宿主处理完成后恢复仍在进行的剧场投影。
        window.setTimeout(function () {
            if (state.active) render();
        }, 0);
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restorePointer);
    else restorePointer();
})();

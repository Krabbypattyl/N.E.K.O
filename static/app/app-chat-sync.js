/* Opt-in chat-original controls. Restoring originals never submits chat or models. */
(function () {
    'use strict';
    var ID = /^[0-9a-f]{32}(?![\s\S])/;
    var liveIds = new Map();
    var userRequests = new Map();
    var assistantTurns = new Map();
    var completedTurns = new Set();
    var knownIds = new Map();
    var deletedLocalIds = new Map();
    var knownCursors = new Map();
    var historyPollRunning = false;
    var pendingHistoryHint = null;
    var historyPollTimer = null;
    var activeUid = null;
    var activeName = null;
    var historyRequestGeneration = 0;
    var dialog = null;
    var settingsCharacterName = null;
    var statusNode = null;
    var DEFAULT_SERVICE_URL = 'https://sync.nekotech.cn';
    var operationRunning = false;
    var writerRecoveryRequired = false;
    var foregroundWakeRunning = false;

    function text(key, values) {
        // All visible labels use the same eight-locale namespace.
        return window.t ? window.t('chatSync.' + key, values || {}) : key;
    }

    function describeError(error) {
        // Only fixed adapter codes reach diagnostics; arbitrary errors may contain credentials.
        var messages = {
            chat_sync_account_mismatch: 'errorAccountMismatch',
            chat_sync_writer_recovery_required: 'errorWriterRecovery',
            chat_sync_login_required: 'loginRequired',
            chat_sync_session_changed: 'errorSessionChanged',
            chat_sync_session_unavailable: 'errorSessionUnavailable',
            chat_sync_unavailable: 'errorNetwork',
            chat_sync_pull_timeout: 'errorNetwork',
            chat_sync_retry_later: 'errorRetryLater',
            chat_sync_service_rejected: 'errorServiceRejected',
            chat_sync_binding_changed: 'errorBindingChanged',
            chat_sync_not_enabled: 'profileNeedsBinding',
            chat_sync_character_not_found: 'errorBindingChanged',
            character_profile_changed: 'errorProfileChanged',
            character_profile_name_conflict: 'cloudNameConflict',
            character_profile_unsupported_value: 'profileUnsupported',
            character_profile_conflict: 'profileConflict',
            profile_revision_conflict: 'profileConflict'
        };
        var code = typeof error === 'string' ? error : error && error.message;
        if (!Object.prototype.hasOwnProperty.call(messages, code)) return text('errorUnknown');
        return text('errorDetail', { message: text(messages[code]), code: code });
    }

    function characterName() {
        // Resolve the current local profile, then use its persisted UID from the API.
        return settingsCharacterName || window.lanlan_config && window.lanlan_config.lanlan_name || window._currentCatgirl || '';
    }

    function traceHistoryMerge(records, uid, localOnly) {
        // A collector may install this opt-in observer; it never owns chat state.
        if (typeof window.__nekoChatSyncTrace !== 'function') return;
        records.forEach(function (record) {
            try {
                window.__nekoChatSyncTrace('ui_merge_requested', {
                    character_uid: uid, message_id: record.messageId,
                    local_message_id: (localOnly ? 'local-reply-' : 'sync-original-') + record.messageId
                });
            } catch (_) { /* Diagnostics must never prevent passive restoration. */ }
        });
    }

    async function api(path, method, body) {
        // Only the local backend handles OAuth; the browser never receives its bearer.
        var response = await fetch('/api/chat-sync' + path, {
            method: method || 'GET', cache: 'no-store',
            headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body)
        });
        var result;
        try { result = await response.json(); }
        catch (_) { throw new Error('chat_sync_unavailable'); }
        if (!response.ok) throw new Error(result && typeof result.detail === 'string' ? result.detail : 'chat_sync_unavailable');
        return result;
    }

    function originalMessage(record, name, localOnly) {
        // Unknown source times stay explicitly unknown instead of becoming import time.
        var numeric = typeof record.originalTime === 'string' && /^\d+$/.test(record.originalTime)
            ? Number(record.originalTime) : NaN;
        var timestamp = Number.isSafeInteger(numeric) ? numeric : 0;
        var date = new Date(timestamp);
        var time = record.originalTime === null ? text('unknownTime')
            : timestamp && !Number.isNaN(date.getTime()) ? date.toLocaleString() : record.originalTime;
        var message = {
            id: (localOnly ? 'local-reply-' : 'sync-original-') + record.messageId,
            role: record.role,
            author: record.role === 'assistant' ? name : text('userLabel'),
            time: time, createdAt: timestamp, status: 'sent',
            blocks: [{ type: 'text', text: record.text }]
        };
        return message;
    }

    function mergeHistory(current, originals, previousKnown, mappings, name, turnMappings, localReplies) {
        // Identity is the only dedupe key; equal text/time never joins distinct messages.
        localReplies = localReplies || [];
        var received = new Set(originals.concat(localReplies).map(function (record) { return record.messageId; }));
        var live = new Map();
        current.forEach(function (message) {
            if (message.id.indexOf('sync-original-') === 0 || message.id.indexOf('local-reply-') === 0) return;
            var identity = mappings.get(message.id)
                || (turnMappings && message.role === 'assistant' && turnMappings.get(message.turnId));
            if (!identity) return;
            if (!live.has(identity)) live.set(identity, []);
            live.get(identity).push(message);
        });
        function restore(record, localOnly) {
            var canonical = originalMessage(record, name, localOnly);
            var displayed = live.get(record.messageId);
            if (!displayed) return [canonical];
            // Wire identity is independent of React keys. Keep the visible split
            // bubbles and blocks so passive sync cannot replay their entry animation.
            // Local-only/tool records stay local; this is presentation, not eligibility.
            return displayed.map(function (message) {
                // Retire cached transitional actions, preserving unrelated ones.
                var actions = (message.actions || []).filter(function (action) {
                    return action.action !== 'chat_sync_delete_original';
                });
                var next = Object.assign({}, message, { status: 'sent', actions: actions.length ? actions : undefined });
                if (typeof next.createdAt !== 'number' && canonical.createdAt) next.createdAt = canonical.createdAt;
                return next;
            });
        }
        var restored = originals.slice().sort(function (a, b) {
            var at = /^\d+$/.test(a.originalTime || '') ? Number(a.originalTime) : 0;
            var bt = /^\d+$/.test(b.originalTime || '') ? Number(b.originalTime) : 0;
            return at - bt || (a.originStreamId || '').localeCompare(b.originStreamId || '')
                || (a.originSeq || 0) - (b.originSeq || 0);
        }).flatMap(function (record) { return restore(record, false); });
        restored = restored.concat(localReplies.flatMap(function (record) { return restore(record, true); }));
        var retained = current.filter(function (message) {
            if (message.id.indexOf('sync-original-') === 0 || message.id.indexOf('local-reply-') === 0) return false;
            var originalId = mappings.get(message.id)
                || (turnMappings && message.role === 'assistant' && turnMappings.get(message.turnId));
            // Preserve live/uncommitted records until their exact source was stored.
            return !originalId || (!received.has(originalId) && !previousKnown.has(originalId));
        });
        // Local-only replies keep their actual place between restored originals.
        // Missing live timestamps remain at the end; equal times retain their order.
        return restored.concat(retained).sort(function (a, b) {
            var at = typeof a.createdAt === 'number' && Number.isFinite(a.createdAt) ? a.createdAt : Infinity;
            var bt = typeof b.createdAt === 'number' && Number.isFinite(b.createdAt) ? b.createdAt : Infinity;
            return at - bt;
        }).map(function (message, index) {
            return Object.assign({}, message, { sortKey: index });
        });
    }

    async function loadHistory(state, idleOnly) {
        // Latest-request wins also fences same-role pages overtaken by a delete.
        var generation = ++historyRequestGeneration;
        var name = characterName();
        if (!name) return;
        if (idleOnly && (document.visibilityState === 'hidden' || unfinishedChat())) return;
        state = state || await api('/status');
        var role = state.characters.find(function (item) { return item.displayName === name; });
        if (!role || characterName() !== name || generation !== historyRequestGeneration) return;
        activeUid = role.characterSyncId;
        activeName = name;
        var result = await api('/characters/' + activeUid + '/history');
        if (characterName() !== name || activeUid !== role.characterSyncId || generation !== historyRequestGeneration) return;
        // A live turn may start while local history I/O waits. Automatic refresh
        // defers without consuming its cursor so the next idle check can retry.
        if (idleOnly && (document.visibilityState === 'hidden' || unfinishedChat())) return;
        var host = window.reactChatWindowHost;
        if (!host || typeof host.getState !== 'function' || typeof host.setMessages !== 'function') return;
        var previous = new Set(knownIds.get(activeUid) || []);
        (deletedLocalIds.get(activeUid) || new Set()).forEach(function (id) { previous.add(id); });
        var localDeleted = deletedLocalIds.get(activeUid) || new Set();
        var localReplies = (result.localReplies || []).filter(function (record) { return !localDeleted.has(record.messageId); });
        if (!result.originals.length && !localReplies.length && !previous.size) {
            // An empty read is still a successful restoration of this exact cursor.
            if (role.binding) knownCursors.set(activeUid, role.binding.cursor);
            return true;
        }
        var merged = mergeHistory(host.getState().messages || [], result.originals, previous, liveIds, name, assistantTurns, localReplies);
        // setMessages is passive: appendMessage would enqueue message reactions.
        traceHistoryMerge(result.originals, activeUid);
        traceHistoryMerge(localReplies, activeUid, true);
        host.setMessages(merged);
        knownIds.set(activeUid, new Set(result.originals.map(function (record) { return record.messageId; })));
        if (role.binding) knownCursors.set(activeUid, role.binding.cursor);
        return true;
    }

    async function refreshDownloadedHistory() {
        // This local-only check does no cloud/auth work. Reuse passive restoration
        // only when a committed download cursor changes, retaining live bubbles.
        if (historyPollRunning || document.visibilityState === 'hidden' || unfinishedChat()) return;
        historyPollRunning = true;
        var hint = null;
        try {
            do {
                hint = pendingHistoryHint;
                pendingHistoryHint = null;
                var name = characterName();
                if (!name || (hint && hint.name !== name)) return;
                var state = await api('/status');
                if (name !== characterName()) return;
                var role = state.characters.find(function (item) { return item.displayName === name; });
                if (hint && (!role || hint.uid !== role.characterSyncId)) return;
                if (document.visibilityState === 'hidden' || unfinishedChat()) {
                    pendingHistoryHint = pendingHistoryHint || hint;
                    return;
                }
                if (role && role.binding && (!knownCursors.has(role.characterSyncId)
                        || knownCursors.get(role.characterSyncId) !== role.binding.cursor)) {
                    if (!await loadHistory(state, true)) {
                        // Do not consume a hint if a live turn began during I/O.
                        if (name === characterName()) pendingHistoryHint = pendingHistoryHint || hint;
                        return;
                    }
                }
                hint = null;
            } while (pendingHistoryHint && document.visibilityState !== 'hidden' && !unfinishedChat());
        } catch (error) {
            pendingHistoryHint = pendingHistoryHint || hint;
            throw error;
        } finally {
            historyPollRunning = false;
        }
    }

    function notifyHistoryCommitted(payload) {
        // This socket hint contains no progress or content. Read the local
        // authoritative cursor and history under the existing role/I/O fences.
        if (!payload || payload.type !== 'chat_sync_history_changed' || typeof payload.characterSyncId !== 'string'
                || !ID.test(payload.characterSyncId)
                || Object.keys(payload).length !== 2) return Promise.resolve();
        if (activeName === characterName() && activeUid !== payload.characterSyncId) return Promise.resolve();
        pendingHistoryHint = { uid: payload.characterSyncId, name: characterName() };
        return refreshDownloadedHistory();
    }

    function retryPendingHistory() {
        // Lifecycle events can fire before their producer clears pending state.
        // Retry after that synchronous cleanup; real rendering/streaming still wins.
        return Promise.resolve().then(function () {
            if (pendingHistoryHint) return refreshDownloadedHistory();
        }).catch(function () { /* The local poll retries after offline/window failures. */ });
    }

    function startHistoryPoll() {
        // BFCache restoration reuses this page instead of firing DOMContentLoaded.
        if (historyPollTimer !== null || !document.getElementById('react-chat-window-root')) return;
        historyPollTimer = window.setInterval(function () {
            return refreshDownloadedHistory().catch(function () { /* Local chat remains available offline. */ });
        }, 5000);
    }

    function unfinishedChat() {
        // Reuse actual live host/socket states rather than creating a sync busy flag.
        var host = window.reactChatWindowHost;
        var state = window.appState || {};
        // A cleared current lifecycle cannot borrow a previous role's fallback ID.
        var turnId = Object.prototype.hasOwnProperty.call(state, 'assistantTurnId')
            ? state.assistantTurnId : window._nekoAssistantTurnId;
        var messages = host && typeof host.getState === 'function' ? host.getState().messages || [] : [];
        // Completion may precede this script's listener. Only exact lifecycle
        // identities release delayed tail bubbles, never unknown streaming work.
        if (state.assistantTurnCompletedId) completedTurns.add(state.assistantTurnCompletedId);
        if (state.assistantTurnSettledId) completedTurns.add(state.assistantTurnSettledId);
        // Completed text and settled voice retain their turn ID until the next
        // user input. Pending submissions and real rendering still block merge.
        return !!(turnId && turnId !== state.assistantTurnCompletedId && turnId !== state.assistantTurnSettledId)
            || !!state.assistantTurnAwaitingBubble || !!window._lastSubmittedRequestId
            || !!(state.pendingTextTurnSubmitAt && Date.now() - state.pendingTextTurnSubmitAt < 15000)
            || !!window._isProcessingRealisticQueue || messages.some(function (message) {
            return message.status === 'sending' || (message.status === 'streaming'
                && !(message.role === 'assistant' && message.turnId && completedTurns.has(message.turnId)));
        });
    }

    function rememberDeletion(uid, receipt) {
        // A just-saved live bubble may not have appeared in a previous history page.
        if (uid === activeUid && activeName === characterName()) historyRequestGeneration++;
        var known = new Set(knownIds.get(uid) || []);
        receipt.messageIds.forEach(function (id) { if (ID.test(id)) known.add(id); });
        knownIds.set(uid, known);
        var localDeleted = new Set(deletedLocalIds.get(uid) || []);
        (receipt.localMessageIds || []).forEach(function (id) { if (ID.test(id)) localDeleted.add(id); });
        deletedLocalIds.set(uid, localDeleted);
        // Exact local hiding must survive a temporarily unavailable witness;
        // fetching history can fail while the ordinary current chat stays visible.
        var host = window.reactChatWindowHost;
        if (uid === activeUid && host && typeof host.getState === 'function' && typeof host.setMessages === 'function') {
            var deleted = new Set(receipt.messageIds);
            localDeleted.forEach(function (id) { deleted.add(id); });
            host.setMessages((host.getState().messages || []).filter(function (message) {
                var identity = message.id.indexOf('sync-original-') === 0 ? message.id.slice('sync-original-'.length)
                    : message.id.indexOf('local-reply-') === 0 ? message.id.slice('local-reply-'.length)
                    : liveIds.get(message.id) || (message.role === 'assistant' && assistantTurns.get(message.turnId));
                return !deleted.has(identity);
            }));
        }
    }

    function updateControls() {
        var recover = dialog.querySelector('[data-sync-action="recover"]');
        recover.disabled = operationRunning || !writerRecoveryRequired || !activeUid;
        recover.setAttribute('aria-busy', String(operationRunning));
    }

    async function refreshStatus() {
        var name = characterName();
        var state = await api('/status');
        if (characterName() !== name) return false;
        var role = state.characters.find(function (item) { return item.displayName === name; });
        activeUid = role ? role.characterSyncId : null;
        var binding = role && role.binding;
        writerRecoveryRequired = !!(state.writerRecoveryRequired || (binding && binding.writerRecoveryRequired));
        var error = role && (role.syncSetupError || (binding && binding.lastSyncError));
        statusNode.textContent = error ? describeError(error)
            : text(writerRecoveryRequired ? 'writerRecoveryRequired' : 'recoveryNotRequired');
        if (binding) {
            if (binding.pendingOriginals || binding.pendingDeletions) {
                statusNode.textContent += '\n' + text('pending', {
                    originals: binding.pendingOriginals || 0, deletions: binding.pendingDeletions || 0
                });
            }
            if (binding.localOnlyOriginals) statusNode.textContent += '\n' + text('oversizedLocalOnly', {count: binding.localOnlyOriginals});
            var awaiting = binding.historyBackfill && binding.historyBackfill.awaitingRegistration;
            if (awaiting) statusNode.textContent += '\n' + text('historyAwaitingRegistration', {count: awaiting});
        }
        updateControls();
        return true;
    }

    async function recoverSync() {
        if (operationRunning || !dialog.open || !writerRecoveryRequired || !activeUid) return;
        var requestedName = characterName(), requestedUid = activeUid;
        operationRunning = true;
        updateControls();
        try {
            if (!await refreshStatus() || !dialog.open || characterName() !== requestedName
                    || activeUid !== requestedUid || !writerRecoveryRequired) return;
            statusNode.textContent = text('working');
            var path = '/characters/' + requestedUid;
            var recovered = await api(path + '/recovery/preview', 'POST', { serviceUrl: DEFAULT_SERVICE_URL });
            if (!dialog.open || characterName() !== requestedName || activeUid !== requestedUid) return;
            await refreshStatus();
            if (!dialog.open || characterName() !== requestedName || activeUid !== requestedUid) return;
            // Recovery never grants a copied deletion or a historical upload.
            // The existing card switch owns combined profile/history consent.
            var deletionTarget = recovered.deletions.localName ? recovered.deletions : recovered.history;
            if (recovered.deletions.count && window.confirm(text('recoveryDeleteConfirm', {
                    count: recovered.deletions.count, localName: deletionTarget.localName,
                    cloudName: deletionTarget.cloudName
                }))) {
                await api(path + '/recovery/deletions/approve', 'POST', { taskId: recovered.deletions.taskId });
            }
            if (!dialog.open || characterName() !== requestedName || activeUid !== requestedUid) return;
            statusNode.textContent = text('recoveryChecked');
            if (recovered.historyError) statusNode.textContent += '\n' + text('initialHistoryFailed');
            await loadHistory();
        } catch (error) {
            if (dialog.open && characterName() === requestedName && activeUid === requestedUid) {
                statusNode.textContent = describeError(error);
            }
        } finally {
            operationRunning = false;
            updateControls();
        }
    }

    function buildDialog() {
        dialog = document.createElement('dialog');
        dialog.id = 'chat-sync-dialog';
        dialog.setAttribute('aria-labelledby', 'chat-sync-title');
        var header = document.createElement('div');
        header.className = 'chat-sync-dialog-header';
        var icon = document.createElement('img');
        icon.src = '/static/icons/set_on.png';
        icon.alt = '';
        header.appendChild(icon);
        var title = document.createElement('h2');
        title.id = 'chat-sync-title';
        title.textContent = text('title');
        header.appendChild(title);
        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'chat-sync-close';
        close.setAttribute('aria-label', text('close'));
        var closeIcon = document.createElement('img');
        closeIcon.src = '/static/icons/close_button.png';
        closeIcon.alt = '';
        close.appendChild(closeIcon);
        close.addEventListener('click', function () { dialog.close(); });
        header.appendChild(close);
        dialog.appendChild(header);
        var row = document.createElement('div');
        row.className = 'chat-sync-recovery-row';
        var recover = document.createElement('button');
        recover.type = 'button';
        recover.className = 'chat-sync-recover';
        recover.dataset.syncAction = 'recover';
        recover.textContent = text('recoverWriter');
        recover.disabled = true;
        recover.setAttribute('aria-describedby', 'chat-sync-recovery-tooltip');
        recover.addEventListener('click', recoverSync);
        row.appendChild(recover);
        var help = document.createElement('span');
        help.className = 'chat-sync-help';
        var helpButton = document.createElement('button');
        helpButton.type = 'button';
        helpButton.className = 'chat-sync-help-button';
        helpButton.textContent = '?';
        helpButton.setAttribute('aria-label', text('recoverWriterHelp'));
        helpButton.setAttribute('aria-describedby', 'chat-sync-recovery-tooltip');
        help.appendChild(helpButton);
        var tooltip = document.createElement('span');
        tooltip.id = 'chat-sync-recovery-tooltip';
        tooltip.setAttribute('role', 'tooltip');
        tooltip.textContent = text('recoverWriterTooltip');
        help.appendChild(tooltip);
        row.appendChild(help);
        dialog.appendChild(row);
        statusNode = document.createElement('p');
        statusNode.className = 'chat-sync-status';
        statusNode.setAttribute('role', 'status');
        dialog.appendChild(statusNode);
        dialog.addEventListener('close', function () { settingsCharacterName = null; });
        document.body.appendChild(dialog);
    }

    window.nekoChatSync = {
        openSettings: async function (name) {
            settingsCharacterName = name || null;
            if (!dialog) buildDialog();
            dialog.showModal();
            statusNode.textContent = text('working');
            dialog.querySelector('[data-sync-action="recover"]').disabled = true;
            try { await refreshStatus(); } catch (error) { statusNode.textContent = describeError(error); }
        },
        describeError: describeError,
        // Pure identity merge is exposed for direct browser-independent regression tests.
        mergeHistory: mergeHistory,
        loadHistory: loadHistory,
        notifyHistoryCommitted: notifyHistoryCommitted,
        markUserRequest: function (requestId, localBubbleId) {
            // This mapping is display-only and does not authorize backend identities.
            if (requestId && localBubbleId) userRequests.set(requestId, localBubbleId);
        },
        markLiveOriginal: function (messageId, localBubbleId) {
            // Only backend-generated fixed IDs are accepted from the current socket.
            if (typeof messageId === 'string' && ID.test(messageId) && localBubbleId) liveIds.set(localBubbleId, messageId);
        },
        markAcceptedUser: function (payload) {
            // Optimistic text bubbles are joined only through a matching request ID.
            var localId = userRequests.get(payload.request_id);
            if (localId) this.markLiveOriginal(payload.message_id, localId);
        },
        markAssistantOriginal: function (messageId, turnId) {
            // Realistic rendering can build bubbles later; its explicit turn ID stays stable.
            if (typeof messageId === 'string' && ID.test(messageId) && turnId) assistantTurns.set(turnId, messageId);
        }
    };

    function memoryBrowserDeletion(data) {
        var receipt = data && data.receipt;
        if (!data || data.action !== 'chat_sync_deleted' || !ID.test(data.characterSyncId || '') || !receipt
                || !Array.isArray(receipt.messageIds) || !receipt.messageIds.every(function (id) { return ID.test(id); })
                || !Array.isArray(receipt.localMessageIds) || !receipt.localMessageIds.every(function (id) { return ID.test(id); })) return;
        rememberDeletion(data.characterSyncId, receipt);
        loadHistory().catch(function () { /* Durable tombstones already hide exact IDs. */ });
    }
    if (typeof window.BroadcastChannel === 'function') {
        var memoryDeletionChannel = new window.BroadcastChannel('neko_page_channel');
        memoryDeletionChannel.addEventListener('message', function (event) { memoryBrowserDeletion(event.data); });
    }
    window.addEventListener('message', function (event) {
        if (event.origin === window.location.origin) memoryBrowserDeletion(event.data);
    });

    window.addEventListener('neko-assistant-turn-end', function (event) {
        // The realistic queue can create streaming tail bubbles after turn_end.
        // Keep earlier completions when the next turn replaces appState's ID.
        var turnId = event.detail && event.detail.turnId;
        if (typeof turnId === 'string' && turnId) completedTurns.add(turnId);
        retryPendingHistory();
    });
    window.addEventListener('neko-assistant-speech-end', retryPendingHistory);
    window.addEventListener('neko-realistic-queue-idle', retryPendingHistory);

    window.addEventListener('voice-input-socket-open', function () {
        // Initial connect and role switches restore local originals without cloud I/O.
        loadHistory().catch(function () { /* Optional sync never blocks local chat. */ });
    });

    document.addEventListener('DOMContentLoaded', function () {
        // Notifications wake backend downloads. This cheap local cursor check
        // belongs to the chat host, independently of optional settings controls.
        startHistoryPoll();
        var button = document.getElementById('chatSyncButton');
        if (!button) return;
        button.addEventListener('click', async function () {
            await window.nekoChatSync.openSettings();
        });
    });

    window.addEventListener('pagehide', function () {
        // A closed chat page owns no timer; the backend scheduler continues.
        if (historyPollTimer !== null) window.clearInterval(historyPollTimer);
        historyPollTimer = null;
    });
    async function foregroundCatchup() {
        // The local bridge owns credentials and all enabled roles. A foreground
        // wake does not perform manual sync or bypass durable retry fences.
        if (foregroundWakeRunning || document.visibilityState === 'hidden' || !document.getElementById('react-chat-window-root')) return;
        foregroundWakeRunning = true;
        try { await api('/wake', 'POST'); } catch (_) { /* Local chat stays usable. */ }
        finally { foregroundWakeRunning = false; }
        await refreshDownloadedHistory().catch(function () { /* Retry on the next local tick. */ });
    }
    document.addEventListener('visibilitychange', foregroundCatchup);
    window.addEventListener('pageshow', function () { startHistoryPoll(); return foregroundCatchup(); });
})();

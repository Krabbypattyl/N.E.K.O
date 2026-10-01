(function (root) {
    'use strict';
    const api = root.NekoClickGuide;
    const stateApi = root.NekoClickGuideState;
    const t = key => root.t('clickGuide.' + key);
    const isChat = location.pathname.replace(/\/$/, '') === '/chat';
    const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('neko_click_guide') : null;
    let active = false;
    let runId = null;
    let currentRunner = null;
    let remoteResolve = null;
    let lastRemoteRun = null;
    let remoteStopped = false;
    let lastPing = 0;

    function send(type, id, reason, details = {}) {
        const message = { action: 'click_guide', type, runId: id, reason, ...details };
        channel?.postMessage(message);
        const bridge = root.nekoTutorialOverlay;
        if (isChat) bridge?.relayToPet?.(message);
        else bridge?.relayToChat?.(message);
    }
    function setActive(value) {
        active = value;
        root.isNekoClickGuideActive = value;
        root.dispatchEvent(new CustomEvent('neko:click-guide-active', { detail: { active: value } }));
    }
    function labels() {
        return { tour: t('tour'), next: t('next'), back: t('back'), skip: t('skip'), unavailable: t('unavailable'),
            nativeFallback: t('nativeFallback') };
    }
    function run(steps, section, options = {}) {
        return new Promise((resolve, reject) => {
            const runner = api.createRunner({ steps, labels: { ...labels(), section }, ...options,
                onEnd: reason => resolve({ reason, history: runner.history, skipped: runner.skipped }),
                presentation: api.createNativePresentation() });
            currentRunner = runner;
            runner.start().catch(reject);
        });
    }
    async function runChat(onReady, resume) {
        let restore;
        try {
            restore = await api.prepareChat();
            if (isChat && remoteStopped) return { reason: 'failed' };
            const steps = api.chatSteps();
            if (steps[resume?.startIndex]?.id === 'restore') {
                root.reactChatWindowHost.setChatSurfaceMode('minimized');
                await api.waitUntil(() => root.reactChatWindowHost.getChatSurfaceMode() === 'minimized',
                    new AbortController().signal, 3000);
            }
            onReady?.();
            return await run(steps, t('sections.chat'), resume);
        } finally {
            await restore?.();
        }
    }
    async function receive(message) {
        if (!message || message.action !== 'click_guide' || typeof message.runId !== 'string') return;
        if (isChat && message.type === 'start' && !active && lastRemoteRun !== message.runId) {
            if (root.isInTutorial || root.isNekoHomeTutorialPending) return;
            lastRemoteRun = runId = message.runId;
            remoteStopped = false;
            lastPing = Date.now();
            setActive(true);
            const lease = setInterval(() => {
                if (Date.now() - lastPing > 6000) {
                    remoteStopped = true;
                    void currentRunner?.stop('failed');
                }
                send('heartbeat', runId);
            }, 1000);
            let result = { reason: 'failed' };
            try { result = await runChat(() => send('ready', runId), message.resume); }
            catch (error) { console.warn('[ClickGuide] Chat:', error); }
            finally {
                clearInterval(lease);
                send('done', runId, result.reason, { history: result.history, skipped: result.skipped });
                setActive(false);
                runId = null;
            }
        } else if (message.runId === runId) {
            if (isChat && message.type === 'heartbeat') lastPing = Date.now();
            if (message.type === 'stop' && isChat) {
                remoteStopped = true;
                await currentRunner?.stop('skipped');
            }
            if (!isChat) remoteResolve?.(message);
        }
    }
    channel?.addEventListener('message', event => void receive(event.data));
    root.addEventListener('neko:tutorial-overlay-relay', event => void receive(event.detail));
    root.addEventListener('message', event => {
        if (event.origin !== location.origin) return;
        const data = event.data;
        if (data?.__nekoTutorialOverlayRelay === true) void receive(data.payload);
        else if (data?.action === '__nekoTutorialOverlayRelay') void receive(data.detail);
    });
    root.addEventListener('pagehide', () => {
        if (runId) send(isChat ? 'done' : 'stop', runId, 'skipped');
        void currentRunner?.stop('skipped');
    });

    async function remoteChat(resume) {
        return new Promise(resolve => {
            let ready = false;
            let timer = setTimeout(() => finish('failed'), 15000);
            const heartbeat = setInterval(() => send('heartbeat', runId), 1000);
            function finish(reason, result = {}) {
                clearTimeout(timer);
                clearInterval(heartbeat);
                remoteResolve = null;
                resolve({ reason, ...result });
            }
            remoteResolve = message => {
                if (message.type === 'ready') ready = true;
                if (ready && ['ready', 'heartbeat'].includes(message.type)) {
                    clearTimeout(timer);
                    timer = setTimeout(() => finish('failed'), 6000);
                }
                if (message.type === 'done') finish(message.reason,
                    { history: message.history, skipped: message.skipped });
            };
            send('start', runId, undefined, { resume });
        });
    }
    async function start() {
        if (active || root.isInTutorial || root.universalTutorialManager?.isTutorialRunning) {
            root.universalTutorialManager?.dispatchStartupGreetingRelease('click-guide-start-deferred');
            return false;
        }
        runId = 'click-' + crypto.randomUUID();
        setActive(true);
        const manager = root.universalTutorialManager;
        manager?.clearStartupGreetingRelease('click-guide-start');
        let outcome = 'skipped';
        let restoreFloating;
        let saving = false;
        try {
            const state = await stateApi.refresh();
            const localChat = api.resolveTarget('#react-chat-window-shell');
            const native = root.nekoTutorialOverlay;
            const chat = resume => !localChat && native?.relayToChat ? remoteChat(resume) : runChat(null, resume);
            let chatResult = await chat();
            while (chatResult.reason === 'completed') {
                restoreFloating = await api.prepareFloating();
                const floating = await run(api.floatingSteps(), t('sections.floating'), { backAtStart: true });
                await restoreFloating();
                restoreFloating = null;
                if (floating.reason === 'back') {
                    const path = chatResult.history || [];
                    if (!path.length) { outcome = 'failed'; break; }
                    runId = 'click-' + crypto.randomUUID();
                    chatResult = await chat({ startIndex: path.at(-1), initialHistory: path.slice(0, -1),
                        initialSkipped: chatResult.skipped || [] });
                    continue;
                }
                outcome = floating.reason;
                if (outcome === 'failed') {
                    root.showStatusToast?.(t('connection.body'), 5000);
                    return false;
                }
                break;
            }
            if (chatResult.reason === 'failed' || outcome === 'failed') {
                // A missing peer must not count as a completed chat tutorial.
                await run([{ title: t('connection.title'), body: t('connection.body'), nextLabel: t('close') }]);
                return false;
            }
            saving = true;
            await stateApi.update('finish', { status: outcome }, state.revision);
            return true;
        } catch (error) {
            console.warn('[ClickGuide] Session:', error);
            root.showStatusToast?.(t(saving ? 'saveFailed' : 'connection.body'), 5000);
            return false;
        } finally {
            try {
                send('stop', runId);
                await currentRunner?.stop('skipped');
            } finally {
                try { await restoreFloating?.(); }
                finally {
                    setActive(false);
                    runId = null;
                    manager?.dispatchStartupGreetingRelease('click-guide-ended');
                }
            }
        }
    }
    async function choose() {
        const wrapper = document.createElement('div');
        wrapper.className = 'click-guide-choice';
        const card = document.createElement('section');
        card.className = 'click-guide-card';
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-modal', 'true');
        card.setAttribute('aria-label', t('choice.title'));
        const title = document.createElement('h2');
        title.textContent = t('choice.title');
        const description = document.createElement('p');
        description.textContent = t('choice.body');
        const actions = document.createElement('div');
        actions.className = 'click-guide-actions';
        card.append(title, description, actions);
        wrapper.append(card);
        document.body.append(wrapper);
        const presentation = api.createNativePresentation();
        return new Promise(resolve => {
            const render = disabled => presentation?.update({ step: 0, rect: null, title: title.textContent,
                body: description.textContent, status: '', progress: '', nextDisabled: disabled, backDisabled: true,
                labels: { tour: t('choice.title'), skip: t('choice.sevenDay') }, nextLabel: t('choice.click') });
            ['click', 'seven-day'].forEach((choice, index) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = t(index === 0 ? 'choice.click' : 'choice.sevenDay');
                if (index === 0) button.className = 'click-guide-next';
                button.onclick = async () => {
                    const buttons = [...actions.children];
                    buttons.forEach(item => { item.disabled = true; });
                    render(true);
                    try {
                        await stateApi.update('choose', { choice });
                        await presentation?.close();
                        wrapper.remove();
                        resolve(choice);
                    } catch (error) {
                        description.textContent = t('saveFailed');
                        buttons.forEach(item => { item.disabled = false; });
                        // A failed save must never trap the user behind the modal.
                        if (actions.children.length === 2) {
                            const later = document.createElement('button');
                            later.type = 'button';
                            later.textContent = t('close');
                            later.onclick = async () => {
                                await presentation?.close();
                                wrapper.remove();
                                resolve(null);
                            };
                            actions.append(later);
                        }
                        void presentation?.close();
                        wrapper.style.visibility = '';
                    }
                };
                actions.append(button);
            });
            actions.firstElementChild.focus();
            if (presentation) {
                wrapper.style.visibility = 'hidden';
                presentation.bind({ next: () => actions.children[0].click(), skip: () => actions.children[1].click(),
                    failed: () => { void presentation.close(); wrapper.style.visibility = ''; } });
                render(false);
            }
            wrapper.addEventListener('keydown', event => {
                if (event.key === 'Tab') {
                    event.preventDefault();
                    const buttons = [...actions.children];
                    buttons[(buttons.indexOf(document.activeElement) + 1) % buttons.length].focus();
                }
            });
        });
    }
    api.handleStartup = async function (manager) {
        if (isChat || manager.currentPage !== 'home') return false;
        try {
            const languageWait = new AbortController();
            await api.waitUntil(() => manager.isI18nReady(), languageWait.signal, 15000);
            const old = root.NekoSevenDayTutorialState?.loadState();
            if (old?.manualResetRound) return false;
            let state = await stateApi.ready();
            if (!state) { manager.dispatchStartupGreetingRelease('click-guide-state-unavailable'); return false; }
            if (!state.choice && !state.pending) {
                // Also respect old browser progress which the seven-day authority has just imported.
                if (old?.completedRounds?.length || old?.skippedRounds?.length || old?.lastAutoShownRound) {
                    state = await stateApi.update('choose', { choice: 'seven-day' });
                } else {
                    manager.setHomeTutorialPending(true);
                    if (!await choose()) {
                        manager.dispatchStartupGreetingRelease('click-guide-choice-deferred');
                        return false;
                    }
                    state = stateApi.get();
                }
            }
            if (state.pending) {
                if (await start()) return true;
                // Keep seven-day startup available on unsupported/broken guide hosts.
                await stateApi.update('choose', { choice: 'seven-day' }, state.revision);
                return false;
            }
            if (state.choice === 'click') { manager.dispatchStartupGreetingRelease('click-guide-already-seen'); return true; }
            return false;
        } catch (error) {
            console.warn('[ClickGuide] Startup:', error);
            manager.dispatchStartupGreetingRelease('click-guide-startup-failed');
            return false;
        }
    };
    api.startHome = start;
})(window);

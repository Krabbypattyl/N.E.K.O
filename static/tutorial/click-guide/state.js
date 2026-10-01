(function (root) {
    'use strict';
    let state = null;
    let settled = false;
    async function refresh() {
        if (typeof root.waitForStorageLocationStartupBarrier === 'function') {
            await root.waitForStorageLocationStartupBarrier();
        } else if (root.__nekoStorageLocationStartupBarrier) {
            await root.__nekoStorageLocationStartupBarrier;
        }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch('/api/click-guide/state', { cache: 'no-store', signal: controller.signal });
            if (!response.ok) throw new Error('click_guide_state_unavailable');
            state = await response.json();
            return state;
        } finally {
            clearTimeout(timer);
        }
    }
    const ready = refresh().catch(error => { console.warn('[ClickGuide]', error); return null; })
        .finally(() => { settled = true; });
    async function update(action, values = {}, expectedRevision = state?.revision) {
        const configResponse = await fetch('/api/config/page_config', { cache: 'no-store' });
        if (!configResponse.ok) throw new Error('click_guide_config_unavailable');
        const config = await configResponse.json();
        const response = await fetch('/api/click-guide/state', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': config.autostart_csrf_token || '' },
            body: JSON.stringify({ action, ...values, expectedRevision })
        });
        const result = await response.json();
        if (result.state) state = result.state;
        if (!response.ok) throw new Error(response.status === 409 ? 'click_guide_state_conflict' : 'click_guide_save_failed');
        return state;
    }
    function isSevenDayOverride(sevenDay) {
        if (!Number.isFinite(state?.selectedAt)) return false;
        const latestReset = sevenDay?.resetHistory?.at(-1);
        return Date.parse(latestReset?.resetAt) > state.selectedAt;
    }
    function resumeSevenDay() {
        const sevenDay = root.NekoSevenDayTutorialState;
        const progress = sevenDay?.loadState();
        if (state?.choice === 'click' && !state.pending && progress?.manualResetRound
                && !isSevenDayOverride(progress)) {
            // Retire the old replay request once; keep dates and settled rounds
            // so normal daily scheduling can proceed after the click replay.
            progress.manualResetRound = null;
            progress.pendingRound = null;
            progress.updatedAt = new Date().toISOString();
            sevenDay.saveState(progress);
        }
        return progress;
    }
    root.NekoClickGuideState = { ready: () => ready, isReady: () => settled, refresh, update, get: () => state, isSevenDayOverride, resumeSevenDay };
})(window);

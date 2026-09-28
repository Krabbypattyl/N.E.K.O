(function (root) {
    'use strict';
    let state = null;
    async function refresh() {
        if (typeof root.waitForStorageLocationStartupBarrier === 'function') {
            await root.waitForStorageLocationStartupBarrier();
        } else if (root.__nekoStorageLocationStartupBarrier) {
            await root.__nekoStorageLocationStartupBarrier;
        }
        const response = await fetch('/api/click-guide/state', { cache: 'no-store' });
        if (!response.ok) throw new Error('click_guide_state_unavailable');
        state = await response.json();
        return state;
    }
    const ready = refresh().catch(error => { console.warn('[ClickGuide]', error); return null; });
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
    root.NekoClickGuideState = { ready: () => ready, refresh, update, get: () => state };
})(window);

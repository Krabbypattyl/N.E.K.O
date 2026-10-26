/** Market installs the selected package; the local selector owns starting and continuing. */
(function () {
    'use strict';
    var transport = window.nekoTheaterTransport;
    if (!transport) return;
    var base = '/api/theater-market/launch';
    var channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('neko_page_channel') : null;
    function label(key) { return window.t ? window.t('theater.' + key) : key; }
    function post(data) {
        var message = transport.createMessage('theater-market', data);
        if (channel) channel.postMessage(message);
        window.postMessage(message, window.location.origin);
        if (window.opener && !window.opener.closed) window.opener.postMessage(message, window.location.origin);
    }
    function openMarket() {
        var popup = window.open('/theater?market=browse', 'neko_theater_market');
        if (!popup && typeof window.showStatusToast === 'function') window.showStatusToast(label('marketConnectError'), 5000);
        return !!popup;
    }
    async function redirectSelector() {
        var query = new URLSearchParams(window.location.search);
        if (!query.has('market')) return;
        var status = document.getElementById('theater-selector-status');
        try {
            if (['127.0.0.1', 'localhost', '[::1]'].indexOf(window.location.hostname) < 0) {
                if (status) {
                    status.setAttribute('data-i18n', 'theater.marketLocalOnly');
                    status.textContent = label('marketLocalOnly');
                }
                return;
            }
            if (status) {
                status.setAttribute('data-i18n', 'theater.marketPreparing');
                status.textContent = label('marketPreparing');
            }
            var body = {random: query.get('market') === 'random'};
            if (query.get('share_id')) {
                body.share_id = Number(query.get('share_id'));
                body.revision = Number(query.get('market_revision'));
                body.file_sha256 = query.get('file_sha256');
            }
            var result = await transport.requestJson(base, {method: 'POST', body: body});
            if (!result.ok) throw new Error(result.reason);
            post({action: 'theater:market-opened', request_id: result.request_id});
            window.location.replace(result.url);
        } catch (_) {
            if (status) {
                status.setAttribute('data-i18n', 'theater.marketConnectError');
                status.textContent = label('marketConnectError');
            }
        }
    }
    window.nekoTheaterMarket = Object.freeze({open: openMarket, redirectSelector: redirectSelector});
})();

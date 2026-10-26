"""Market entry, host handoff and window completion stay outside the engine."""

import pytest
import json
from pathlib import Path
from playwright.sync_api import expect


@pytest.mark.frontend
def test_lan_market_entry_explains_local_only_import_without_pairing(page):
    repo = Path(__file__).resolve().parents[2]
    message = json.loads((repo / 'static/locales/zh-CN.json').read_text(encoding='utf-8'))['theater']['marketLocalOnly']
    def serve(route):
        if route.request.url.endswith('/theater_market.js'):
            return route.fulfill(path=repo / 'static/js/theater_market.js', content_type='application/javascript')
        route.fulfill(content_type='text/html', body=f'''
            <meta charset="utf-8">
            <div id="theater-selector-status"></div>
            <script>window.__requests = 0;
            window.nekoTheaterTransport = {{requestJson: () => {{window.__requests++; throw Error('unexpected pairing');}}}};
            window.t = () => {json.dumps(message, ensure_ascii=False)};</script>
            <script src="/theater_market.js"></script>
        ''')
    page.route('http://192.168.1.100:48911/**', serve)
    page.goto('http://192.168.1.100:48911/theater?market=browse')
    page.evaluate('window.nekoTheaterMarket.redirectSelector()')
    expect(page.locator('#theater-selector-status')).to_have_text(message)
    assert page.evaluate('window.__requests') == 0


@pytest.mark.frontend
def test_market_entry_is_independent_of_toggle_for_pointer_and_keyboard(mock_page, running_server):
    mock_page.add_init_script("window.localStorage.setItem('neko_tutorial_settings', 'seen')")
    mock_page.route('**/api/seven-day-tutorial/state', lambda route: route.fulfill(json={
        'success': True, 'initialized': True, 'revision': 1, 'state': {'completedRounds': list(range(1, 8))}}))
    mock_page.route('**/api/characters/persona-onboarding-state', lambda route: route.fulfill(json={
        'success': True, 'state': {'status': 'completed'}}))
    mock_page.goto(running_server, wait_until="domcontentloaded")
    mock_page.wait_for_function("() => typeof window.createChatModeToggle === 'function' && window.nekoTheaterMarket")
    mock_page.wait_for_function("() => window.i18n && window.i18n.isInitialized")
    mock_page.evaluate("""() => {
        window.proactiveTheaterInviteEnabled = false;
        window.__marketSaved = 0;
        window.saveNEKOSettings = () => window.__marketSaved++;
        window.__marketOpened = [];
        window.open = url => { window.__marketOpened.push(url); return {}; };
        const row = window.createChatModeToggle({checkboxId:'test-theater-entry',
            labelKey:'settings.toggles.proactiveTheaterInviteChat',
            tooltipKey:'settings.toggles.proactiveTheaterInviteChatTooltip',
            globalVarName:'proactiveTheaterInviteEnabled'});
        row.id = 'test-theater-entry-row';
        Object.assign(row.style, {position:'fixed',top:'20px',left:'20px',width:'320px',zIndex:'2147483647',background:'white',pointerEvents:'auto'});
        document.body.appendChild(row);
    }""")
    row = mock_page.locator("#test-theater-entry-row")
    button = row.locator('button[data-i18n="theater.marketEnter"]')
    expect(button).to_be_visible()
    button.click()
    button.focus()
    button.press("Enter")
    button.press("Space")
    assert mock_page.evaluate("window.__marketOpened") == ["/theater?market=browse"] * 3
    assert mock_page.evaluate("window.proactiveTheaterInviteEnabled") is False
    assert mock_page.evaluate("window.__marketSaved") == 0
    row.locator(".chat-mode-indicator").click()
    assert mock_page.evaluate("window.proactiveTheaterInviteEnabled") is True
    assert mock_page.evaluate("window.__marketSaved") == 1


def _market_host(page, running_server):
    """Real transport/adapter, with an isolated host and no model or user data."""
    page.route('**/__market_test_host', lambda route: route.fulfill(content_type='text/html', body='''
        <button onclick="nekoTheaterMarket.open()">Market</button>
        <script src="/static/js/theater_transport.js"></script>
        <script>window.__starts = [];
        window.addEventListener('message', event => {
            const data = event.data;
            if (data.action === 'theater:start-request') {
                window.__starts.push(data);
            }
        });</script>
        <script src="/static/js/theater_market.js"></script>'''))
    page.goto(running_server + '/__market_test_host')


@pytest.mark.frontend
def test_market_bootstrap_never_initializes_or_displays_local_selector(page, running_server):
    _market_host(page, running_server)
    requests = []
    page.context.route('**/api/theater-numeric/**', lambda route: (
        requests.append(route.request.url), route.fulfill(json={'ok': False})))
    page.context.route('**/api/theater-market/launch', lambda route: route.fulfill(json={
        'ok': False, 'reason': 'isolated_connection_failure'}))
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Market', exact=True).click()
    popup = opened.value
    expect(popup.locator('#theater-selector-status')).to_have_text('无法连接剧本市场，请保持 NEKO 开启并重试。')
    expect(popup.locator('#theater-story-list')).to_have_count(0)
    expect(popup.locator('#theater-start-btn')).to_have_count(0)
    assert not requests
    assert len(page.context.pages) == 2  # host plus exactly one entry window


@pytest.mark.frontend
def test_market_entry_replaces_idle_library_without_starting(page, running_server):
    _market_host(page, running_server)
    calls = []
    page.context.route('**/api/theater-market/launch/test-launch**', lambda route: (
        calls.append(route.request.url), route.fulfill(status=404, json={'ok': False})))
    page.context.route('**/api/theater-market/launch', lambda route: route.fulfill(json={
        'ok': True, 'request_id': 'test-launch', 'url': 'http://market.test/selected'}))
    page.context.route('http://market.test/**', lambda route: route.fulfill(body='<h1>Market selection</h1>'))
    page.context.route('**/api/theater-numeric/stories', lambda route: route.fulfill(json={
        'ok': True, 'stories': [], 'character_id': 'test-cat'}))
    page.context.route('**/api/theater-numeric/memory/stories', lambda route: route.fulfill(json={
        'ok': True, 'stories': []}))
    with page.expect_popup() as old_opened:
        page.evaluate("window.open('/theater', 'old_local_library')")
    old_library = old_opened.value
    expect(old_library.locator('#theater-selector-status')).to_have_attribute('data-i18n', 'theater.ready')
    with old_library.expect_event('close'):
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Market', exact=True).click()
    popup = opened.value
    expect(popup.get_by_role('heading', name='Market selection')).to_be_visible()
    assert old_library.is_closed()
    assert not calls
    assert page.evaluate('window.__starts') == []
    assert len(page.context.pages) == 2

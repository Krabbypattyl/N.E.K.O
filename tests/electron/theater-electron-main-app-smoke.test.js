const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const repoRoot = path.resolve(__dirname, '..', '..');
const pcRootCandidates = [
  path.join(repoRoot, 'N.E.K.O.-PC'),
  path.join(repoRoot, '..', 'N.E.K.O.-PC'),
];

/** Locate the sibling or nested N.E.K.O.-PC checkout for the main-app smoke. */
function getPcRoot() {
  const pcRoot = pcRootCandidates.find((candidate) => fs.existsSync(candidate));
  assert.ok(pcRoot, 'N.E.K.O.-PC checkout is required for the Electron main-app smoke');
  return pcRoot;
}

/** Resolve the Electron binary from N.E.K.O.-PC dependencies. */
function getElectronBinary(pcRoot) {
  const electron = require(path.join(pcRoot, 'node_modules', 'electron'));
  assert.equal(typeof electron, 'string', 'electron package must expose the binary path');
  assert.ok(fs.existsSync(electron), `missing electron binary: ${electron}`);
  return electron;
}

/** 启动隔离后端：真实提供 theater 模板与脚本，并保存一次输入后的公开恢复快照。 */
function startFakeBackend() {
  return new Promise((resolve, reject) => {
    const theaterHtml = fs.readFileSync(path.join(repoRoot, 'templates', 'theater.html'), 'utf8')
      .replaceAll('{{ static_asset_version }}', 'electron-main-smoke');
    const theaterScript = fs.readFileSync(path.join(repoRoot, 'static', 'js', 'theater.js'), 'utf8');
    const windowControlsScript = fs.readFileSync(
      path.join(repoRoot, 'static', 'js', 'window_controls.js'),
      'utf8',
    );
    let sessionActive = false;
    let startRequests = 0;
    let stateRequests = 0;
    let activeRequests = 0;
    let turnRequests = 0;
    let freeStartRequests = 0;
    let freeStateRequests = 0;
    let freeActiveRequests = 0;
    let freeTurnRequests = 0;
    let systemStatusRequests = 0;
    let chatPageRequests = 0;
    let stateRevision = 0;
    let latestDialogue = '第一次启动的公开对白。';
    let lastTurnRequest = null;

    // 只返回玩家可见恢复字段，确保 Electron smoke 与正式公开 session 协议一致。
    function publicSessionSnapshot() {
      return {
        ok: true,
        session_id: 'electron_restore_session',
        story_id: 'electron_restore_story',
        state_revision: stateRevision,
        can_resume: true,
        stale: false,
        scene: { scene_id: 'electron_scene', title: '桌边', text: '桌上的剧本仍停在同一页。' },
        narration: { text: '' },
        dialogue: { text: latestDialogue },
        scenario_board: {
          available_props: [{ id: 'prop_note', label: '折叠便笺', public_hint: '尚未展开。' }],
          used_props: [],
          discovered_clues: [],
        },
        scenario_trace: null,
        suggestion_options: [
          { choice_id: 'choice_open_note', label: '展开折叠便笺', choice_mode: 'action' },
        ],
        ending: { should_offer_ending: false, should_end_session: false },
      };
    }

    // 统一 JSON 响应，避免测试服务在各接口重复拼响应头。
    function sendJson(res, payload) {
      res.setHeader('content-type', 'application/json; charset=utf-8');
      res.end(JSON.stringify(payload));
    }

    // 读取真实 theater.js 发出的 JSON，避免用测试旁路伪造玩家回合。
    function readJsonBody(req) {
      return new Promise((bodyResolve, bodyReject) => {
        let rawBody = '';
        req.setEncoding('utf8');
        req.on('data', (chunk) => { rawBody += chunk; });
        req.on('end', () => {
          try {
            bodyResolve(rawBody ? JSON.parse(rawBody) : {});
          } catch (error) {
            bodyReject(error);
          }
        });
        req.on('error', bodyReject);
      });
    }

    const server = http.createServer(async (req, res) => {
      const url = new URL(req.url || '/', 'http://127.0.0.1');
      if (url.pathname === '/health') {
        sendJson(res, { app: 'N.E.K.O', service: 'main' });
        return;
      }
      if (url.pathname === '/api/system/status') {
        // 放行真实 PC storage gate，令 smoke 同时获得独立的普通聊天窗口隔离信号。
        systemStatusRequests += 1;
        sendJson(res, {
          ready: true,
          lifecycle_state: 'ready',
          storage: {
            selection_required: false,
            migration_pending: false,
            recovery_required: false,
          },
        });
        return;
      }
      if (url.pathname === '/api/theater/stories') {
        sendJson(res, {
          ok: true,
          stories: [{
            id: 'electron_restore_story',
            title: 'Electron 恢复剧本',
            background: '桌边留着一张尚未展开的便笺，等待两位参与者继续确认。',
            initial_scene: {
              scene_id: 'electron_scene',
              title: '桌边',
              text: '桌上的剧本仍停在同一页。',
            },
          }],
        });
        return;
      }
      if (url.pathname === '/api/theater/session/start') {
        sessionActive = true;
        startRequests += 1;
        sendJson(res, publicSessionSnapshot());
        return;
      }
      if (url.pathname === '/api/theater/session/input') {
        const body = await readJsonBody(req);
        turnRequests += 1;
        lastTurnRequest = {
          session_id: String(body.session_id || ''),
          input_kind: String(body.input_kind || ''),
          client_turn_id: String(body.client_turn_id || ''),
          base_revision: body.base_revision,
          message: String(body.message || ''),
        };
        stateRevision += 1;
        latestDialogue = '我听见你说要检查便笺了，我们现在就一起看。';
        sendJson(res, publicSessionSnapshot());
        return;
      }
      if (url.pathname === '/api/theater/session/state') {
        stateRequests += 1;
        sendJson(res, publicSessionSnapshot());
        return;
      }
      if (url.pathname === '/api/theater/session/active') {
        activeRequests += 1;
        sendJson(
          res,
          sessionActive
            ? publicSessionSnapshot()
            : { ok: false, reason: 'active_session_not_found' },
        );
        return;
      }
      if (url.pathname === '/api/theater/free/session/start') {
        freeStartRequests += 1;
        sendJson(res, { ok: false, reason: 'session_not_found' });
        return;
      }
      if (url.pathname === '/api/theater/free/session/input') {
        freeTurnRequests += 1;
        sendJson(res, { ok: false, reason: 'session_not_found' });
        return;
      }
      if (url.pathname === '/api/theater/free/session/state') {
        freeStateRequests += 1;
        sendJson(res, { ok: false, reason: 'session_not_found' });
        return;
      }
      if (url.pathname === '/api/theater/free/session/active') {
        // theater.js restores both modes on startup. Mirror the production
        // inactive response so this main-process smoke exercises its real
        // renderer path instead of turning a missing fake route into a 404.
        freeActiveRequests += 1;
        sendJson(res, { ok: false, reason: 'session_not_found' });
        return;
      }
      if (url.pathname === '/__smoke-metrics') {
        sendJson(res, {
          startRequests,
          stateRequests,
          activeRequests,
          turnRequests,
          freeStartRequests,
          freeStateRequests,
          freeActiveRequests,
          freeTurnRequests,
          systemStatusRequests,
          chatPageRequests,
          lastTurnRequest,
        });
        return;
      }
      res.setHeader('content-type', 'text/html; charset=utf-8');
      if (url.pathname === '/' || url.pathname === '/index.html') {
        res.end('<!doctype html><html><body data-pet-app><button id="open-theater">pet</button></body></html>');
        return;
      }
      if (url.pathname === '/theater') {
        res.end(theaterHtml);
        return;
      }
      if (url.pathname === '/static/js/theater.js') {
        res.setHeader('content-type', 'application/javascript; charset=utf-8');
        res.end(theaterScript);
        return;
      }
      if (url.pathname === '/static/js/window_controls.js') {
        // 标题栏按钮的真实绑定逻辑必须参与 smoke，不能只探测 preload API 是否存在。
        res.setHeader('content-type', 'application/javascript; charset=utf-8');
        res.end(windowControlsScript);
        return;
      }
      if (url.pathname.startsWith('/static/')) {
        // 恢复链只依赖 theater.js；其它装饰资源返回空内容，避免引入与本轮无关的网络依赖。
        res.setHeader(
          'content-type',
          url.pathname.endsWith('.css') ? 'text/css; charset=utf-8' : 'application/javascript; charset=utf-8',
        );
        res.end('');
        return;
      }
      if (url.pathname === '/chat' || url.pathname === '/subtitle') {
        if (url.pathname === '/chat') chatPageRequests += 1;
        res.end('<!doctype html><html><body data-aux-window></body></html>');
        return;
      }
      res.statusCode = 404;
      res.end('not found');
    });
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      resolve({
        server,
        baseUrl: `http://127.0.0.1:${server.address().port}/`,
      });
    });
  });
}

/** Write isolated PC userData config that points the real main process at the fake backend. */
function writeIsolatedPcConfig(userDataDir, baseUrl) {
  const config = {
    apiBaseUrl: baseUrl,
    customUrls: {
      MAIN_SERVER_URL: baseUrl,
    },
    autoLaunch: false,
    useSystemProxy: false,
    streamerMode: true,
    darkMode: false,
    globalAlwaysOnTop: false,
    preventSystemSleep: false,
    compatibilityMode: false,
    linuxForceX11: false,
  };
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(path.join(userDataDir, 'core_config.txt'), JSON.stringify(config, null, 2), 'utf8');
}

/** Write the wrapper that requires the real N.E.K.O.-PC src/main.js and inspects its windows. */
function writeMainAppWrapper(tempDir, pcRoot) {
  const wrapperPath = path.join(tempDir, 'pc-main-smoke-wrapper.js');
  fs.writeFileSync(wrapperPath, `
const { app, BrowserWindow } = require('electron');

app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-software-rasterizer');

const baseUrl = process.env.NEKO_THEATER_MAIN_SMOKE_BASE_URL;
const pcMainPath = process.env.NEKO_PC_MAIN_PATH;
let finished = false;
let phase = 'OPEN_INITIAL';
let initialTheaterWindowId = null;
let reopenedTheaterWindowId = null;
let closeObserved = false;
let metricsAfterReload = null;
let windowControlResult = null;
let submittedLogBeforeReload = '';

// Finish the smoke with a machine-readable result line before exiting Electron.
function finish(code, payload) {
  if (finished) return;
  finished = true;
  console.log('NEKO_THEATER_MAIN_SMOKE_RESULT ' + JSON.stringify(payload || {}));
  setTimeout(() => app.exit(code), 50);
}

// Read a fake-backend path without allowing the normal chat window to be mistaken for Pet.
function backendPathOf(win) {
  try {
    const url = win.webContents.getURL();
    if (!url || !url.startsWith(baseUrl)) return '';
    return new URL(url).pathname;
  } catch (_) {
    return '';
  }
}

// Return true only for the real fake-backend Pet/root window.
function isPetBackendWindow(win) {
  const pathname = backendPathOf(win);
  return pathname === '/' || pathname === '/index.html';
}

function isNormalChatWindow(win) {
  return backendPathOf(win) === '/chat';
}

function isTheaterWindow(win) {
  return backendPathOf(win) === '/theater';
}

async function waitFor(label, predicate, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 8000);
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (_) {
      // Window creation and destruction race with polling; keep waiting for the stable state.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(label);
}

async function waitForWindowState(win, label, predicate) {
  await waitFor(label, () => !win.isDestroyed() && predicate(win), 5000);
}

// Ask the loaded pet page to open the theater route through the real PC child-window handler.
async function openTheaterFromPet(win) {
  if (phase !== 'OPEN_INITIAL' && phase !== 'OPEN_REOPEN') return;
  if (!win || win.isDestroyed()) return;
  if (!isPetBackendWindow(win)) return;
  // Change phase before window.open so the fallback poll cannot create a duplicate child.
  phase = phase === 'OPEN_INITIAL' ? 'WAIT_INITIAL_LOAD' : 'WAIT_REOPEN_LOAD';
  try {
    await win.webContents.executeJavaScript("window.open('/theater', '_blank'); true");
  } catch (error) {
    finish(3, { error: 'open-theater-failed', detail: String(error && error.message || error) });
  }
}

async function readRestoredTheaterState(win) {
  return win.webContents.executeJavaScript("(async () => { const waitFor = async (predicate) => { const deadline = Date.now() + 8000; while (Date.now() < deadline) { if (predicate()) return; await new Promise((resolve) => setTimeout(resolve, 50)); } throw new Error('restore-timeout'); }; await waitFor(() => !document.querySelector('#theater-input').disabled && document.querySelector('#theater-log').innerText.includes('我听见你说要检查便笺了，我们现在就一起看。')); const metrics = await fetch('/__smoke-metrics').then((response) => response.json()); return { href: location.href, hasTheaterRoot: !!document.querySelector('[data-theater-app]'), hasHostClose: !!(window.nekoHost && typeof window.nekoHost.closeWindow === 'function'), hasMinimize: !!(window.nekoWindowControl && typeof window.nekoWindowControl.minimize === 'function'), hasMaximize: !!(window.nekoWindowControl && typeof window.nekoWindowControl.maximize === 'function'), hasMaximizedProbe: !!(window.nekoWindowControl && typeof window.nekoWindowControl.isMaximized === 'function'), theaterMode: document.querySelector('#theater-mode-select').value, restoredLog: document.querySelector('#theater-log').innerText, sessionPointer: localStorage.getItem('neko.theater.activeSession.v1'), modePointer: localStorage.getItem('neko.theater.activeMode.v1'), startRequests: metrics.startRequests, stateRequests: metrics.stateRequests, activeRequests: metrics.activeRequests, turnRequests: metrics.turnRequests, freeStartRequests: metrics.freeStartRequests, freeStateRequests: metrics.freeStateRequests, freeActiveRequests: metrics.freeActiveRequests, freeTurnRequests: metrics.freeTurnRequests, systemStatusRequests: metrics.systemStatusRequests, chatPageRequests: metrics.chatPageRequests, lastTurnRequest: metrics.lastTurnRequest }; })()");
}

async function waitForWindowControls(win) {
  return win.webContents.executeJavaScript("(async () => { const waitFor = async (predicate) => { const deadline = Date.now() + 5000; while (Date.now() < deadline) { if (predicate()) return; await new Promise((resolve) => setTimeout(resolve, 50)); } throw new Error('window-controls-ready-timeout'); }; const button = (name) => document.querySelector('[data-neko-window-control=' + JSON.stringify(name) + ']'); await waitFor(() => ['minimize', 'maximize', 'close'].every((name) => { const item = button(name); return item && item.dataset.nekoWindowControlBound === '1'; })); return { hasRuntime: !!window.nekoWindowControls, minimizeBound: button('minimize').dataset.nekoWindowControlBound === '1', maximizeBound: button('maximize').dataset.nekoWindowControlBound === '1', closeBound: button('close').dataset.nekoWindowControlBound === '1' }; })()");
}

async function clickWindowControl(win, controlName) {
  const selector = '[data-neko-window-control="' + controlName + '"]';
  const script = "(() => { const button = document.querySelector(" + JSON.stringify(selector) + "); if (!button || button.dataset.nekoWindowControlBound !== '1') throw new Error('window-control-not-ready:' + " + JSON.stringify(controlName) + "); button.click(); return true; })()";
  return win.webContents.executeJavaScript(script);
}

async function runWindowControlStep(win, eventName, label, predicate, action) {
  let observed = false;
  win.once(eventName, () => { observed = true; });
  await action();
  await waitForWindowState(win, label, (candidate) => observed && predicate(candidate));
  return observed;
}

async function readMaximizeDisplayState(win) {
  return win.webContents.executeJavaScript("(async () => ({ bridgeMaximized: await window.nekoWindowControl.isMaximized(), documentMaximized: document.documentElement.classList.contains('neko-window-maximized'), maximizeBound: document.querySelector('[data-neko-window-control=' + JSON.stringify('maximize') + ']').dataset.nekoWindowControlBound === '1' }))()");
}

// Drive the real title-bar bindings and verify native BrowserWindow state/events, not only IPC success.
async function exerciseWindowControls(win) {
  const bindings = await waitForWindowControls(win);
  if (!bindings.hasRuntime || !bindings.minimizeBound || !bindings.maximizeBound || !bindings.closeBound) {
    throw new Error('window-controls-bindings-missing');
  }

  const minimizeEvent = await runWindowControlStep(
    win,
    'minimize',
    'native-minimize-timeout',
    (candidate) => candidate.isMinimized(),
    () => clickWindowControl(win, 'minimize'),
  );

  let restoreResult = null;
  const restoreEvent = await runWindowControlStep(
    win,
    'restore',
    'native-restore-timeout',
    (candidate) => !candidate.isMinimized(),
    async () => {
      restoreResult = await win.webContents.executeJavaScript("(async () => window.nekoWindowControl.restore())()");
    },
  );
  if (!restoreResult || !restoreResult.ok) throw new Error('native-restore-rejected');

  const initialMaximized = win.isMaximized();
  const firstMaximizeEvent = await runWindowControlStep(
    win,
    initialMaximized ? 'unmaximize' : 'maximize',
    'native-maximize-toggle-timeout',
    (candidate) => candidate.isMaximized() === !initialMaximized,
    () => clickWindowControl(win, 'maximize'),
  );
  const afterFirstToggle = await readMaximizeDisplayState(win);

  const secondMaximizeEvent = await runWindowControlStep(
    win,
    initialMaximized ? 'maximize' : 'unmaximize',
    'native-maximize-restore-timeout',
    (candidate) => candidate.isMaximized() === initialMaximized,
    () => clickWindowControl(win, 'maximize'),
  );
  const afterSecondToggle = await readMaximizeDisplayState(win);

  return {
    ...bindings,
    minimizeEvent,
    restoreEvent,
    restoreResult,
    initialMaximized,
    firstMaximizeEvent,
    secondMaximizeEvent,
    afterFirstToggle,
    afterSecondToggle,
  };
}

// Close through the real title-bar button. The closed event is the reopening gate because renderer IPC can race teardown.
async function closeInitialTheaterFromControl(win) {
  phase = 'WAIT_INITIAL_CLOSED';
  await new Promise((resolve, reject) => {
    const closeTimer = setTimeout(() => reject(new Error('theater-close-timeout')), 5000);
    win.once('closed', () => {
      clearTimeout(closeTimer);
      closeObserved = true;
      const parent = BrowserWindow.getAllWindows().find((candidate) => isPetBackendWindow(candidate));
      if (!parent || parent.isDestroyed()) {
        reject(new Error('pet-window-closed-with-theater'));
        return;
      }
      phase = 'OPEN_REOPEN';
      // Reopen only after the original child is gone, preserving the real child-window lifecycle.
      void openTheaterFromPet(parent);
      resolve();
    });
    void win.webContents.executeJavaScript("(() => { const button = document.querySelector('.theater-close-btn'); if (!button || button.dataset.nekoWindowControlBound !== '1') throw new Error('close-control-not-ready'); button.click(); return true; })()").catch((error) => {
      if (!closeObserved) {
        clearTimeout(closeTimer);
        reject(error);
      }
    });
  });
}

async function readNormalChatIsolation() {
  const chat = await waitFor(
    'normal-chat-window-timeout',
    () => BrowserWindow.getAllWindows().find((candidate) => isNormalChatWindow(candidate)),
    8000,
  );
  return chat.webContents.executeJavaScript("(async () => { const deadline = Date.now() + 5000; while (Date.now() < deadline) { if (document.querySelector('[data-aux-window]')) return { hasNormalChatRoot: true, hasTheaterRoot: !!document.querySelector('[data-theater-app]'), hasTheaterScript: Array.from(document.scripts).some((item) => item.src.includes('/static/js/theater.js')) }; await new Promise((resolve) => setTimeout(resolve, 50)); } throw new Error('normal-chat-dom-timeout'); })()");
}

// First child starts and submits; its reload restores. A separately created child must then restore again.
async function inspectTheaterWindow(win) {
  if (finished || !win || win.isDestroyed()) return;
  try {
    if (phase === 'WAIT_INITIAL_LOAD') {
      initialTheaterWindowId = win.id;
      const started = await win.webContents.executeJavaScript("(async () => { const waitFor = async (predicate, label) => { const deadline = Date.now() + 8000; while (Date.now() < deadline) { if (predicate()) return; await new Promise((resolve) => setTimeout(resolve, 50)); } throw new Error(label); }; await waitFor(() => document.querySelectorAll('#theater-story-select option').length > 0 && !document.querySelector('#theater-start-btn').disabled, 'start-ready-timeout'); document.querySelector('#theater-start-btn').click(); await waitFor(() => !document.querySelector('#theater-input').disabled && localStorage.getItem('neko.theater.activeSession.v1') === 'electron_restore_session', 'start-session-timeout'); const input = document.querySelector('#theater-input'); input.value = '请先检查折叠便笺'; document.querySelector('#theater-input-form').requestSubmit(); await waitFor(() => !input.disabled && document.querySelector('#theater-log').innerText.includes('请先检查折叠便笺') && document.querySelector('#theater-log').innerText.includes('我听见你说要检查便笺了，我们现在就一起看。'), 'submit-input-timeout'); return { submittedLog: document.querySelector('#theater-log').innerText, sessionPointer: localStorage.getItem('neko.theater.activeSession.v1'), modePointer: localStorage.getItem('neko.theater.activeMode.v1') }; })()");
      if (!started.submittedLog.includes('第一次启动的公开对白。')
          || !started.submittedLog.includes('请先检查折叠便笺')
          || !started.submittedLog.includes('我听见你说要检查便笺了，我们现在就一起看。')
          || started.sessionPointer !== 'electron_restore_session'
          || started.modePointer !== 'script') {
        finish(4, { error: 'initial-session-state-mismatch', ...started });
        return;
      }
      submittedLogBeforeReload = started.submittedLog;
      // Keep the same BrowserWindow reload check before proving close-and-reopen on a new child.
      phase = 'WAIT_RELOAD_LOAD';
      win.webContents.reload();
      return;
    }

    if (phase === 'WAIT_RELOAD_LOAD') {
      if (win.id !== initialTheaterWindowId) throw new Error('reload-created-a-different-window');
      const reloaded = await readRestoredTheaterState(win);
      metricsAfterReload = { stateRequests: reloaded.stateRequests };
      windowControlResult = await exerciseWindowControls(win);
      await closeInitialTheaterFromControl(win);
      return;
    }

    if (phase !== 'WAIT_REOPEN_LOAD') return;
    if (win.id === initialTheaterWindowId) throw new Error('reopen-reused-closed-window');
    reopenedTheaterWindowId = win.id;
    const result = await readRestoredTheaterState(win);
    result.submittedLogBeforeReload = submittedLogBeforeReload;
    result.initialTheaterWindowId = initialTheaterWindowId;
    result.reopenedTheaterWindowId = reopenedTheaterWindowId;
    result.closeObserved = closeObserved;
    result.reloadStateRequests = metricsAfterReload && metricsAfterReload.stateRequests;
    result.windowControls = windowControlResult;
    const parent = BrowserWindow.getAllWindows().find((candidate) => isPetBackendWindow(candidate));
    result.parentIsClean = !!parent && await parent.webContents.executeJavaScript("!document.querySelector('[data-theater-app]') && !Array.from(document.scripts).some((item) => item.src.includes('/static/js/theater.js'))");
    result.parentSurvivedClose = !!parent && !parent.isDestroyed();
    result.normalChat = await readNormalChatIsolation();
    const controlsPassed = result.windowControls
      && result.windowControls.minimizeEvent
      && result.windowControls.restoreEvent
      && result.windowControls.firstMaximizeEvent
      && result.windowControls.secondMaximizeEvent
      && result.windowControls.afterFirstToggle.bridgeMaximized === !result.windowControls.initialMaximized
      && result.windowControls.afterFirstToggle.documentMaximized === !result.windowControls.initialMaximized
      && result.windowControls.afterSecondToggle.bridgeMaximized === result.windowControls.initialMaximized
      && result.windowControls.afterSecondToggle.documentMaximized === result.windowControls.initialMaximized;
    const ok = result.hasTheaterRoot
      && result.hasHostClose
      && result.hasMinimize
      && result.hasMaximize
      && result.hasMaximizedProbe
      && result.theaterMode === 'script'
      && result.modePointer === 'script'
      && result.parentIsClean
      && result.parentSurvivedClose
      && result.normalChat.hasNormalChatRoot
      && !result.normalChat.hasTheaterRoot
      && !result.normalChat.hasTheaterScript
      && result.closeObserved
      && result.initialTheaterWindowId !== result.reopenedTheaterWindowId
      && controlsPassed
      && result.sessionPointer === 'electron_restore_session'
      && result.startRequests === 1
      && result.turnRequests === 1
      && result.freeStartRequests === 0
      && result.freeStateRequests === 0
      && result.freeActiveRequests === 1
      && result.freeTurnRequests === 0
      && result.systemStatusRequests >= 1
      && result.chatPageRequests >= 1
      && result.lastTurnRequest
      && result.lastTurnRequest.session_id === 'electron_restore_session'
      && result.lastTurnRequest.input_kind === 'free_input'
      && result.lastTurnRequest.message === '请先检查折叠便笺'
      && result.lastTurnRequest.base_revision === 0
      && result.lastTurnRequest.client_turn_id.startsWith('turn_web_')
      && result.stateRequests > result.reloadStateRequests;
    finish(ok ? 0 : 4, result);
  } catch (error) {
    finish(5, { error: 'inspect-theater-failed', phase, detail: String(error && error.message || error) });
  }
}

app.on('browser-window-created', (_event, win) => {
  win.webContents.on('did-finish-load', () => {
    if (isTheaterWindow(win)) {
      inspectTheaterWindow(win);
      return;
    }
    openTheaterFromPet(win);
  });
});

require(pcMainPath);

setInterval(() => {
  for (const win of BrowserWindow.getAllWindows()) {
    openTheaterFromPet(win);
  }
}, 250).unref();

setTimeout(() => {
  finish(9, {
    error: 'timeout',
    windows: BrowserWindow.getAllWindows().map((win) => {
      try { return win.webContents.getURL(); } catch (_) { return '<unreadable>'; }
    }),
  });
}, 35000).unref();
`, 'utf8');
  return wrapperPath;
}

/** Run the optional real PC main-process smoke with isolated userData and a fake backend. */
async function runMainAppSmoke() {
  const pcRoot = getPcRoot();
  const { server, baseUrl } = await startFakeBackend();
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'neko-theater-main-app-smoke-'));
  const userDataDir = path.join(tempDir, 'user-data');
  writeIsolatedPcConfig(userDataDir, baseUrl);
  const wrapperPath = writeMainAppWrapper(tempDir, pcRoot);
  try {
    return await new Promise((resolve) => {
      const child = spawn(getElectronBinary(pcRoot), [wrapperPath], {
        cwd: pcRoot,
        env: {
          ...process.env,
          NEKO_USER_DATA_DIR: userDataDir,
          NEKO_THEATER_MAIN_SMOKE_BASE_URL: baseUrl,
          NEKO_PC_MAIN_PATH: path.join(pcRoot, 'src', 'main.js'),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
      let stdout = '';
      let stderr = '';
      const timer = setTimeout(() => child.kill(), 45000);
      child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
      child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
      child.on('close', (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal, stdout, stderr });
      });
    });
  } finally {
    server.close();
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

test('Electron PC main app preserves theater state across reload, native controls, and child-window reopen', {
  skip: process.env.NEKO_RUN_ELECTRON_MAIN_SMOKE === '1'
    ? false
    : 'set NEKO_RUN_ELECTRON_MAIN_SMOKE=1 to run the PC main-app theater smoke validation',
}, async () => {
  const result = await runMainAppSmoke();
  assert.equal(result.signal, null, `electron was killed\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`);
  assert.equal(result.code, 0, `electron exited non-zero\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`);
  assert.match(result.stdout, /NEKO_THEATER_MAIN_SMOKE_RESULT/);
  assert.match(result.stdout, /"hasTheaterRoot":true/);
  assert.match(result.stdout, /"hasHostClose":true/);
  assert.match(result.stdout, /"hasMinimize":true/);
  assert.match(result.stdout, /"hasMaximize":true/);
  assert.match(result.stdout, /"hasMaximizedProbe":true/);
  // 玩家输入只提交一次；刷新和关闭后的新子窗口都恢复同一公开 Session。
  assert.match(result.stdout, /"sessionPointer":"electron_restore_session"/);
  assert.match(result.stdout, /"theaterMode":"script"/);
  assert.match(result.stdout, /"modePointer":"script"/);
  assert.match(result.stdout, /"startRequests":1/);
  assert.match(result.stdout, /"turnRequests":1/);
  assert.match(result.stdout, /"input_kind":"free_input"/);
  assert.match(result.stdout, /"message":"请先检查折叠便笺"/);
  assert.match(result.stdout, /"base_revision":0/);
  assert.match(result.stdout, /"client_turn_id":"turn_web_[^"]+"/);
  assert.match(result.stdout, /"stateRequests":[1-9][0-9]*/);
  assert.match(result.stdout, /"parentIsClean":true/);
  assert.match(result.stdout, /"parentSurvivedClose":true/);
  assert.match(result.stdout, /"closeObserved":true/);
  assert.match(result.stdout, /"minimizeEvent":true/);
  assert.match(result.stdout, /"restoreEvent":true/);
  assert.match(result.stdout, /"firstMaximizeEvent":true/);
  assert.match(result.stdout, /"secondMaximizeEvent":true/);
  // 首次空指针恢复会探测 Free Mode；后续全程必须不创建、读取或提交自由模式会话。
  assert.match(result.stdout, /"freeStartRequests":0/);
  assert.match(result.stdout, /"freeStateRequests":0/);
  assert.match(result.stdout, /"freeActiveRequests":1/);
  assert.match(result.stdout, /"freeTurnRequests":0/);
  // 真实 storage gate 拉起的普通聊天窗口保持自己的 DOM，不接入小剧场脚本。
  assert.match(result.stdout, /"chatPageRequests":[1-9][0-9]*/);
  assert.match(result.stdout, /"hasNormalChatRoot":true/);
  assert.match(result.stdout, /"hasTheaterScript":false/);
  assert.match(result.stdout, /我听见你说要检查便笺了，我们现在就一起看。/);
});

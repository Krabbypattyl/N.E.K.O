const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/app/app-interpage/bootstrap-resources-and-model-reload.js'), 'utf8');
const start = source.indexOf('    I.handleModelReload = async function');
const actualReload = source.slice(start, source.indexOf('\n    }', start) + 7).trim();
assert.ok(start > 0 && actualReload.endsWith('}'));

for (const type of ['live2d', 'vrm']) for (const roleChanges of [false, true]) {
    test(`cloud candidate ${roleChanges ? 'role takeover' : 'load failure'} retains the existing ${type}`, async () => {
        const old = {};
        let cleanups = 0, disposals = 0;
        const context = {
            I: { isMainUIHiddenByModelManager: () => false,
                cleanupLive2DOverlayUI() { cleanups++; }, cleanupVRMOverlayUI() { cleanups++; } },
            console: { log() {}, warn() {}, error() {} }, Date, JSON, Promise, setTimeout, clearTimeout,
            _isModelHostPage: () => true, activeMMDReloadCanvasSessionId: '', deferredMainUIShowAfterModelReload: null,
            lanlan_config: { lanlan_name: 'Synthetic', model_type: type }, live2dManager: { currentModel: old },
            Live2DModel: { from: async () => {
                if (!roleChanges) throw new Error('invalid_model');
                context.lanlan_config.lanlan_name = 'Other';
                return { destroy() { disposals++; } };
            } },
        };
        context.window = context;
        vm.createContext(context);
        vm.runInContext(actualReload, context);
        await assert.rejects(context.I.handleModelReload('Synthetic', {
            temporaryConfig: { model_type: 'live2d', model_path: '/user_live2d/candidate/model.model3.json' },
            suppressToast: true, throwOnError: true, preservePreviousUntilLoaded: true,
        }), roleChanges ? /model_role_changed/ : /invalid_model/);
        assert.equal(context.live2dManager.currentModel, old);
        assert.equal(context.lanlan_config.model_type, type);
        assert.equal(context._modelReloadInFlight, false);
        assert.equal(cleanups, 0);
        assert.equal(disposals, roleChanges ? 1 : 0);
    });
}

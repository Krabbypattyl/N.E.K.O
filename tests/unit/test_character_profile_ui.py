"""Execute the served profile script against disposable DOM and local API doubles."""

import json
from pathlib import Path

import pytest

from tests.node_harness import run_node_script


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "static/app/app-character-profile-sync.js"


def run_script(assertions):
    """Observe real async listeners without a browser, remote HTTP or OAuth data."""
    bootstrap = r"""
        const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
        const events={}, calls=[], alerts=[];
        function element(tag='') {
            return {tag,children:[],dataset:{},events:{},textContent:'',style:{},open:false,
                appendChild(child){this.children.push(child);child.parent=this;},
                insertBefore(child,other){this.children.splice(this.children.indexOf(other),0,child);child.parent=this;},
                setAttribute(){},addEventListener(name,callback){this.events[name]=callback;},
                querySelector(selector){return this.children.find(x=>selector==='[data-profile-preview]'&&x.dataset.profilePreview);},
                showModal(){this.open=true;},close(){if(this.open){this.open=false;if(this.events.close)this.events.close();}},
                remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}};
        }
        const body=element('body');
        global.document={body,visibilityState:'visible',createElement:element,addEventListener(n,c){events[n]=c;}};
        global.window={t(k){return k;},confirm(){return true;},alert(v){alerts.push(v);},addEventListener(){},
            setInterval(){return 1;},clearInterval(){},dispatchEvent(){}};
        global.CustomEvent=class {constructor(name){this.name=name;}};
        global.fetch=async(path,init)=>{calls.push({path,init});return {ok:true,json:async()=>response(path,init)};};
        const uid='a'.repeat(32), profile={schemaVersion:1,name:'Synthetic',nickname:'NICKNAME',systemPrompt:'RAW SETTING',
            fields:[{key:'Literal field',value:'FIELD VALUE'}],persona:{presetId:'internal_id',promptGuidance:'SPEAKING STYLE',fields:[]}};
        const candidate={local:profile,head:{...profile,name:'Cloud Name'},local_hash:'d'.repeat(64),head_revision:2,
            dirty:true,conflict:false,renameRequired:true,apply_pending:true};
        let response=()=>{throw new Error('Unexpected local API');};
        function flatten(node){return [node,...node.children.flatMap(flatten)];}
        function choice(panel,name){return flatten(panel).find(node=>node.dataset.profileChoice===name);}
        function button(panel,label){return flatten(panel).find(node=>node.tag==='button'&&node.textContent===label);}
        vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
    """
    script = (bootstrap + assertions).replace('process.argv[1]', json.dumps(str(SCRIPT)))
    result = run_node_script("node", script, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_declined_save_records_divergence_without_queueing_upload():
    run_script(r"""
        (async()=>{
            window.confirm=()=>false;
            response=(path)=>path.endsWith('/status')?{characters:[{displayName:'Synthetic',characterSyncId:uid,binding:{}}]}:
                {...candidate,head:null,conflict:false,renameRequired:false};
            await window.nekoCharacterProfileSync.afterSave('Synthetic',false);
            assert.deepEqual(calls.map(x=>x.path),['/api/chat-sync/status','/api/chat-sync/characters/'+uid+'/profile/local']);
            assert.equal(alerts.length,0);
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_unbound_save_asks_and_explains_manual_binding_without_enabling():
    run_script(r"""
        (async()=>{
            let asked=0;window.confirm=()=>{asked++;return true;};
            response=()=>({characters:[{displayName:'Synthetic',characterSyncId:uid,binding:null}]});
            await window.nekoCharacterProfileSync.afterSave('Synthetic',false);
            assert.equal(asked,1);assert.deepEqual(alerts,['chatSync.profileNeedsBinding']);
            assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/chat-sync/status');
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_approved_save_pins_saved_hash_and_cloud_revision_only():
    run_script(r"""
        (async()=>{
            response=(path)=>path.endsWith('/status')?{characters:[{displayName:'Synthetic',characterSyncId:uid,binding:{}}]}:
                {...candidate,head:null,conflict:false,renameRequired:false};
            await window.nekoCharacterProfileSync.afterSave('Synthetic',false);
            assert.equal(calls.length,3);
            assert.deepEqual(JSON.parse(calls[2].init.body),{localHash:candidate.local_hash,revision:2});
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_preview_is_readable_inert_persona_and_remote_rename_needs_confirmation():
    run_script(r"""
        (async()=>{
            await window.nekoCharacterProfileSync.review(uid,candidate);const panel=body.children[0];
            const shown=flatten(panel).map(x=>x.textContent).join('\n');
            for(const value of ['NICKNAME','RAW SETTING','FIELD VALUE','SPEAKING STYLE'])assert(shown.includes(value));
            for(const value of ['schemaVersion','internal_id','local_hash','operationId'])assert(!shown.includes(value));
            assert.equal(calls.length,0);window.confirm=()=>false;
            await choice(panel,'remote').events.click();assert.equal(calls.length,0);assert(panel.open);
            window.confirm=()=>true;response=()=>({});await choice(panel,'remote').events.click();
            assert.deepEqual(JSON.parse(calls[0].init.body),{choice:'remote',localHash:candidate.local_hash,revision:2,confirmRename:true});
            assert(!panel.open);
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_late_resolve_receipt_never_closes_a_new_dialog():
    run_script(r"""
        (async()=>{
            let release;global.fetch=async()=>{await new Promise(resolve=>release=resolve);return {ok:true,json:async()=>({})};};
            await window.nekoCharacterProfileSync.review(uid,candidate);const first=body.children[0];
            const pending=choice(first,'local').events.click();first.close();
            await window.nekoCharacterProfileSync.review(uid,candidate);const second=body.children[0];assert.notEqual(first,second);
            release();await pending;assert(second.open);assert.equal(body.children[0],second);
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_cloud_role_is_not_created_by_listing_or_preview():
    run_script(r"""
        (async()=>{
            let refreshed=0;window.loadCharacterCards=async()=>refreshed++;
            response=(path)=>path.endsWith('/character-policy')?{revision:3,selectedCharacterSyncIds:[uid],characters:[{characterSyncId:uid,displayName:'Cloud Role'}]}:
                path.endsWith('/profile')?{profile,revision:3,profileHash:'e'.repeat(64)}:{displayName:'Synthetic',existing:false};
            await window.nekoCharacterProfileSync.cloudImport();const panel=body.children[0];
            assert.equal(calls.length,1);await button(panel,'Cloud Role').events.click();assert.equal(calls.length,2);
            assert(flatten(panel).some(x=>x.textContent.includes('RAW SETTING')));
            await button(panel,'chatSync.cloudCreate').events.click();assert.equal(calls.length,3);
            assert.deepEqual(JSON.parse(calls[2].init.body),{revision:3,profileHash:'e'.repeat(64)});
            assert.equal(calls[2].path,'/api/chat-sync/cloud/characters/'+uid+'/import');
            assert.equal(refreshed,1);
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


@pytest.mark.parametrize("choice,success", [("remote", True), ("remote", False), ("local", True)])
def test_review_refreshes_cards_only_after_remote_apply_without_replacing_draft(choice, success):
    """A confirmed remote save refreshes the existing list callback, never the editor page."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # Synthetic DOM/API boundaries cannot reach user roles or the running service.
            page.route("**/*", lambda route: route.abort())
            page.set_content('<div id="chara-cards-container"><span class="card-name">Synthetic</span></div>'
                             '<div class="catgirl-panel-overlay"><textarea id="draft">Unsaved synthetic draft</textarea></div>')
            page.add_style_tag(path=str(ROOT / "static/css/index.css"))
            page.evaluate("""success => {
                window.t = key => key;
                window.confirm = () => true;
                window.refreshCount = 0;
                window.loadCharacterCards = async () => {
                    window.refreshCount++;
                    document.querySelector('.card-name').textContent = 'Cloud Name';
                };
                window.fetch = async () => ({ok: success, json: async () => success ? {} : {detail: 'profile_revision_conflict'}});
            }""", success)
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("""async () => {
                const local = {schemaVersion: 1, name: 'Synthetic', nickname: '', systemPrompt: '', fields: [], persona: null};
                await window.nekoCharacterProfileSync.review('a'.repeat(32), {local, head: {...local, name: 'Cloud Name'},
                    local_hash: 'b'.repeat(64), head_revision: 2, conflict: true, renameRequired: true});
            }""")
            page.locator(f'[data-profile-choice="{choice}"]').click()
            if success:
                page.wait_for_function("!document.querySelector('dialog[open]')")
            else:
                page.locator('dialog [role="status"]').get_by_text("chatSync.profileChanged", exact=True).wait_for()
            expected = int(success and choice == "remote")
            assert page.evaluate("window.refreshCount") == expected
            assert page.locator(".card-name").text_content() == ("Cloud Name" if expected else "Synthetic")
            assert page.locator("#draft").input_value() == "Unsaved synthetic draft"
            assert page.locator(".catgirl-panel-overlay").count() == 1
        finally:
            browser.close()


def test_all_served_entrypoints_and_eight_locales_cover_profile_labels():
    """Classic scripts are served directly; template inclusion is the runtime entry."""
    for filename in ("index.html", "chat.html", "character_card_manager.html"):
        assert "/static/app/app-character-profile-sync.js" in (ROOT / "templates" / filename).read_text(encoding="utf-8")
    locales = [json.loads((ROOT / "static/locales" / (language + ".json")).read_text(encoding="utf-8"))["chatSync"]
               for language in ("en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW")]
    assert all(locale.keys() == locales[0].keys() for locale in locales)
    for key in ("profileConflict", "profileNeedsBinding", "profileStyle", "profileName", "cloudNameConflict", "cloudCreate"):
        assert all(locale[key] for locale in locales)


@pytest.mark.parametrize('enabled,is_new', [(True, True), (True, False), (False, False)])
def test_card_save_uses_one_choice_without_a_second_profile_confirmation(enabled, is_new):
    run_script(r"""
        (async()=>{
            let asked=0;const messages=[];
            window.confirm=()=>{asked++;throw new Error('Unexpected second confirmation');};
            window.showMessage=(text,kind)=>messages.push({text,kind});
            response=path=>path.endsWith('/status')?{characters:[{displayName:'Synthetic',characterSyncId:uid}]}:{};
            await window.nekoCharacterProfileSync.afterCardSave('Synthetic',ENABLED,IS_NEW);
            assert.equal(asked,0);assert.equal(alerts.length,0);assert.equal(calls.length,2);
            assert.equal(calls[1].path,'/api/chat-sync/characters/'+uid+'/sync-choice');
            assert.deepEqual(JSON.parse(calls[1].init.body),{enabled:ENABLED,includeHistory:true});
            assert.equal(messages[0].text,'chatSync.syncChoiceSaved');
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """.replace('ENABLED', json.dumps(enabled)).replace('IS_NEW', json.dumps(is_new)))


@pytest.mark.parametrize('enabled,loading', [(False, False), (True, False), (True, True)])
def test_card_save_freezes_known_sync_choice_before_async_work(enabled, loading):
    """Late checkbox edits or preference loading cannot enlarge the clicked Save's consent."""
    run_script(r"""(async()=>{
        const source=fs.readFileSync(CARD_SOURCE,'utf8');
        vm.runInThisContext(source.slice(source.indexOf('async function saveCatgirlFromPanel('),
            source.indexOf('async function ensureCanModifyCardsOutsideVoiceMode(')));
        const toggle={checked:ENABLED,disabled:LOADING}, name={value:'Synthetic',readOnly:true};
        let releaseVoices,releaseChoice;
        const form={dataset:{},parentNode:{},_previousVoiceId:'',
            _voicesLoadPromise:new Promise(resolve=>{releaseVoices=resolve;}),
            _chatSyncLoadPromise:new Promise(resolve=>{releaseChoice=resolve;}),
            querySelector(selector){return selector.includes('chat_sync_enabled')?toggle:
                selector.includes('档案名')?name:selector.includes('voice_id')?{value:''}:null;}};
        global.ensureSafeExistingCharacterPathName=async()=>true;
        global.collectCharacterFields=()=>({data:{'档案名':'Synthetic'},fieldOrder:[]});
        global.attachCharacterFieldOrderPayload=()=>{};
        global.buildLocalCatgirlRawData=(_name,data)=>data;
        global.syncCharacterCardCache=()=>{};
        global.setLocalRawDataFieldOrder=()=>{};
        global.mergeFreshCatgirlRawDataWithLocal=(_fresh,local)=>local;
        global.buildCatgirlDetailForm=()=>{};
        global.loadCharacterData=async()=>({'猫娘':{Synthetic:{}}});
        global.loadCharacterCards=async()=>{};
        global.showMessage=window.showMessage=()=>{};
        response=path=>path.endsWith('/status')?{characters:[{displayName:'Synthetic',characterSyncId:uid}]}:{};
        const saving=saveCatgirlFromPanel(form,'Synthetic',false);
        // Resolve the original preference and edit the switch while voice loading still blocks saving.
        toggle.checked=!ENABLED;toggle.disabled=false;releaseChoice();releaseVoices();
        assert.equal(await saving,true);
        const grants=calls.filter(call=>call.path.endsWith('/sync-choice'));
        assert.equal(grants.length,LOADING?0:1);
        if(!LOADING)assert.deepEqual(JSON.parse(grants[0].init.body),{enabled:ENABLED,includeHistory:true});
    })().catch(e=>{console.error(e);process.exitCode=1;});
    """.replace('CARD_SOURCE', json.dumps(str(ROOT / 'static/js/character_card_manager/card-form-and-actions.js')))
        .replace('ENABLED', json.dumps(enabled)).replace('LOADING', json.dumps(loading)))


@pytest.mark.parametrize('is_new,binding,choice,expected', [
    (True, None, None, False), (False, None, None, False),
    (False, {'enabled': False}, False, False), (False, None, False, False),
    (False, {'enabled': False, 'bindingId': None, 'generation': 1}, True, True),
])
def test_real_card_switch_is_compact_defaults_from_saved_state_and_has_no_long_hint(is_new, binding, choice, expected):
    from playwright.sync_api import sync_playwright
    source = (ROOT / 'static/js/character_card_manager/card-form-and-actions.js').read_text(encoding="utf-8")
    start = source.index("    {\n        const row = document.createElement('div');")
    switch_code = source[start:source.index('    const btnArea', start)]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 375, 'height': 700}, reduced_motion='reduce')
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div class="settings-form-layout panel-tab-settings"><form id="test-card"></form></div>')
            page.add_style_tag(path=str(ROOT / 'static/css/character_card_manager.css'))
            page.evaluate("""role => {
                window.t=key=>key==='chatSync.newRoleSync'?'Sync character and chats (including history)':key;
                window.setInterval=()=>1;window.fetchCalls=[];
                window.fetch=async path=>{fetchCalls.push(path);return {ok:true,json:async()=>({characters:[role]})};};
            }""", {'displayName': 'Synthetic', 'characterSyncId': 'a' * 32, 'binding': binding, 'syncEnabled': choice})
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("""async ({code,isNew})=>{
                const form=document.querySelector('form');
                new Function('form','isNew','name',code)(form,isNew,'Synthetic');
                await form._chatSyncLoadPromise;
            }""", {'code': switch_code, 'isNew': is_new})
            toggle=page.get_by_role('switch')
            assert toggle.is_checked() == expected and toggle.is_enabled()
            assert page.locator('.character-chat-sync-row').inner_text().splitlines()[0] == 'Sync character and chats (including history)'
            # The existing advanced recovery entry remains available on saved cards.
            assert page.get_by_text('chatSync.moreOptions').count() == (0 if is_new else 1)
            assert page.locator('.character-chat-sync-row').bounding_box()['height'] < 80
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            toggle.focus();toggle.press('Space')
            assert toggle.is_checked() != expected
            assert page.evaluate('fetchCalls') == ([] if is_new else ['/api/chat-sync/status'])
        finally:
            browser.close()


def test_card_save_keeps_local_success_when_sync_choice_cannot_be_applied():
    run_script(r"""
        (async()=>{
            const messages=[];window.confirm=()=>{throw new Error('Unexpected confirmation');};
            window.showMessage=(text,kind)=>messages.push({text,kind});
            response=path=>path.endsWith('/status')?{characters:[{displayName:'Synthetic',characterSyncId:uid}]}:
                (()=>{throw new Error('synthetic failure');})();
            await window.nekoCharacterProfileSync.afterCardSave('Synthetic',true,false);
            assert.equal(calls.length,2);
            assert.equal(messages[0].kind,'warning');assert.equal(alerts.length,0);
        })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_card_save_before_choice_load_preserves_previous_consent_without_warning():
    """A pending status read is not a failed sync save or new consent."""
    run_script(r"""(async()=>{
        const messages=[];window.showMessage=(text,kind)=>messages.push({text,kind});
        await window.nekoCharacterProfileSync.afterCardSave('Synthetic',undefined,false);
        assert.deepEqual(calls,[]);assert.deepEqual(messages,[]);assert.deepEqual(alerts,[]);
    })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_card_choice_uses_effective_pending_consent_while_binding_is_still_paused():
    """A durable resume choice is visible before an offline binding can activate."""
    run_script(r"""(async()=>{
        response=()=>({characters:[{displayName:'Synthetic',characterSyncId:uid,
            syncEnabled:true,syncPending:true,binding:{enabled:false,bindingId:'binding',generation:2}}]});
        assert.equal(await window.nekoCharacterProfileSync.getSyncChoice('Synthetic'),true);
    })().catch(e=>{console.error(e);process.exitCode=1;});
    """)


def test_sync_navigation_copy_names_the_existing_more_settings_button_in_every_locale():
    """Recovery instructions point to the actual localized button label."""
    for language in ('en', 'es', 'ja', 'ko', 'pt', 'ru', 'zh-CN', 'zh-TW'):
        locale = json.loads((ROOT / 'static/locales' / (language + '.json')).read_text(encoding="utf-8"))['chatSync']
        for key in ('setupNeedsSave', 'initialHistoryFailed', 'profileNeedsBinding'):
            assert locale['moreOptions'].casefold() in locale[key].casefold(), (language, key)


def test_character_quota_setup_failure_is_visible_once_without_rejecting_local_card():
    """An asynchronous cloud registration failure explains the limit and retains local use."""
    run_script(r"""(async()=>{
        const messages=[];window.showMessage=(text,kind)=>messages.push({text,kind});
        response=()=>({characters:[{displayName:'Synthetic',characterSyncId:uid,
            syncSetupError:'character_quota_exceeded',binding:null}]});
        await window.nekoCharacterProfileSync.poll();await window.nekoCharacterProfileSync.poll();
        assert.deepEqual(messages,[{text:'chatSync.characterQuotaExceeded',kind:'warning'}]);
        assert.equal(calls.filter(call=>call.init.method==='POST').length,0);
    })().catch(e=>{console.error(e);process.exitCode=1;});
    """)
    for language in ('en', 'es', 'ja', 'ko', 'pt', 'ru', 'zh-CN', 'zh-TW'):
        locale = json.loads((ROOT / 'static/locales' / (language + '.json')).read_text(encoding="utf-8"))['chatSync']
        assert '3' in locale['characterQuotaExceeded']


@pytest.mark.parametrize('outcome', ['cancel', 'success', 'stale', 'offline', 'pending'])
def test_cloud_selection_dialog_uses_actual_dom_and_freezes_exact_deletion_scope(outcome):
    """The served dialog lists destructive scope, enforces three, and retains failed drafts."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div id="local-card">Local synthetic card</div><textarea id="draft">Unsaved draft</textarea>')
            page.add_style_tag(path=str(ROOT / 'static/css/index.css'))
            translations = json.loads((ROOT / 'static/locales/en.json').read_text(encoding="utf-8"))['chatSync']
            page.on('dialog', lambda dialog: dialog.accept())
            page.evaluate("""({translations,outcome}) => {
                window.t = key => translations[key.replace('chatSync.','')] || key;
                window.calls = []; window.refreshCount = 0;
                window.loadCharacterCards = async () => { window.refreshCount++; };
                window.showMessage = () => {};
                const roles = [1,2,3,4].map(n => ({characterSyncId: n.toString(16).repeat(32), displayName: 'Role ' + n}));
                const snapshot = {ownerId:'00000000-0000-4000-8000-000000000001',revision: 7, characterCount: 4, requiresSelection: true, selectedCharacterSyncIds: [], characters: roles};
                if (outcome === 'pending') {
                    Object.assign(snapshot, {revision:9, characterCount:3, requiresSelection:false, selectedCharacterSyncIds:roles.slice(0,3).map(role=>role.characterSyncId),
                        characters:roles.slice(0,3),pendingSelection:{baseRevision:7,selectedCharacterSyncIds:roles.slice(0,3).map(role=>role.characterSyncId),characters:roles}});
                }
                window.fetch = async (path,init) => {
                    window.calls.push({path,method:init.method,body:init.body ? JSON.parse(init.body) : null});
                    if (path.endsWith('/status')) return {ok:true,json:async()=>({characters:[],characterPolicy:snapshot})};
                    if (init.method === 'GET') return {ok:true,json:async()=>snapshot};
                    if (outcome === 'offline') throw new Error('Synthetic transport failure');
                    return {ok:['success','pending'].includes(outcome),json:async()=>['success','pending'].includes(outcome)?{}:{detail:'character_policy_changed'}};
                };
            }""", {'translations': translations, 'outcome': outcome})
            page.add_script_tag(path=str(SCRIPT))
            # A background local status poll actively opens the over-limit choice.
            page.evaluate('window.nekoCharacterProfileSync.' + ('cloudImport()' if outcome == 'pending' else 'poll()'))
            panel = page.locator('dialog[open]')
            assert panel.count() == 1
            assert 'permanently deleted' in panel.inner_text()
            assert 'Local copies remain available' in panel.inner_text()
            checkboxes = panel.locator('input[type="checkbox"]')
            if outcome != 'pending':
                for index in range(3):
                    checkboxes.nth(index).check()
            assert checkboxes.nth(3).is_disabled()
            assert panel.locator('[data-cloud-deletion-list] li').all_text_contents() == ['Role 4']
            if outcome == 'cancel':
                panel.get_by_role('button', name='Cancel', exact=True).click()
                assert page.evaluate("window.calls.filter(call=>call.method==='POST').length") == 0
                assert page.locator('dialog[open]').count() == 0
            else:
                panel.locator('[data-cloud-confirm-selection]').click()
                page.wait_for_function("window.calls.some(call=>call.method==='POST')")
                approved = page.evaluate("window.calls.find(call=>call.method==='POST')")
                assert approved['body'] == {'baseRevision': 7, 'selectedCharacterSyncIds': [str(n) * 32 for n in (1, 2, 3)], 'expectedOwner': '00000000-0000-4000-8000-000000000001'}
                assert approved['path'] == '/api/chat-sync/cloud/character-policy'
                if outcome in ('success', 'pending'):
                    page.wait_for_function('window.refreshCount===1')
                    assert page.locator('dialog[open]').count() == 0
                else:
                    page.wait_for_function("!document.querySelector('dialog [role=status]').textContent.includes('Working')")
                    assert panel.count() == 1
                    assert all(checkboxes.nth(index).is_disabled() for index in range(4))
                    if outcome == 'stale':
                        assert panel.get_by_role('button', name='Reload cloud list').is_visible()
                        assert panel.locator('[data-cloud-confirm-selection]').is_disabled()
                    else:
                        panel.locator('[data-cloud-confirm-selection]').click()
                        requests = page.evaluate("window.calls.filter(call=>call.method==='POST')")
                        assert len(requests) == 2 and requests[0]['body'] == requests[1]['body']
                    assert page.evaluate('window.refreshCount') == 0
            assert page.locator('#local-card').inner_text() == 'Local synthetic card'
            assert page.locator('#draft').input_value() == 'Unsaved draft'
        finally:
            browser.close()


@pytest.mark.parametrize('succeeded', [True, False])
def test_cloud_individual_delete_requires_explicit_confirmation_and_preserves_local_ui(succeeded):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div id="local-card">Local synthetic card</div>')
            page.evaluate("""succeeded => {
                window.calls=[];window.refreshCount=0;window.confirmations=[];window.accept=false;
                window.t=(key,values={})=>key+' '+(values.name||'');
                window.confirm=message=>{window.confirmations.push(message);return window.accept;};
                window.showMessage=()=>{};
                window.loadCharacterCards=async()=>{window.refreshCount++;};
                window.fetch=async(path,init)=>{
                    window.calls.push({path,method:init.method,body:init.body?JSON.parse(init.body):null});
                    if(init.method==='GET')return {ok:true,json:async()=>({ownerId:'00000000-0000-4000-8000-000000000001',revision:9,requiresSelection:false,
                        selectedCharacterSyncIds:['a'.repeat(32)],characters:[{characterSyncId:'a'.repeat(32),displayName:'Synthetic Cloud'}]})};
                    return {ok:succeeded,json:async()=>succeeded?{}:{detail:'character_policy_changed'}};
                };
            }""", succeeded)
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate('window.nekoCharacterProfileSync.cloudImport()')
            remove = page.locator('[data-cloud-delete]')
            remove.click()
            assert page.evaluate("window.calls.filter(call=>call.method==='POST').length") == 0
            assert 'Synthetic Cloud' in page.evaluate('window.confirmations[0]')
            page.evaluate('window.accept=true')
            remove.click()
            page.wait_for_function("window.calls.some(call=>call.method==='POST')")
            posted = page.evaluate("window.calls.find(call=>call.method==='POST')")
            assert posted == {'path': '/api/chat-sync/cloud/characters/' + 'a' * 32 + '/delete', 'method': 'POST', 'body': {'baseRevision': 9, 'expectedOwner': '00000000-0000-4000-8000-000000000001'}}
            if succeeded:
                page.wait_for_function('window.refreshCount===1')
                assert page.locator('dialog[open]').count() == 0
            else:
                page.get_by_text('chatSync.cloudPolicyChanged ', exact=True).wait_for()
                assert page.locator('dialog[open]').count() == 1
                assert page.evaluate('window.refreshCount') == 0
            assert page.locator('#local-card').inner_text() == 'Local synthetic card'
        finally:
            browser.close()


@pytest.mark.parametrize('rejection', ['character_policy_changed', 'chat_sync_account_mismatch'])
def test_cloud_reload_waits_for_native_dialog_close_before_reopening(rejection):
    """A native asynchronous close event releases the dialog before cloud discovery."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div id="local-card">Local synthetic card</div><textarea id="draft">Unsaved draft</textarea>')
            page.add_style_tag(path=str(ROOT / 'static/css/index.css'))
            page.evaluate("""rejection => {
                window.timeline=[];window.gets=0;window.posts=0;
                window.t=key=>key;window.confirm=()=>true;
                window.fetch=async(path,init)=>{
                    if(init.method==='POST'){
                        window.posts++;
                        return {ok:false,json:async()=>({detail:rejection})};
                    }
                    window.timeline.push('get');window.gets++;
                    return {ok:true,json:async()=>({ownerId:'00000000-0000-4000-8000-000000000001',
                        revision:window.gets,requiresSelection:false,selectedCharacterSyncIds:['a'.repeat(32)],
                        characters:[{characterSyncId:'a'.repeat(32),displayName:'Cloud role '+window.gets}]})};
                };
            }""", rejection)
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate('window.nekoCharacterProfileSync.cloudImport()')
            page.evaluate("""() => {
                window.originalDialog=document.querySelector('dialog');
                window.originalDialog.addEventListener('close',()=>window.timeline.push('close'));
            }""")
            page.locator('[data-cloud-delete]').click()
            reload = page.get_by_role('button', name='chatSync.cloudReload', exact=True)
            reload.wait_for()
            reload.click()
            page.wait_for_function('window.gets===2', timeout=1500)
            assert page.locator('dialog[open]').count() == 1
            assert page.get_by_role('button', name='Cloud role 2', exact=True).is_visible()
            assert page.evaluate('window.timeline') == ['get', 'close', 'get']
            assert page.evaluate('document.body.contains(window.originalDialog)') is False
            assert page.evaluate('window.posts') == 1
            assert page.locator('#local-card').inner_text() == 'Local synthetic card'
            assert page.locator('#draft').input_value() == 'Unsaved draft'
        finally:
            browser.close()


@pytest.mark.parametrize('selected_count', [0, 2])
@pytest.mark.parametrize('accepted', [False, True])
def test_cloud_selection_requires_native_confirmation_for_exact_deletion_scope(selected_count, accepted):
    """An empty selection may delete all, but only after a second native confirmation."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<div id="local-card">Local synthetic card</div><textarea id="draft">Unsaved draft</textarea>')
            page.add_style_tag(path=str(ROOT / 'static/css/index.css'))
            translations = json.loads((ROOT / 'static/locales/en.json').read_text(encoding='utf-8'))['chatSync']
            page.evaluate("""translations => {
                window.calls=[];window.refreshCount=0;
                window.t=(key,values={})=>(translations[key.replace('chatSync.','')]||key)
                    .split('{{count}}').join(values.count??'').split('{{names}}').join(values.names??'');
                window.loadCharacterCards=async()=>{window.refreshCount++;};window.showMessage=()=>{};
                window.snapshot={ownerId:'00000000-0000-4000-8000-000000000001',revision:7,requiresSelection:true,
                    selectedCharacterSyncIds:[],characters:[1,2,3,4].map(n=>({characterSyncId:String(n).repeat(32),displayName:'Role '+n}))};
                window.fetch=async(path,init)=>{
                    window.calls.push({path,method:init.method,body:init.body?JSON.parse(init.body):null});
                    if(init.method==='GET')return {ok:true,json:async()=>window.snapshot};
                    return await new Promise(resolve=>{window.finishPost=()=>resolve({ok:true,json:async()=>({})});});
                };
            }""", translations)
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate('window.nekoCharacterProfileSync.cloudImport()')
            panel = page.locator('dialog[open]')
            for index in range(selected_count):
                panel.locator('input[type="checkbox"]').nth(index).check()
            deleted_names = [f'Role {index}' for index in range(selected_count + 1, 5)]
            assert panel.locator('[data-cloud-deletion-list] li').all_text_contents() == deleted_names
            confirmations = []

            def confirm(dialog):
                confirmations.append(dialog.message)
                if accepted:
                    dialog.accept()
                else:
                    dialog.dismiss()

            page.on('dialog', confirm)
            panel.locator('[data-cloud-confirm-selection]').click()
            assert len(confirmations) == 1
            prompt = confirmations[0]
            assert f'{4-selected_count} cloud characters' in prompt
            assert 'profiles and all cloud chats' in prompt
            assert 'permanently' in prompt.lower() and 'Both devices keep' in prompt and 'sync off' in prompt
            assert prompt.endswith('\n'.join(deleted_names))
            for index in range(1, selected_count + 1):
                assert f'Role {index}' not in prompt
            if selected_count == 0:
                assert 'ALL 4 cloud characters' in prompt
            posts = page.evaluate("window.calls.filter(call=>call.method==='POST')")
            if accepted:
                assert len(posts) == 1
                assert posts[0] == {'path': '/api/chat-sync/cloud/character-policy', 'method': 'POST', 'body': {
                    'baseRevision': 7, 'selectedCharacterSyncIds': [str(index) * 32 for index in range(1, selected_count + 1)],
                    'expectedOwner': '00000000-0000-4000-8000-000000000001'}}
                # A later account/list update cannot expand the already reviewed request.
                page.evaluate("""() => {
                    window.snapshot.ownerId='00000000-0000-4000-8000-000000000002';window.snapshot.revision=99;
                    window.snapshot.characters.push({characterSyncId:'f'.repeat(32),displayName:'Later role'});
                    window.finishPost();
                }""")
                page.wait_for_function('window.refreshCount===1')
                assert page.locator('dialog[open]').count() == 0
                assert len(page.evaluate("window.calls.filter(call=>call.method==='POST')")) == 1
            else:
                assert posts == []
                assert panel.count() == 1
                assert panel.locator('[data-cloud-deletion-list] li').all_text_contents() == deleted_names
            assert page.locator('#local-card').inner_text() == 'Local synthetic card'
            assert page.locator('#draft').input_value() == 'Unsaved draft'
        finally:
            browser.close()

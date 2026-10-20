"""Execute the served classic script to check identity-only passive restoration."""

import json
from pathlib import Path
import re

import pytest

from tests.node_harness import run_node_script


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "static/app/app-chat-sync.js"


def run_script(assertions: str) -> None:
    """Run the actual served JavaScript with isolated DOM/host boundaries."""
    bootstrap = r"""
        const fs = require('node:fs');
        const vm = require('node:vm');
        const assert = require('node:assert/strict');
        const documentEvents = {}, windowEvents = {}, intervals = [];
        function element() {
            return {children:[],events:{},dataset:{},open:false, setAttribute(){},
                appendChild(child){this.children.push(child);}, addEventListener(name,callback){this.events[name]=callback;},
                replaceChildren(){this.children=[];},
                querySelectorAll(selector){const all=this.children.flatMap(child=>[child,...child.querySelectorAll('*')]);
                    if(selector==='*')return all;
                    return all.filter(child=>selector==='[data-sync-action]'?!!child.dataset.syncAction:
                        selector==='[data-history-action]'?!!child.dataset.historyAction:
                        selector.includes('"'+(child.dataset.syncAction||child.dataset.historyAction)+'"'));},
                querySelector(selector){return this.querySelectorAll(selector)[0];},
                showModal(){this.open=true;},close(){this.open=false;}};
        }
        const headerButton=element(), body=element();
        global.window = { addEventListener(name,callback) {windowEvents[name]=callback;}, confirm(){return true;},
            setInterval(callback,delay){intervals.push({callback,delay});return intervals.length;},clearInterval(){},
            lanlan_config: { lanlan_name: 'Neko' }, t(key,values) { return values && values.name ? key+':'+values.name
                :values && typeof values.count==='number'?key+':'+values.count:key; } };
        global.document = { addEventListener(name,callback) {documentEvents[name]=callback;},
            getElementById(){return headerButton;},createElement:element,body };
        global.fetch = () => { throw new Error('Unexpected network from pure restoration'); };
        vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
        const id = 'a'.repeat(32), other = 'b'.repeat(32);
        const original = {messageId:id,characterSyncId:'c'.repeat(32),role:'user',text:'same',
            originalTime:'1791540000123',originStreamId:'d'.repeat(32),originSeq:1};
    """
    script = (bootstrap + assertions).replace('process.argv[1]', json.dumps(str(SCRIPT)))
    result = run_node_script("node", script, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('code,key', [
    ('chat_sync_account_mismatch', 'errorAccountMismatch'),
    ('chat_sync_writer_recovery_required', 'errorWriterRecovery'),
    ('chat_sync_unavailable', 'errorNetwork'),
    ('chat_sync_retry_later', 'errorRetryLater'),
    ('chat_sync_session_changed', 'errorSessionChanged'),
    ('private token from an unknown error', 'errorUnknown'),
])
def test_recovery_failure_explains_the_actual_error_and_next_step(code, key):
    """The actual recovery listener uses localized reasons without leaking arbitrary details."""
    labels = json.loads((ROOT / 'static/locales/en.json').read_text(encoding='utf-8'))['chatSync']
    run_script(r"""(async()=>{
        const labels=LABELS,code=CODE,key=KEY;
        window.t=(key,values={})=>labels[key.replace('chatSync.','')]
            .replace(/{{(.*?)}}/g,(_,name)=>values[name]);
        global.fetch=async(path)=>({ok:path.endsWith('/status'),json:async()=>path.endsWith('/status')
            ?{writerRecoveryRequired:true,characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32)}]}
            :{detail:code}});
        await window.nekoChatSync.openSettings();
        const modal=body.children[0],recover=modal.querySelector('[data-sync-action="recover"]');
        await recover.events.click();
        const shown=modal.querySelectorAll('*').find(node=>node.className==='chat-sync-status').textContent;
        assert(shown.includes(labels[key]));
        if(key==='errorUnknown')assert(!shown.includes(code));
        else assert(shown.includes(code));
        assert.notEqual(shown,labels.failed);
        assert.equal(recover.disabled,false);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace('LABELS', json.dumps(labels)).replace('CODE', json.dumps(code)).replace('KEY', json.dumps(key)))


@pytest.mark.parametrize('source', ['setup', 'binding'])
def test_opening_sync_settings_shows_the_retained_failure_without_retrying(source):
    """A local status read keeps setup and channel errors visible across dialog openings."""
    run_script(r"""(async()=>{
        const requests=[];
        window.t=(key,values={})=>key+JSON.stringify(values);
        global.fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>({characters:[{
            displayName:'Neko',characterSyncId:'c'.repeat(32),
            syncSetupError:SOURCE==='setup'?'chat_sync_account_mismatch':null,
            binding:{writerRecoveryRequired:true,lastSyncError:SOURCE==='binding'?'chat_sync_account_mismatch':null}
        }]})};};
        await window.nekoChatSync.openSettings();
        const shown=body.children[0].querySelectorAll('*').find(node=>node.className==='chat-sync-status').textContent;
        assert(shown.includes('errorAccountMismatch')&&shown.includes('chat_sync_account_mismatch'));
        assert.deepEqual(requests,['/api/chat-sync/status']);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace('SOURCE', json.dumps(source)))


def test_sync_settings_reports_pending_and_local_only_work():
    run_script(r"""(async()=>{
        window.t=(key,values={})=>key+JSON.stringify(values);
        global.fetch=async()=>({ok:true,json:async()=>({characters:[{
            displayName:'Neko',characterSyncId:original.characterSyncId,binding:{pendingOriginals:3,
                pendingDeletions:2,localOnlyOriginals:1,historyBackfill:{awaitingRegistration:4}}
        }]})});
        await window.nekoChatSync.openSettings();
        const shown=body.children[0].querySelectorAll('*').find(node=>node.className==='chat-sync-status').textContent;
        assert(shown.includes('chatSync.pending')&&shown.includes('"originals":3')&&shown.includes('"deletions":2'));
        assert(shown.includes('chatSync.oversizedLocalOnly')&&shown.includes('"count":1'));
        assert(shown.includes('chatSync.historyAwaitingRegistration')&&shown.includes('"count":4'));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_legacy_history_with_unknown_order_can_merge():
    run_script(r"""
        const records=[{...original,originalTime:null,originStreamId:null,originSeq:null},
            {...original,messageId:other,originalTime:null,originStreamId:null,originSeq:null}];
        const merged=window.nekoChatSync.mergeHistory([],records,new Set(),new Map(),'Neko');
        assert.equal(merged.length,2);
    """)


@pytest.mark.parametrize("broken_observer", [False, True])
def test_optional_trace_observer_never_changes_passive_history(broken_observer):
    run_script(r"""(async()=>{
        const observed=[], merged=[];
        window.__nekoChatSyncTrace=(event,metadata)=>{
            observed.push({event,metadata});
            if(BROKEN)throw new Error('observer unavailable');
        };
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:items=>merged.push(items)};
        global.fetch=async()=>({ok:true,json:async()=>({originals:[original]})});
        await window.nekoChatSync.loadHistory({characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor:1}}]});
        assert.equal(merged.length,1);
        assert.deepEqual(observed,[{event:'ui_merge_requested',metadata:{character_uid:original.characterSyncId,
            message_id:id,local_message_id:'sync-original-'+id}}]);
        assert.equal(JSON.stringify(observed).includes('same'),false);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("BROKEN", json.dumps(broken_observer)))


@pytest.mark.parametrize("busy", ["none", "turn", "speech", "queue", "unknown", "hidden"])
def test_committed_history_hint_refreshes_now_or_after_real_idle(busy):
    """A committed local hint uses passive reads; busy/hidden states retain it for retry."""
    run_script(r"""(async()=>{
        const requests=[], merged=[];
        window.reactChatWindowHost={getState:()=>({messages:BUSY==='unknown'?[{id:'live',role:'assistant',status:'streaming'}]:[]}),
            setMessages:messages=>merged.push(messages)};
        window.appState=BUSY==='turn'||BUSY==='speech'?{assistantTurnId:'turn',assistantTurnAwaitingBubble:true}:{};
        window._isProcessingRealisticQueue=BUSY==='queue';
        document.visibilityState=BUSY==='hidden'?'hidden':'visible';
        global.fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>path.endsWith('/status')
            ?{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor:1}}]}
            :{originals:[original]}};};
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        if(BUSY==='none'){assert.equal(merged.length,1);assert.equal(requests.length,2);return;}
        assert.equal(merged.length,0);assert.equal(requests.length,0);
        if(BUSY==='turn'||BUSY==='speech'){
            const eventName=BUSY==='turn'?'neko-assistant-turn-end':'neko-assistant-speech-end';
            windowEvents[eventName]({detail:{turnId:'turn'}});
            window.appState.assistantTurnAwaitingBubble=false;
            window.appState[BUSY==='turn'?'assistantTurnCompletedId':'assistantTurnSettledId']='turn';
        }else if(BUSY==='queue'){
            window._isProcessingRealisticQueue=false;
            windowEvents['neko-realistic-queue-idle']({});
        }else if(BUSY==='hidden'){
            document.visibilityState='visible';await documentEvents.visibilitychange();
        }else{
            windowEvents['neko-assistant-turn-end']({detail:{turnId:'other-turn'}});
        }
        for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));
        assert.equal(merged.length,BUSY==='unknown'?0:1);
        assert.ok(requests.every(path=>!path.endsWith('/sync')));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("BUSY", json.dumps(busy)))


def test_local_history_hints_reject_foreign_uid_and_coalesce_without_losing_late_commit():
    """A second commit during a local read gets one follow-up, without polling or cloud work."""
    run_script(r"""(async()=>{
        let cursor=1, release, waiting=false;
        const requests=[], merged=[];
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:messages=>merged.push(messages)};
        global.fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>{
            if(path.endsWith('/status'))return{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor}}]};
            if(!waiting){waiting=true;await new Promise(resolve=>{release=resolve;});}return{originals:[original]};
        }};};
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:other});
        assert.equal(merged.length,0);
        requests.length=0;
        const first=window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        for(let i=0;i<10&&!release;i++)await new Promise(resolve=>setImmediate(resolve));
        cursor=2;
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        release();await first;
        assert.equal(merged.length,2);
        assert.equal(requests.filter(path=>path.endsWith('/history')).length,2);
        assert.ok(requests.every(path=>path.endsWith('/history')||path.endsWith('/status')));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_foreign_hint_cannot_replace_a_current_role_hint_deferred_by_busy():
    """An old role's socket hint cannot strand the active role until fallback polling."""
    run_script(r"""(async()=>{
        let cursor=0;const merged=[];
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:messages=>merged.push(messages)};
        global.fetch=async(path)=>({ok:true,json:async()=>path.endsWith('/status')
            ?{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor}}]}
            :{originals:[original]}});
        await window.nekoChatSync.loadHistory();merged.length=0;cursor=1;
        window.appState={assistantTurnId:'turn',assistantTurnAwaitingBubble:true};
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:other});
        windowEvents['neko-assistant-turn-end']({detail:{turnId:'turn'}});
        window.appState.assistantTurnAwaitingBubble=false;window.appState.assistantTurnCompletedId='turn';
        for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));
        assert.equal(merged.length,1);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_hint_hidden_during_history_io_keeps_cursor_for_foreground_retry():
    """A hidden window must not consume a committed cursor while its read is in flight."""
    run_script(r"""(async()=>{
        let hide=true;const merged=[];
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:messages=>merged.push(messages)};
        global.fetch=async(path)=>({ok:true,json:async()=>{
            if(path.endsWith('/status'))return{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor:1}}]};
            if(path.endsWith('/history')){if(hide)document.visibilityState='hidden';return{originals:[original]};}
            return{};
        }});
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        assert.equal(merged.length,0);
        hide=false;document.visibilityState='visible';await documentEvents.visibilitychange();
        assert.equal(merged.length,1);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


@pytest.mark.parametrize("script", ["app-chat-adapter.js", "app-chat.js"])
def test_realistic_queue_idle_event_only_after_current_owner_finishes(script):
    """Execute the actual queue function; a replaced owner cannot announce idle."""
    from tests.unit.test_app_websocket_static_routes import _block_after

    source = (ROOT / "static/app" / script).read_text(encoding="utf-8")
    body = _block_after(source, "async function processRealisticQueue(queueVersion) {")
    code = r"""
        global.CustomEvent=function(type){this.type=type;};
        const events=[];window.dispatchEvent=event=>events.push(event.type);
        window._realisticGeminiQueue=[];
        function createRealisticQueueOwnerToken(){return 'owner';}
        function getRealisticQueueItemText(item){return item;}
        function getRealisticQueueItemTurnId(){return 'turn';}
        const chatContainer={parentElement:null}, S={dom:{chatContainer}};
        function createGeminiBubble(){window._realisticProcessingOwner='replacement';}
        async function processRealisticQueue(queueVersion){BODY}
        (async()=>{
            await processRealisticQueue(1);
            assert.deepEqual(events,['neko-realistic-queue-idle']);
            events.length=0;window._realisticGeminiVersion=1;window._realisticGeminiQueue=['synthetic'];
            await processRealisticQueue(1);
            assert.equal(window._realisticProcessingOwner,'replacement');
            assert.deepEqual(events,[]);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """
    run_script(code.replace("BODY", body))


def test_current_local_socket_dispatches_history_hint_and_stale_socket_is_ignored():
    """Execute the actual onmessage handler, preserving its socket replacement fence."""
    from tests.unit.test_app_websocket_static_routes import _block_after

    source = (ROOT / "static/app/app-websocket.js").read_text(encoding="utf-8")
    body = _block_after(source, "S.socket.onmessage = function (event) {")
    run_script(r"""
        const calls=[], socket={}, S={socket}, _thisSocket=socket;
        window.nekoChatSync.notifyHistoryCommitted=payload=>{calls.push(payload);return Promise.resolve();};
        function onmessage(event){BODY}
        const hint={type:'chat_sync_history_changed',characterSyncId:original.characterSyncId};
        onmessage({data:JSON.stringify(hint)});
        assert.deepEqual(calls,[hint]);
        S.socket={};onmessage({data:JSON.stringify(hint)});
        assert.deepEqual(calls,[hint]);
    """.replace("BODY", body))


def test_pc_pet_ipc_bridge_delivers_committed_hint_to_full_chat_passive_refresh():
    """Use existing PC bridges/router, including both chat targets and stale-host filtering."""
    from tests.unit.test_app_websocket_static_routes import _block_after

    pc = ROOT.parent / "N.E.K.O.-PC"
    harness = pc / "test/preload-bridge-lifecycle.test.js"
    if not harness.is_file():
        pytest.skip("optional NEKOPC checkout is unavailable")
    source = harness.read_text(encoding="utf-8")
    helpers = "\n".join(
        opener + _block_after(source, opener) + "}"
        for opener in ("function createWindowHarness() {", "function createPetBridgeHarness() {")
    )
    callback = _block_after((ROOT / "static/app/app-websocket.js").read_text(encoding="utf-8"), "S.socket.onmessage = function (event) {")
    run_script(r"""
        const {EventEmitter}=require('node:events'), Module=require('node:module'), pc=PC_ROOT;
        const {createPreloadLifecycle}=require(pc+'/src/preload/shared/lifecycle');
        const {installPetWebSocketBridge}=require(pc+'/src/preload/bridges/pet-websocket-bridge');
        const {installChatWebSocketBridge}=require(pc+'/src/preload/bridges/chat-websocket-bridge');
        const channels=require(pc+'/src/ipc-channels'), ipcMain=new EventEmitter();
        const originalLoad=Module._load;
        let router;
        try{
            Module._load=function(name){return name==='electron'?{ipcMain}:originalLoad.apply(this,arguments);};
            router=require(pc+'/src/ipc-router');
        }finally{Module._load=originalLoad;}
        HELPERS
        global.CloseEvent=class CloseEvent extends Event{constructor(type,options){super(type);Object.assign(this,options);}};
        const pet=createPetBridgeHarness(), clients=[];
        function client(){
            const page=createWindowHarness(), ipc=new EventEmitter();ipc.send=()=>{};page.WebSocket=pet.RealWebSocket;
            const lifecycle=createPreloadLifecycle(page);
            installChatWebSocketBridge({lifecycle,window:page,ipcRenderer:ipc,WS_PROXY_CHANNELS:channels.WS_PROXY_CHANNELS});
            const proxy=new page.WebSocket('ws://synthetic.test');
            const webContents=new EventEmitter();webContents.isDestroyed=()=>false;
            webContents.send=(name,data)=>ipc.emit(name,{},data);
            const native={isDestroyed:()=>false,isVisible:()=>true,webContents};
            clients.push({lifecycle,proxy});return{proxy,native};
        }
        const compact=client(), full=client(), petContents=new EventEmitter();petContents.isDestroyed=()=>false;
        const windows={pet:{isDestroyed:()=>false,webContents:petContents},chat:compact.native,fullChat:full.native};
        router.setupIPCRouter({getWindows:()=>windows,log:()=>{}});
        pet.ipcRenderer.send=(name,data)=>ipcMain.emit(name,{sender:petContents},data);
        installPetWebSocketBridge({lifecycle:pet.lifecycle,window:pet.window,ipcRenderer:pet.ipcRenderer,channels});
        const S={socket:full.proxy}, _thisSocket=full.proxy, mirrored=[], merged=[];
        full.proxy.onmessage=function(event){CALLBACK};
        compact.proxy.onmessage=event=>mirrored.push(JSON.parse(event.data));
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:messages=>merged.push(messages)};
        global.fetch=async(path)=>({ok:true,json:async()=>path.endsWith('/status')
            ?{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor:1}}]}
            :{originals:[original]}});
        (async()=>{
            const socket=new pet.window.WebSocket('ws://synthetic.test');
            socket.readyState=pet.RealWebSocket.OPEN;socket.emit('open');
            const hint={type:'chat_sync_history_changed',characterSyncId:original.characterSyncId};
            socket.emit('message',{data:JSON.stringify(hint)});
            for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));
            assert.deepEqual(mirrored,[hint]);assert.equal(merged.length,1);
            new pet.window.WebSocket('ws://synthetic.test');
            socket.emit('message',{data:JSON.stringify(hint)});
            assert.equal(mirrored.length,1);
            full.proxy.close();pet.lifecycle.dispose();clients.forEach(item=>item.lifecycle.dispose());
            assert.equal(full.proxy.readyState,pet.RealWebSocket.CLOSED);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("PC_ROOT", json.dumps(str(pc))).replace("HELPERS", helpers).replace("CALLBACK", callback))


def test_recovery_uses_builtin_service_instead_of_legacy_saved_origin():
    """Old local test setup must not make a user's recovery request fail API validation."""
    run_script(r"""(async()=>{
        const uid='c'.repeat(32),requests=[];let needsRecovery=true;
        global.fetch=async(path,options)=>({ok:true,async json(){
            requests.push({path,body:options.body});
            if(path.endsWith('/status'))return {serviceUrl:'https://localhost:8443',
                writerRecoveryRequired:needsRecovery,
                characters:[{displayName:'Neko',characterSyncId:uid,binding:null}]};
            if(path.endsWith('/recovery/preview')){
                needsRecovery=false;
                return {history:{taskId:'f'.repeat(32),characterSyncId:uid,localName:'Neko',
                    cloudName:'Cloud Neko',count:0,unverifiedCount:null,status:'preview'},deletions:{count:0}};
            }
            if(path.endsWith('/history'))return {originals:[]};
            throw new Error('Unexpected request '+path);
        }});
        await window.nekoChatSync.openSettings();
        await body.children[0].querySelector('[data-sync-action="recover"]').events.click();
        assert.deepEqual(requests.filter(item=>item.path.endsWith('/recovery/preview')).map(item=>JSON.parse(item.body)),
            [{serviceUrl:'https://sync.nekotech.cn'}]);
        assert.equal(requests.filter(item=>item.path.endsWith('/history/approve')
            || item.path.endsWith('/recovery/deletions/approve')).length,0);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_empty_history_consumes_its_cursor_and_only_reloads_after_it_changes():
    """Successful empty restoration must not refetch the same history every local poll."""
    run_script(r"""(async()=>{
        const uid='c'.repeat(32),requests=[];let cursor=1;
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages(){throw new Error('Empty merge');}};
        global.fetch=async(path)=>({ok:true,async json(){
            requests.push(path);
            return path.endsWith('/status')?{characters:[{displayName:'Neko',characterSyncId:uid,binding:{cursor}}]}:
                {originals:[],localReplies:[]};
        }});
        documentEvents.DOMContentLoaded();
        const tick=intervals.find(item=>item.delay===5000).callback;
        await tick();await tick();
        assert.equal(requests.filter(path=>path.endsWith('/history')).length,1);
        cursor=2;await tick();
        assert.equal(requests.filter(path=>path.endsWith('/history')).length,2);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_same_text_distinct_ids_survive_and_exact_live_identity_is_replaced():
    """Only an explicit backend source ID can replace an optimistic bubble."""
    run_script(r"""
        const current = [{id:'optimistic',role:'user',blocks:[{type:'text',text:'same'}],status:'sent'}];
        const originals = [original, {...original,messageId:other,originSeq:2}];
        const merged = window.nekoChatSync.mergeHistory(current, originals, new Set(), new Map([['optimistic',id]]), 'Neko');
        assert.deepEqual(merged.map(x=>x.id), ['optimistic','sync-original-'+other]);
        assert.deepEqual(merged.map(x=>x.blocks[0].text), ['same','same']);
        assert.equal(merged[0].actions,undefined);
    """)


def test_memory_browser_save_receipt_hides_exact_chat_without_bubble_actions():
    """The saved receipt fences old pages and never deletes equal-text siblings."""
    run_script(r"""(async()=>{
        window.location={origin:'http://localhost'};
        let messages=[];
        window.reactChatWindowHost={getState:()=>({messages}),setMessages:next=>{messages=next;}};
        global.fetch=async(path)=>({ok:true,json:async()=>path.endsWith('/status')
            ? {characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{enabled:true}}]}
            : {originals:[original,{...original,messageId:other,originSeq:2}]}});
        await window.nekoChatSync.loadHistory();
        const data={action:'chat_sync_deleted',characterSyncId:original.characterSyncId,
            receipt:{messageIds:[id],localMessageIds:[]}};
        windowEvents.message({origin:'https://foreign.invalid',data});
        assert.equal(messages.length,2);
        global.fetch=async()=>{throw new Error('synthetic history outage');};
        windowEvents.message({origin:window.location.origin,data});
        assert.deepEqual(messages.map(item=>item.id),['sync-original-'+other]);
        assert.equal(messages[0].actions,undefined);
        assert.equal(windowEvents['react-chat-window:action'],undefined);
    })().catch(error=>{console.error(error);process.exitCode=1;});""")


def test_retired_delete_actions_are_removed_without_affecting_other_actions():
    run_script(r"""
        const keep={action:'unrelated',label:'keep'};
        const current=[{id:'live',role:'user',actions:[{action:'chat_sync_delete_original'},keep]}];
        const merged=window.nekoChatSync.mergeHistory(current,[original],new Set(),new Map([['live',id]]),'Neko');
        assert.deepEqual(merged[0].actions,[keep]);
    """)


def test_streaming_and_unsaved_live_messages_survive_download():
    """A displayed source cannot disappear before its final original was stored."""
    run_script(r"""
        const current = [{id:'streaming',role:'assistant',status:'streaming',turnId:'turn-one'},
            {id:'unsaved',role:'user',status:'sending'}];
        const merged = window.nekoChatSync.mergeHistory(current,[original],new Set(),new Map([['unsaved',other]]),'Neko',
            new Map([['turn-one',other]]));
        assert.deepEqual(merged.map(x=>x.id),['sync-original-'+id,'streaming','unsaved']);
        assert.equal(merged[1].status,'streaming');
    """)


def test_local_only_reply_stays_between_older_and_newer_originals():
    """A retained local reply keeps its known time after every passive refresh."""
    run_script(r"""
        const time=Number(original.originalTime);
        const local={id:'local-only',role:'assistant',createdAt:time+1000,status:'sent',
            turnId:'local-turn',blocks:[{type:'text',text:'same'}]};
        const originals=[{...original,messageId:other,originalTime:String(time+2000),originSeq:2},original];
        const current=[local];
        const merged=window.nekoChatSync.mergeHistory(current,originals,new Set(),new Map(),'Neko');
        assert.deepEqual(merged.map(item=>item.id),['sync-original-'+id,'local-only','sync-original-'+other]);
        assert.equal(merged[1].actions,undefined);
        assert.equal(merged[1].turnId,'local-turn');
        assert.deepEqual(merged.map(item=>item.sortKey),[0,1,2]);
        const refreshed=window.nekoChatSync.mergeHistory(merged,originals,new Set([id,other]),new Map(),'Neko');
        assert.deepEqual(refreshed,merged);
        assert.equal(local.sortKey,undefined);
        assert.deepEqual(current,[local]);
    """)


def test_retained_reply_order_preserves_ties_and_unknown_times():
    """Known source times interleave; missing live times are never guessed."""
    run_script(r"""
        const time=Number(original.originalTime);
        const current=[{id:'first-sentence',role:'assistant',createdAt:time,status:'streaming'},
            {id:'second-sentence',role:'assistant',createdAt:time,status:'streaming'},
            {id:'unknown',role:'user',status:'sending'}];
        const originals=[original,{...original,messageId:other,originSeq:2,originalTime:String(time+2000)}];
        const merged=window.nekoChatSync.mergeHistory(current,originals,new Set(),new Map(),'Neko');
        assert.deepEqual(merged.map(item=>item.id),['sync-original-'+id,'first-sentence','second-sentence',
            'sync-original-'+other,'unknown']);
        assert.equal(merged[1].status,'streaming');
        assert.equal(merged[4].status,'sending');
        assert.equal(merged[4].createdAt,undefined);
    """)


@pytest.mark.parametrize("bound", [False, True])
def test_fresh_window_restores_local_only_replies_without_sync_actions(bound):
    """New windows restore explicit local bytes even before cloud consent exists."""
    run_script(r"""(async()=>{
        const merged=[],requests=[];
        window.appendMessage=()=>{throw new Error('restoration submitted a reply');};
        window.sendTextPayload=()=>{throw new Error('restoration submitted input');};
        global.WebSocket=()=>{throw new Error('restoration opened a model socket');};
        window.reactChatWindowHost={getState:()=>({messages:[]}),setMessages:items=>merged.push(items)};
        const local={messageId:other,characterSyncId:original.characterSyncId,role:'assistant',text:'local only',
            originalTime:String(Number(original.originalTime)+1000)};
        global.fetch=async(path,options)=>{
            requests.push([path,options.method]);
            return {ok:true,json:async()=>({originals:BOUND?[original]:[],localReplies:[local]})};
        };
        const role={displayName:'Neko',characterSyncId:original.characterSyncId,binding:BOUND?{cursor:1}:null};
        await window.nekoChatSync.loadHistory({characters:[role]});
        assert.equal(merged.length,1);
        const restored=merged[0].find(item=>item.id==='local-reply-'+other);
        assert.equal(restored.blocks[0].text,'local only');
        assert.equal(restored.createdAt,Number(local.originalTime));
        assert.equal(restored.actions,undefined);
        assert.deepEqual(requests,[['/api/chat-sync/characters/'+original.characterSyncId+'/history','GET']]);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("BOUND", json.dumps(bound)))


def test_local_only_identity_replaces_exact_split_bubbles_without_text_deduplication():
    """Local restoration joins the same turn; equal text in another turn survives."""
    run_script(r"""
        const time=Number(original.originalTime),local={messageId:other,characterSyncId:original.characterSyncId,
            role:'assistant',text:'same',originalTime:String(time+1000)};
        const current=[{id:'sentence-one',role:'assistant',turnId:'local-turn',createdAt:time+1000},
            {id:'sentence-two',role:'assistant',turnId:'local-turn',createdAt:time+1000},
            {id:'different-turn',role:'assistant',turnId:'other-turn',createdAt:time+2000,blocks:[{type:'text',text:'same'}]}];
        const merge=items=>window.nekoChatSync.mergeHistory(items,[original],new Set(),new Map(),'Neko',
            new Map([['local-turn',other]]),[local]);
        const merged=merge(current);
        assert.deepEqual(merged.map(item=>item.id),['sync-original-'+id,'sentence-one','sentence-two','different-turn']);
        assert.equal(merged[1].actions,undefined);
        assert.deepEqual(merge(merged),merged);
    """)


def test_deleted_known_sources_do_not_reappear_from_live_bubbles():
    """An omitted previously known ID is removed together with its exact live alias."""
    run_script(r"""
        const current = [{id:'sync-original-'+id,role:'user'}, {id:'local-copy',role:'user'}, {id:'untouched',role:'assistant'}];
        const merged = window.nekoChatSync.mergeHistory(current,[],new Set([id]),new Map([['local-copy',id]]),'Neko');
        assert.deepEqual(merged.map(x=>x.id),['untouched']);
    """)


def test_assistant_turn_identity_handles_delayed_realistic_rendering():
    """Delayed split bubbles use an explicit turn map rather than text or position."""
    run_script(r"""
        const current = [{id:'sentence-1',role:'assistant',turnId:'turn-one'}, {id:'sentence-2',role:'assistant',turnId:'turn-one'}];
        const merged = window.nekoChatSync.mergeHistory(current,[{...original,role:'assistant'}],new Set(),new Map(),'Neko',
            new Map([['turn-one',id]]));
        assert.deepEqual(merged.map(x=>x.id),['sentence-1','sentence-2']);
        assert.equal(merged[0].actions,undefined);
        assert.equal(merged[1].actions,undefined);
    """)


def test_sync_keeps_live_split_bubble_keys_and_blocks_without_replaying_entry():
    """Canonical identity adds metadata; it must not remount already visible text."""
    run_script(r"""
        const first=[{type:'text',text:'First.'}],second=[{type:'text',text:'Second.'}];
        const current=[{id:'live-1',role:'assistant',turnId:'turn',blocks:first},
            {id:'live-2',role:'assistant',turnId:'turn',blocks:second}];
        const mappings=new Map([['turn',id]]),record={...original,role:'assistant',text:'First. Second.'};
        const merge=items=>window.nekoChatSync.mergeHistory(items,[record],new Set([id]),new Map(),'Neko',mappings);
        const merged=merge(current);
        assert.deepEqual(merged.map(x=>x.id),['live-1','live-2']);
        assert.equal(merged[0].blocks,first);assert.equal(merged[1].blocks,second);
        assert.deepEqual(merge(merged),merged);
        assert.deepEqual(window.nekoChatSync.mergeHistory(merged,[],new Set([id]),new Map(),'Neko',mappings),[]);
    """)


def test_unknown_original_time_never_becomes_download_time():
    """Unknown source time remains unknown in the current-window message view."""
    run_script(r"""
        const merged = window.nekoChatSync.mergeHistory([],[{...original,originalTime:null}],new Set(),new Map(),'Neko');
        assert.equal(merged[0].createdAt,0);
        assert.equal(merged[0].time,'chatSync.unknownTime');
    """)


def test_history_uses_passive_host_set_without_append_or_models():
    """A local original page restores without generating reactions or re-uploading."""
    run_script(r"""
        (async()=>{
            let calls=[];
            window.reactChatWindowHost={getState(){return {messages:[{id:'live',role:'user',status:'sending'}]};},
                setMessages(messages){calls.push(messages);}, appendMessage(){throw new Error('Active append used');}};
            global.fetch=async(path)=>({ok:true,async json(){
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{enabled:true}}]};
                assert.equal(path,'/api/chat-sync/characters/'+'c'.repeat(32)+'/history');
                // A late historical page can arrive after its same-millisecond successor.
                return {originals:[{...original,messageId:other,originSeq:2},original]};
            }});
            await window.nekoChatSync.loadHistory();
            assert.equal(calls.length,1);
            assert.deepEqual(calls[0].map(x=>x.id),['sync-original-'+id,'sync-original-'+other,'live']);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_automatic_local_cursor_refresh_is_passive_and_skips_unchanged_history():
    """The cloud worker's committed cursor reaches the current idle chat window."""
    run_script(r"""
        (async()=>{
            let messages=[], cursor=1, historyCalls=0;
            window.reactChatWindowHost={getState(){return {messages};},setMessages(next){messages=next;},
                appendMessage(){throw new Error('Automatic refresh triggered reaction');}};
            global.fetch=async(path)=>({ok:true,async json(){
                assert.ok(path.startsWith('/api/chat-sync/'),'Browser must only call local backend');
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor}}]};
                historyCalls++;
                return {originals:cursor===1?[original]:[original,{...original,messageId:other,originSeq:2}]};
            }});
            documentEvents.DOMContentLoaded();
            assert.equal(intervals[0].delay,5000);
            await intervals[0].callback();
            assert.equal(messages.length,1);
            await intervals[0].callback();
            assert.equal(historyCalls,1);
            cursor=2;
            await intervals[0].callback();
            assert.deepEqual(messages.map(item=>item.id),['sync-original-'+id,'sync-original-'+other]);
            assert.equal(historyCalls,2);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


@pytest.mark.parametrize("completion", ["assistantTurnCompletedId", "assistantTurnSettledId"])
@pytest.mark.parametrize("busy", [None, "streaming", "sending", "realistic", "awaiting", "submitted", "pending", "hidden"])
def test_completed_retained_turn_refreshes_only_after_real_busy_state_finishes(completion, busy):
    """A completed ID remains valid while real streaming/queued work still fences UI."""
    run_script("(async()=>{" + r"""
        let applied=0, requests=0;
        const completion=COMPLETION, busy=BUSY;
        window._nekoAssistantTurnId='retained';
        window.appState={assistantTurnId:'retained',[completion]:'retained'};
        window.reactChatWindowHost={getState(){return {messages:busy==='streaming'||busy==='sending'
            ?[{id:'live',status:busy}]:[]};},setMessages(){applied++;},appendMessage(){throw new Error('reaction');}};
        if(busy==='realistic')window._isProcessingRealisticQueue=true;
        if(busy==='awaiting')window.appState.assistantTurnAwaitingBubble=true;
        if(busy==='submitted')window._lastSubmittedRequestId='submitted';
        if(busy==='pending')window.appState.pendingTextTurnSubmitAt=Date.now();
        if(busy==='hidden')document.visibilityState='hidden';
        global.fetch=async(path)=>({ok:true,async json(){
            requests++;
            if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor:1}}]};
            assert.ok(path.endsWith('/history'));return {originals:[original]};
        }});
        documentEvents.DOMContentLoaded();
        await intervals[0].callback();
        assert.equal(applied,busy?0:1);
        assert.equal(requests,busy?0:2);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("COMPLETION", json.dumps(completion)).replace("BUSY", json.dumps(busy)))


@pytest.mark.parametrize("busy", [None, "realistic", "active", "unknown", "unidentified", "sending"])
def test_completed_turn_tail_bubbles_refresh_after_the_render_queue_drains(busy):
    """Exact end events release old tail bubbles without releasing genuine work."""
    run_script("(async()=>{" + r"""
        const busy=BUSY;
        let applied=0,requests=0;
        const messages=['previous','latest'].map(turnId=>({id:turnId,role:'assistant',turnId,status:'streaming'}));
        window.appState={assistantTurnId:'latest',assistantTurnCompletedId:'latest'};
        window.reactChatWindowHost={getState(){return {messages};},setMessages(){applied++;}};
        windowEvents['neko-assistant-turn-end']?.({detail:{turnId:'previous',source:'turn_end'}});
        windowEvents['neko-assistant-turn-end']?.({detail:{turnId:'latest',source:'turn_end'}});
        if(busy==='realistic')window._isProcessingRealisticQueue=true;
        if(busy==='active')window.appState.assistantTurnId='new-active';
        if(busy==='unknown')messages.push({id:'unknown',role:'assistant',turnId:'unconfirmed',status:'streaming'});
        if(busy==='unidentified')messages.push({id:'unidentified',role:'assistant',status:'streaming'});
        if(busy==='sending')messages.push({id:'pending',role:'user',turnId:'latest',status:'sending'});
        global.fetch=async(path)=>({ok:true,async json(){
            requests++;
            if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor:1}}]};
            return {originals:[original]};
        }});
        documentEvents.DOMContentLoaded();
        await intervals[0].callback();
        assert.equal(applied,busy?0:1);
        assert.equal(requests,busy?0:2);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("BUSY", json.dumps(busy)))


@pytest.mark.parametrize("completion", ["assistantTurnCompletedId", "assistantTurnSettledId"])
def test_existing_completion_releases_only_its_exact_assistant_tail(completion):
    """A page can observe completion state even if it missed the end event."""
    run_script("(async()=>{" + r"""
        let applied=0;
        window.appState={assistantTurnId:'retained',[COMPLETION]:'retained'};
        window.reactChatWindowHost={getState(){return {messages:[
            {id:'tail',role:'assistant',turnId:'retained',status:'streaming'}]};},setMessages(){applied++;}};
        global.fetch=async(path)=>({ok:true,async json(){
            if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor:1}}]};
            return {originals:[original]};
        }});
        documentEvents.DOMContentLoaded();
        await intervals[0].callback();
        assert.equal(applied,1);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("COMPLETION", json.dumps(completion)))


@pytest.mark.parametrize("lifecycle", ["explicit_null", "legacy_missing", "current_active", "unknown_streaming"])
def test_current_lifecycle_distinguishes_cleared_role_from_legacy_fallback(lifecycle):
    """Cleared current state ignores an old role ID while real and legacy work waits."""
    run_script(r"""(async()=>{
        const lifecycle=LIFECYCLE, requests=[], merged=[];
        const messages=lifecycle==='unknown_streaming'
            ?[{id:'unknown-live',role:'assistant',turnId:'unknown-current',status:'streaming'}]:[];
        window._nekoAssistantTurnId='old-role-turn';
        window.appState={assistantTurnId:null,assistantTurnCompletedId:null,assistantTurnSettledId:null,
            assistantTurnAwaitingBubble:false,pendingTextTurnSubmitAt:0};
        if(lifecycle==='legacy_missing')delete window.appState.assistantTurnId;
        if(lifecycle==='current_active')window.appState.assistantTurnId='current-role-turn';
        window.reactChatWindowHost={getState:()=>({messages}),setMessages:items=>merged.push(items),
            appendMessage(){throw new Error('Passive restoration must not enqueue a reaction');}};
        document.visibilityState='visible';
        global.fetch=async(path)=>{requests.push(path);return {ok:true,json:async()=>path.endsWith('/status')
            ?{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor:1}}]}
            :{originals:[original]}};};
        documentEvents.DOMContentLoaded();
        await window.nekoChatSync.notifyHistoryCommitted({type:'chat_sync_history_changed',characterSyncId:original.characterSyncId});
        if(lifecycle==='explicit_null'){
            assert.equal(merged.length,1);
        }else{
            assert.equal(merged.length,0);
            assert.equal(requests.length,0);
            window.appState.assistantTurnCompletedId='unrelated-turn';
            await intervals[0].callback();
            assert.equal(merged.length,0);
            if(lifecycle==='legacy_missing')window._nekoAssistantTurnId=null;
            if(lifecycle==='current_active'){
                window.appState.assistantTurnCompletedId='old-role-turn';
                await intervals[0].callback();
                assert.equal(merged.length,0);
                window.appState.assistantTurnCompletedId='current-role-turn';
            }
            if(lifecycle==='unknown_streaming')messages[0].status='sent';
            await intervals[0].callback();
            assert.equal(merged.length,1);
        }
        assert.deepEqual(merged[0].map(item=>item.id),lifecycle==='unknown_streaming'
            ?['sync-original-'+id,'unknown-live']:['sync-original-'+id]);
        assert.equal(requests.length,2);
        assert.ok(requests.every(path=>path.endsWith('/status')||path.endsWith('/history')));
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("LIFECYCLE", json.dumps(lifecycle)))


def test_foreground_wake_coalesces_visibility_and_pageshow_without_manual_sync():
    """Hidden pages stay quiet; returning wakes all roles through the local bridge."""
    run_script(r"""
        (async()=>{
            let wakeCount=0,release;
            const waiting=new Promise(resolve=>release=resolve);
            global.fetch=async(path,options)=>{
                if(path.endsWith('/wake')){
                    assert.equal(options.method,'POST');wakeCount++;await waiting;
                    return {ok:true,json:async()=>({scheduled:true})};
                }
                assert.ok(path.endsWith('/status'),'No manual sync or role-limited network operation');
                return {ok:true,json:async()=>({characters:[]})};
            };
            documentEvents.DOMContentLoaded();
            document.visibilityState='hidden';
            await documentEvents.visibilitychange();
            assert.equal(wakeCount,0);
            document.visibilityState='visible';
            const first=documentEvents.visibilitychange(),second=windowEvents.pageshow();
            assert.equal(wakeCount,1);
            release();await Promise.all([first,second]);
            assert.equal(wakeCount,1);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_automatic_history_defers_streaming_before_and_during_io_without_consuming_cursor():
    """A deferred cursor remains retryable when the same live turn finishes."""
    run_script(r"""
        (async()=>{
            let messages=[], requests=0, applied=0, beginDuringRead=true;
            window.reactChatWindowHost={getState(){return {messages};},setMessages(next){messages=next;applied++;}};
            global.fetch=async(path)=>({ok:true,async json(){
                requests++;
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor:1}}]};
                if(beginDuringRead)window._nekoAssistantTurnId='active-turn';
                return {originals:[original]};
            }});
            documentEvents.DOMContentLoaded();
            window._nekoAssistantTurnId='active-turn';
            await intervals[0].callback();
            assert.equal(requests,0);
            window._nekoAssistantTurnId=null;
            await intervals[0].callback();
            assert.equal(applied,0);
            window._nekoAssistantTurnId=null;
            beginDuringRead=false;
            await intervals[0].callback();
            assert.equal(applied,1);
            assert.equal(messages[0].id,'sync-original-'+id);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_automatic_history_cannot_replace_new_role_after_local_io():
    """The automatic path keeps the same role and latest-request fences."""
    run_script(r"""
        (async()=>{
            window.reactChatWindowHost={getState(){return {messages:[]};},setMessages(){throw new Error('Stale auto history applied');}};
            global.fetch=async(path)=>({ok:true,async json(){
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{cursor:1}}]};
                window.lanlan_config.lanlan_name='Other';
                return {originals:[original]};
            }});
            documentEvents.DOMContentLoaded();
            await intervals[0].callback();
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_late_old_character_page_cannot_replace_new_character_window():
    """Changing the active local profile while a page waits prevents its application."""
    run_script(r"""
        (async()=>{
            window.reactChatWindowHost={getState(){return {messages:[]};},setMessages(){throw new Error('Stale page applied');}};
            global.fetch=async(path)=>({ok:true,async json(){
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{enabled:true}}]};
                window.lanlan_config.lanlan_name='Other';
                return {originals:[original]};
            }});
            await window.nekoChatSync.loadHistory();
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_latest_same_character_history_wins_when_responses_arrive_out_of_order():
    """An older page cannot revive a message after a newer empty deletion view."""
    run_script(r"""
        (async()=>{
            let messages=[], releaseOld, enteredOld;
            const oldEntered=new Promise(resolve=>enteredOld=resolve);
            const oldPage=new Promise(resolve=>releaseOld=resolve);
            let histories=0;
            window.reactChatWindowHost={getState(){return {messages};},setMessages(next){messages=next;}};
            global.fetch=async(path)=>({ok:true,async json(){
                if(path.endsWith('/status'))return {characters:[{displayName:'Neko',characterSyncId:'c'.repeat(32),binding:{enabled:true}}]};
                if(++histories===1){enteredOld();return await oldPage;}
                return {originals:[]};
            }});
            const old=window.nekoChatSync.loadHistory();
            await oldEntered;
            await window.nekoChatSync.loadHistory();
            assert.deepEqual(messages,[]);
            releaseOld({originals:[original]});
            await old;
            assert.deepEqual(messages,[]);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


@pytest.mark.parametrize("locale", ["en", "es", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"])
def test_sync_locale_namespace_is_complete(locale):
    """Every supported locale supplies the same feature labels and message placeholders."""
    baseline = json.loads((ROOT / "static/locales/en.json").read_text(encoding="utf-8"))["chatSync"]
    actual = json.loads((ROOT / f"static/locales/{locale}.json").read_text(encoding="utf-8"))["chatSync"]
    assert actual.keys() == baseline.keys()
    assert "{{originals}}" in actual["pending"]
    assert "{{deletions}}" in actual["pending"]


def test_both_served_pages_restore_sync_without_a_header_control():
    """Both chat entrypoints keep passive restoration without an extra user action."""
    for name in ("index", "chat"):
        html = (ROOT / f"templates/{name}.html").read_text(encoding="utf-8")
        assert 'id="chatSyncButton"' not in html
        assert 'id="react-chat-window-root"' in html
        assert '/static/app/app-chat-sync.js?v=' in html


@pytest.mark.parametrize("lifecycle", ["poll", "pageshow", "visibilitychange"])
def test_chat_host_refreshes_and_wakes_without_a_sync_button(lifecycle):
    """Removing the header control must not disable local polling or foreground catchup."""
    run_script(r"""
        (async()=>{
            const lifecycle=LIFECYCLE, requests=[];
            let messages=[], cursor=1;
            document.getElementById=name=>name==='react-chat-window-root'?body:null;
            document.visibilityState='visible';
            window.reactChatWindowHost={getState:()=>({messages}),setMessages:next=>{messages=next;},
                appendMessage(){throw new Error('Passive sync must not invoke message reactions');}};
            global.fetch=async(path,options)=>{
                requests.push({path,method:options.method});
                assert.ok(path.endsWith('/status')||path.endsWith('/history')||path.endsWith('/wake'));
                if(path.endsWith('/wake'))assert.equal(options.method,'POST');
                return {ok:true,json:async()=>path.endsWith('/status')
                    ?{characters:[{displayName:'Neko',characterSyncId:original.characterSyncId,binding:{cursor}}]}
                    :path.endsWith('/history')?{originals:cursor===1?[original]:[original,{...original,messageId:other,originSeq:2}]}:{}};
            };
            documentEvents.DOMContentLoaded();
            assert.equal(intervals.length,1);
            await intervals[0].callback();
            assert.deepEqual(messages.map(message=>message.id),['sync-original-'+id]);
            cursor=2;
            if(lifecycle==='poll')await intervals[0].callback();
            else if(lifecycle==='pageshow'){
                windowEvents.pagehide();
                await windowEvents.pageshow();
                assert.equal(intervals.length,2);
            }else{
                document.visibilityState='hidden';
                const previous=requests.length;
                await documentEvents.visibilitychange();
                assert.equal(requests.length,previous);
                document.visibilityState='visible';
                await documentEvents.visibilitychange();
            }
            assert.deepEqual(messages.map(message=>message.id),['sync-original-'+id,'sync-original-'+other]);
            assert.equal(requests.filter(request=>request.path.endsWith('/wake')).length,lifecycle==='poll'?0:1);
            assert.equal(body.children.length,0);
        })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace("LIFECYCLE", json.dumps(lifecycle)))


@pytest.mark.parametrize("kind", ["chat", "profile"])
def test_sync_dialogs_accept_pointer_input_without_releasing_chat_passthrough(kind):
    """Real CSS inheritance must allow modal clicks while chat/tutorial gates stay intact."""
    from playwright.sync_api import sync_playwright

    template = (ROOT / ("templates/chat.html" if kind == "chat" else "templates/character_card_manager.html")).read_text(encoding="utf-8")
    stylesheets = re.findall(r'<link rel="stylesheet" href="([^"?]+)', template)
    # static/react/ is a gitignored vite output that unit jobs never build. Its
    # chat rules come from the plain source stylesheet, so load that instead and
    # keep this click check running in clean CI (vendor @imports carry no gates).
    react_source = re.sub(r"^@import[^;]*;", "", (ROOT / "frontend/react-neko-chat/src/styles.css").read_text(encoding="utf-8"),
                          flags=re.MULTILINE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # This disposable page never reaches the running backend or user data.
            page.route("**/*", lambda route: route.abort())
            page.set_content(('<body class="electron-chat-window subtitle-web-host yui-guide-chat-buttons-disabled">' if kind == 'chat' else '<body>') +
                             '<div id="react-chat-window-shell"><button id="tutorial-control">Tutorial</button></div>'
                             '<button id="chatSyncButton">Sync</button></body>')
            for href in stylesheets:
                built = ROOT / href.lstrip("/")
                if href.startswith("/static/react/") and not built.is_file():
                    page.add_style_tag(content=react_source)
                else:
                    page.add_style_tag(path=str(built))
            page.add_style_tag(path=str(ROOT / "static/css/chat_sync.css"))
            for css in re.findall(r"<style>(.*?)</style>", template, re.DOTALL):
                page.add_style_tag(content=css)
            page.evaluate("""() => {
                window.t = key => key;
                window.setInterval = () => 1;
                window.lanlan_config = {lanlan_name: 'Synthetic'};
                window.fetchCalls = [];
                window.fetch = async path => {
                    window.fetchCalls.push(path);
                    return {ok: true, json: async () => path.endsWith('/history') ? {originals: []} : {
                        serviceUrl: 'https://sync.test', characters: [{displayName: 'Synthetic',
                        characterSyncId: 'a'.repeat(32), binding: {enabled: true, pendingOriginals: 0, pendingDeletions: 0}}]}};
                };
            }""")
            if kind == "chat":
                page.add_script_tag(path=str(SCRIPT))
                page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
                page.locator("#chatSyncButton").click()
                modal = page.locator("#chat-sync-dialog")
            else:
                page.add_script_tag(path=str(ROOT / "static/app/app-character-profile-sync.js"))
                page.evaluate("""async () => {
                    const profile = {schemaVersion: 1, name: 'Synthetic', nickname: '', systemPrompt: '', fields: [], persona: null};
                    await window.nekoCharacterProfileSync.review('a'.repeat(32), {local: profile, head: profile,
                        local_hash: 'b'.repeat(64), head_revision: 1, conflict: true, renameRequired: false});
                }""")
                modal = page.locator("dialog")
            assert page.locator("body").evaluate("node => getComputedStyle(node).pointerEvents") == ("none" if kind == "chat" else "auto")
            assert page.locator("#tutorial-control").evaluate("node => getComputedStyle(node).pointerEvents") == ("none" if kind == "chat" else "auto")
            assert modal.locator("button").evaluate_all(
                "nodes => nodes.every(node => getComputedStyle(node).pointerEvents === 'auto')"
            )
            if kind == "chat":
                assert modal.locator('[data-sync-action]').count() == 1
                assert modal.locator('[data-sync-action="recover"]').is_disabled()
                assert modal.locator('input, summary').count() == 0
                modal.get_by_role("button", name="chatSync.close", exact=True).click()
            else:
                modal.locator('[data-profile-choice="close"]').click()
            assert page.locator("dialog[open]").count() == 0
        finally:
            browser.close()


@pytest.mark.parametrize("change", ["closed", "role", "uid"])
def test_recovery_ignores_late_results_after_its_dialog_context_changes(change):
    """An old recovery result cannot prompt deletion for a newly opened character."""
    run_script(r"""(async()=>{
        const uid='c'.repeat(32),requests=[];let release,changed=false;
        window.confirm=()=>{throw new Error('Stale recovery requested deletion consent');};
        global.fetch=async(path,options)=>({ok:true,async json(){
            requests.push({path,body:options.body});
            if(path.endsWith('/status'))return {writerRecoveryRequired:true,characters:[{
                displayName:changed&&CHANGE==='role'?'Other':'Neko',
                characterSyncId:changed&&CHANGE==='uid'?'d'.repeat(32):uid,binding:null}]};
            if(path.endsWith('/recovery/preview'))return await new Promise(resolve=>{release=resolve;});
            throw new Error('Unexpected request '+path);
        }});
        await window.nekoChatSync.openSettings();
        const modal=body.children[0],recover=modal.querySelector('[data-sync-action="recover"]');
        const pending=recover.events.click();
        await new Promise(resolve=>setImmediate(resolve));
        await recover.events.click();
        assert.equal(requests.filter(item=>item.path.endsWith('/recovery/preview')).length,1);
        changed=true;
        if(CHANGE==='closed')modal.close();
        else if(CHANGE==='role')await window.nekoChatSync.openSettings('Other');
        release({history:{localName:'Neko',cloudName:'Cloud Neko'},deletions:{count:1,taskId:'e'.repeat(32)}});
        await pending;
        assert.equal(requests.filter(item=>item.path.endsWith('/recovery/deletions/approve')
            ||item.path.endsWith('/history/approve')).length,0);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """.replace('CHANGE', json.dumps(change)))


@pytest.mark.parametrize("theme,language,width,needs_recovery", [
    ("light", "zh-CN", 375, True), ("dark", "zh-CN", 1280, True), ("dark", "ru", 375, False),
])
def test_recovery_panel_tooltip_is_accessible_and_fits_the_viewport(theme, language, width, needs_recovery):
    """Mouse and keyboard help remain usable even while the recovery button is disabled."""
    from playwright.sync_api import sync_playwright

    labels = json.loads((ROOT / "static/locales" / (language + ".json")).read_text(encoding="utf-8"))["chatSync"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 700})
            page.route("**/*", lambda route: route.abort())
            page.set_content('<html data-theme="' + theme + '"><body></body></html>')
            page.add_style_tag(path=str(ROOT / "static/css/chat_sync.css"))
            page.evaluate("""({labels,needsRecovery})=>{
                window.t=key=>labels[key.replace('chatSync.','')];
                window.setInterval=()=>1;window.lanlan_config={lanlan_name:'Synthetic'};
                window.fetch=async()=>({ok:true,json:async()=>({writerRecoveryRequired:needsRecovery,
                    characters:[{displayName:'Synthetic',characterSyncId:'c'.repeat(32),binding:null}]})});
            }""", {"labels": labels, "needsRecovery": needs_recovery})
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("async()=>await window.nekoChatSync.openSettings()")
            modal = page.locator('#chat-sync-dialog')
            assert modal.locator('[data-sync-action]').evaluate_all("nodes=>nodes.map(node=>node.dataset.syncAction)") == ['recover']
            assert modal.locator('[data-sync-action="recover"]').is_enabled() == needs_recovery
            assert modal.locator('details, [data-history-action]').count() == 0
            help_button = modal.get_by_role('button', name=labels['recoverWriterHelp'], exact=True)
            tooltip = modal.get_by_role('tooltip', include_hidden=True)
            assert not tooltip.is_visible()
            help_button.hover()
            assert tooltip.is_visible()
            page.mouse.move(0, 0)
            help_button.focus()
            assert tooltip.is_visible()
            assert help_button.get_attribute('aria-describedby') == tooltip.get_attribute('id')
            bounds = tooltip.bounding_box()
            assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
            assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 700
            assert modal.bounding_box()['width'] <= width - 32
            modal.get_by_role('button', name=labels['close'], exact=True).click()
            assert page.locator('dialog[open]').count() == 0
        finally:
            browser.close()


@pytest.mark.parametrize("approve_deletions", [False, True])
@pytest.mark.parametrize("history_available", [False, True])
def test_writer_recovery_keeps_separate_deletion_consent_without_history_controls(approve_deletions, history_available):
    """The compact recovery panel cannot publish history or copied deletes by opening it."""
    from playwright.sync_api import sync_playwright

    labels = json.loads((ROOT / "static/locales/en.json").read_text(encoding="utf-8"))["chatSync"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content('<button id="chatSyncButton">Sync</button>')
            page.evaluate("""({labels, historyAvailable}) => {
                window.t = (key, values = {}) => labels[key.replace('chatSync.', '')]
                    .replace(/{{(.*?)}}/g, (_, name) => values[name]);
                window.setInterval = () => 1;
                window.lanlan_config = {lanlan_name: 'Synthetic'};
                window.requests = []; window.needsRecovery = true;
                window.fetch = async (path, options) => {
                    window.requests.push({path, body: options.body});
                    return {ok: true, json: async () => {
                        if (path.endsWith('/status')) return {serviceUrl: 'https://localhost:8443',
                            writerRecoveryRequired: window.needsRecovery, characters: [{displayName: 'Synthetic',
                            characterSyncId: 'c'.repeat(32), binding: {enabled: !window.needsRecovery,
                                ownerId: 'owner', bindingId: 'binding', generation: 1}}]};
                        if (path.endsWith('/recovery/preview')) {
                            window.needsRecovery = false;
                            return {history: historyAvailable ? {localName: 'Synthetic', cloudName: 'Cloud Neko', count: 3} : null,
                                historyError: historyAvailable ? null : 'chat_sync_unavailable',
                                deletions: {taskId: 'e'.repeat(32), count: 2, localName: 'Synthetic', cloudName: 'Cloud Neko'}};
                        }
                        if (path.endsWith('/recovery/deletions/approve')) return {status: 'registered'};
                        if (path.endsWith('/history')) return {originals: []};
                        throw new Error('Unexpected request: ' + path);
                    }};
                };
            }""", {"labels": labels, "historyAvailable": history_available})
            page.add_script_tag(path=str(SCRIPT))
            page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
            page.locator("#chatSyncButton").click()
            modal = page.locator("#chat-sync-dialog")
            assert modal.locator('[data-sync-action]').count() == 1
            assert modal.locator('[data-sync-action="recover"]').is_enabled()
            assert modal.locator('[data-history-action], details').count() == 0
            assert page.evaluate("window.requests.every(item => item.body === undefined)")
            messages = []

            def consent(dialog):
                messages.append(dialog.message)
                dialog.accept() if approve_deletions else dialog.dismiss()

            page.on("dialog", consent)
            modal.get_by_role("button", name=labels["recoverWriter"], exact=True).click()
            modal.get_by_role("status").filter(has_text=labels["recoveryChecked"]).wait_for()
            if not history_available:
                assert labels['initialHistoryFailed'] in modal.get_by_role('status').inner_text()
            assert messages and "2" in messages[0] and "Synthetic" in messages[0] and "Cloud Neko" in messages[0]
            assert page.evaluate("window.requests.filter(item => item.path.endsWith('/recovery/preview')).map(item => JSON.parse(item.body))") == [{"serviceUrl": "https://sync.nekotech.cn"}]
            assert page.evaluate("window.requests.filter(item => item.path.endsWith('/recovery/deletions/approve')).map(item => JSON.parse(item.body))") == ([{"taskId": "e" * 32}] if approve_deletions else [])
            assert page.evaluate("window.requests.some(item => ['/history/approve','/sync-choice','/enable','/sync'].some(suffix => item.path.endsWith(suffix)))") is False
            assert modal.locator('[data-sync-action="recover"]').is_disabled()
        finally:
            browser.close()

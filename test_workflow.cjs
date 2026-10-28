const vm=require('vm'),fs=require('fs'),assert=require('assert');
let promptAnswer=null,promptCount=0,requestCount=0;const streams=[];class EventSource{constructor(url){this.url=url;this.listeners={};streams.push(this)}addEventListener(k,fn){this.listeners[k]=fn}close(){this.closed=true}};
const base=process.env.TRAILMIX_TEST_URL||'http://127.0.0.1:8081';let cookie='';const nodes=new Map(),download=[];
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:'',innerHTML:'',textContent:'',style:{},checked:false,tagName:'INPUT',focus(){document.activeElement=this},setAttribute(k,v){this[k]=v}});return nodes.get(id)}
const classes=new Set(), prefs=new Map();
Object.defineProperty(node('content'),'innerHTML',{get(){return this._html||''},set(v){this._html=v;nodes.delete('inspectionMass');nodes.delete('reworkMass')}});
const document={body:{classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name)}}},getElementById(id){if(['inspectionMass','reworkMass'].includes(id)&&!node('content').innerHTML.includes('id="'+id+'"'))return null;return node(id)},activeElement:{tagName:'BODY'},createElement(){const a={click(){download.push(a.href)}};return a}};
const intervals=[];
const context=vm.createContext({document,localStorage:{getItem(k){return prefs.get(k)||''},setItem(k,v){prefs.set(k,v)},removeItem(k){prefs.delete(k)}},console,URL,Blob,EventSource,AbortSignal,Math,Date,JSON,Number,Promise,Error,setTimeout,clearTimeout,setInterval:fn=>{intervals.push(fn)},window:{addEventListener(){}},prompt:()=>{promptCount++;return promptAnswer},confirm:()=>true,fetch:async(path,options={})=>{requestCount++;const headers={...options.headers,...(cookie?{Cookie:cookie}:{})};const r=await fetch(base+path,{...options,headers});const set=r.headers.get('set-cookie');if(set)cookie=set.split(';')[0];return r}});
const js=fs.readFileSync(require('path').join(__dirname,'trailmix.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInContext(js,context);
const run=s=>vm.runInContext(s,context),sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function settled(){for(let i=0;i<200;i++){if(!run('busy')&&!run('outboxSending')&&!run('polling'))return;await sleep(10)}throw Error('Request did not settle')}
async function action(code){run(code);await settled();assert(!run('pending'),'Unconfirmed request');if(node('toast').textContent&&!node('toast').textContent.startsWith('Saved'))throw Error(node('toast').textContent)}
function val(id,v){node(id).value=String(v)}
function view(p){run(`go(${JSON.stringify(p)})`);assert(node('content').innerHTML.includes('<h1>'));for(const m of node('content').innerHTML.matchAll(/onclick="([^"]*)"/g))new Function(m[1]);return node('content').innerHTML}
(async()=>{
await sleep(100);await settled();assert(node('content').innerHTML.includes('Connect to production'));
val('accessCode','partly-entered');const idleRequests=requestCount;for(let i=0;i<10;i++){for(const tick of intervals)tick();await run('refresh()')}assert.equal(requestCount,idleRequests);assert.equal(node('accessCode').value,'partly-entered');assert.equal(streams.length,0);val('accessCode','test-access');await run('login()');await settled();assert(run('connected'));assert.equal(streams.length,1);const liveRequests=requestCount;for(let i=0;i<10;i++)for(const tick of intervals)tick();assert.equal(requestCount,liveRequests);val('operator','DOM flow test');
view('Labeling');val('newLabels',4);await action("doAction('label')");val('dest',1);val('labelPickup',4);await action("doAction('labelPickup')");
view('Filling stations');const movement=run('state.moves[0].id');await action(`doAction('receiveLabels',${movement})`);val('mixBatch1','MIX-DOM-01');await action("doAction('assignBatch',1)");await action("doAction('fill',1)");const bag=run('state.bags[0].id');val('fillPickup1',1);await action("doAction('fillPickup',1)");
let html=view('Inspection');assert(html.includes('Receive 1 bags'));await action("doAction('receive','inspectionTransit')");assert.equal(document.activeElement.id,'inspectionMass');assert(!node('content').innerHTML.includes('id="labelOK"'));val('inspectionMass',2.4);run(`toggleLabelFailure(${bag})`);assert.equal(run('labelFailBag'),bag);assert.equal(node('inspectionMass').value,'2.4');assert.equal(document.activeElement.id,'inspectionMass');run(`toggleLabelFailure(${bag})`);assert.equal(run('labelFailBag'),null);val('inspectionMass',2.4);await action(`doAction('weigh',${bag})`);assert.equal(run('state.bags[0].status'),'hold');
run("enterStation('inspection')");assert(node('content').innerHTML.includes('stationReworkPickup'));val('stationReworkPickup',1);await action("doAction('move','hold','','stationReworkPickup')");run('exitStationMode()');html=view('Rework');assert(html.includes('Receive 1 bags'));await action("doAction('receive','reworkTransit')");val('reworkMass',2.1);await action(`doAction('clear',${bag})`);assert.equal(run('state.bags[0].status'),'returnReady');
run("enterStation('rework')");assert(node('content').innerHTML.includes('stationReturnPickup'));val('stationReturnPickup',1);await action("doAction('move','returnReady','','stationReturnPickup')");run('exitStationMode()');view('Sealing');await action("doAction('receive','returnTransit')");assert.equal(run('state.bags[0].status'),'sealing');await action(`doAction('seal',${bag})`);
run("enterStation('sealing')");assert(node('content').innerHTML.includes('stationFinalPickup'));val('stationFinalPickup',1);await action("doAction('move','sealed','','stationFinalPickup')");run('exitStationMode()');view('Final inspection');await action("doAction('receive','finalTransit')");assert(!node('content').innerHTML.includes('type="checkbox"'));assert(!node('nav').innerHTML.includes('Transport & final'));
const beforeFail=run('state.events.length');promptAnswer=null;await action(`doAction('finalFail',${bag})`);assert.equal(run('state.events.length'),beforeFail);
promptAnswer='Leak at bag corner';await action(`doAction('finalFail',${bag})`);assert.equal(run('state.bags[0].status'),'finalHold');assert.equal(run('state.events[state.events.length-1].detail'),'Leak at bag corner');
await run("send('disposition',{bag:"+bag+",destination:'reseal',bagLabelOK:true,note:'Bag and label acceptable for reseal',safetyPin:'123456'})");await settled();
view('Sealing');await action(`doAction('seal',${bag})`);val('sealingFinalPickup',1);await action("doAction('move','sealed','','sealingFinalPickup')");view('Final inspection');await action("doAction('receive','finalTransit')");
run("enterStation('final')");assert(!node('content').innerHTML.includes('type="checkbox"'));const promptsBeforePass=promptCount;await action(`doAction('finish',${bag})`);assert.equal(promptCount,promptsBeforePass);assert.equal(run('state.events[state.events.length-1].data.confirmation'),'pass-button');run('exitStationMode()');assert.equal(run('state.bags[0].status'),'done');
for(const p of ['Overview','Labeling','Filling stations','Inspection','Rework','Sealing','Final inspection','Activity','Safety & holds','Settings'])view(p);
await run('loadRuns()');assert(node('runList').innerHTML.includes('Active'));
run("exportData('weights')");assert(download[0].includes('weights'));const res=await fetch(base+download[0],{headers:{Cookie:cookie}});assert(res.ok);assert((await res.text()).includes('MIX-DOM-01'));
const resUnauth=await fetch(base+'/api/state');assert.equal(resUnauth.status,401);
const forged=await fetch(base+'/api/action',{method:'POST',headers:{Cookie:cookie,'Content-Type':'application/json','X-TrailMix':'1',Origin:'http://evil.example'},body:'{}'});assert.equal(forged.status,403);
const missingHeader=await fetch(base+'/api/action',{method:'POST',headers:{Cookie:cookie,'Content-Type':'application/json'},body:'{}'});assert.equal(missingHeader.status,403);

// Device mode must limit controls, persist locally, and leave the production log intact.
const beforeMode=run('state.events.length');
run("enterStation('fill:1')");assert(classes.has('station-mode'));assert.equal(run('stationMode'),'fill:1');assert.equal(prefs.get('trailmix-desk-v1-station'),'fill:1');
assert(node('stationModeButton').hidden);assert(!node('exitStationButton').hidden);
assert(node('content').innerHTML.includes('+ Filled 1 bag'));assert(!node('content').innerHTML.includes('Filling station 2'));assert(!node('content').innerHTML.includes('Filling station performance'));
run("go('Settings')");assert(node('content').innerHTML.includes('+ Filled 1 bag'));assert(!node('content').innerHTML.includes('Download for analysis'));
for(const mode of ['label','fill:1','fill:2','fill:3','inspection','rework','sealing','final','stocker','firstMover','finalMover']){
 run('enterStation('+JSON.stringify(mode)+')');const html=node('content').innerHTML;assert(html.includes('<h1'));assert(html.includes('Station mode'));assert(!html.includes('Download for analysis'));
 for(const m of html.matchAll(/onclick="([^"]*)"/g))new Function(m[1]);
}
assert.equal(run('state.events.length'),beforeMode);
run('exitStationMode()');assert(!classes.has('station-mode'));assert(!prefs.has('trailmix-desk-v1-station'));assert.equal(run('page'),'Overview');
// The next bag's controls actually save through the shared API in station mode.
run("enterStation('fill:1')");await action("doAction('fill',1)");assert.equal(run('state.bags.length'),2);assert.equal(run('state.bags[1].station'),1);assert.equal(run('stationMode'),'fill:1');
run('exitStationMode()');


const secondBag=run('state.bags[1].id');
await run("send('move',{source:'filled',station:1,qty:1})");await settled();
await run("send('receive',{source:'inspectionTransit',qty:1})");await settled();
run("enterStation('inspection')");assert.equal(document.activeElement.id,'inspectionMass');
const stationHtml=node('content').innerHTML;assert(stationHtml.includes("event.key==='Enter'"));assert(stationHtml.includes('Label failed'));assert(!stationHtml.includes('Choose label result'));
val('inspectionMass',2.13);run('toggleLabelFailure('+secondBag+')');
await action("doAction('weigh',"+secondBag+")");assert.equal(run('state.bags[1].status'),'discarded');assert.equal(run('state.events[state.events.length-1].data.labelOK'),false);assert.equal(run('labelFailBag'),null);
run('exitStationMode()');


// Keyboard Enter submits exactly once and focuses the empty input for the next bag.
run("enterStation('fill:1')");await action("doAction('fill',1)");await action("doAction('fill',1)");
await run("send('move',{source:'filled',station:1,qty:2})");await settled();
await run("send('receive',{source:'inspectionTransit',qty:2})");await settled();
run("enterStation('inspection')");const queued=JSON.parse(run("JSON.stringify(q('inspection'))"));assert.equal(queued.length,2);
val('inspectionMass',2.11);const firstWeightInput=node('inspectionMass');document.activeElement=firstWeightInput;
context.keyboardSubmit={tagName:'BUTTON',disabled:false,closest(){return null},click(){run("doAction('weigh',"+queued[0].id+")")}};
run("stationKeys.set('enter',keyboardSubmit);stationControls=[{action:'weigh',el:keyboardSubmit}]");context.enterEvent={key:'Enter',altKey:false,ctrlKey:false,metaKey:false,repeat:false,isComposing:false,preventDefault(){},stopImmediatePropagation(){}};
run('stationKeyboard(enterEvent)');await settled();
assert.equal(run("q('inspection').length"),1);assert.equal(document.activeElement.id,'inspectionMass');assert.notEqual(document.activeElement,firstWeightInput);assert.equal(node('inspectionMass').value,'');assert(node('content').innerHTML.includes('B'+String(queued[1].id).padStart(4,'0')));
run('exitStationMode()');


// Team assignments, targeted messages, and source-specific mover controls.
await run("send('personAdd',{name:'Mover One',assignments:['stocker','firstMover']})");await settled();const mover=run('state.people[state.people.length-1].id');
await run("send('personUpdate',{person:'"+mover+"',assignments:['stocker','firstMover'],status:'working',expectedVersion:0})");await settled();
await run("send('messagePost',{targetType:'person',target:'"+mover+"',text:'PRIVATE: assignment note',priority:'normal'})");await settled();const privateMessage=run('state.messages[state.messages.length-1].id');
await run("send('messagePost',{targetType:'delivery',text:'PUBLIC: collect from Filling 2',priority:'urgent'})");await settled();
view('Operators');assert(node('content').innerHTML.includes('Mover One'));assert(node('content').innerHTML.includes('First inspection mover'));assert(node('content').innerHTML.includes('PRIVATE: assignment note'));
val('operator','Mover One');run('renderPersonBar()');assert(node('personBar').innerHTML.includes('PRIVATE: assignment note'));assert(node('personBar').innerHTML.includes('Bag stocker'));
// Acknowledging a message preserves a partially typed weight and its focus.
run("enterStation('inspection')");val('inspectionMass',2.17);document.activeElement=node('inspectionMass');
await run("send('messageAcknowledge',{message:'"+privateMessage+"'})");await settled();assert.equal(node('inspectionMass').value,'2.17');assert.equal(document.activeElement.id,'inspectionMass');
run('exitStationMode()');
await run("send('label',{qty:5})");await settled();
for(const [station,qty] of [[1,2],[2,3]]){
 await run("send('labelPickup',{station:"+station+",qty:"+qty+"})");await settled();const movement=run('state.moves[state.moves.length-1].id');
 await run("send('receiveLabels',{movement:"+movement+"})");await settled();await run("send('assignBatch',{station:"+station+",batch:'MIX-MOVER-"+station+"'})");await settled();
 for(let i=0;i<qty;i++){await run("send('fill',{station:"+station+"})");await settled()}
}
for(const mode of ['label','stocker','firstMover','finalMover']){run('enterStation('+JSON.stringify(mode)+')');assert(!/<select\b/.test(node('content').innerHTML),mode+' still has a dropdown');run('exitStationMode()')}
run("enterStation('firstMover')");assert(node('content').innerHTML.includes('Filling station 1'));assert(node('content').innerHTML.includes('Filling station 2'));assert(node('content').innerHTML.includes('Pick up 2 bags'));assert(node('content').innerHTML.includes('Pick up 3 bags'));
await action("doAction('pickupFillingStation',2)");assert.equal(run("q('filled',1).length"),2);assert.equal(run("q('inspectionTransit',2).length"),3);
await action("doAction('receiveFillingStation',2)");assert.equal(run("q('inspectionTransit',2).length"),0);assert.equal(run("q('inspection',2).length"),3);
run('exitStationMode()');view('Delivery dashboard');assert(!/<select\b/.test(node('content').innerHTML));assert(node('content').innerHTML.includes('PUBLIC: collect from Filling 2'));assert(!node('content').innerHTML.includes('PRIVATE: assignment note'));
run('startDeliveryDisplay()');assert(run('projectorMode'));assert.equal(node('personBar').innerHTML,'');assert(!node('content').innerHTML.includes('PRIVATE: assignment note'));assert(node('content').innerHTML.includes('PUBLIC: collect from Filling 2'));
const displayEventCount=run('state.events.length');await run("send('label',{qty:1})");assert.equal(run('state.events.length'),displayEventCount);
run('exitDeliveryDisplay()');
for(const kind of ['people','messages']){const response=await fetch(base+'/api/export?kind='+kind,{headers:{Cookie:cookie}});assert(response.ok);assert((await response.text()).length>50)}

view('Devices');assert(node('content').innerHTML.includes('Device assignment'));assert.equal(run('state.devices.length'),1);
// A remote coordinator assignment waits for the current reading, preserving actor attribution.
run("enterStation('inspection')");val('operator','Before assignment');val('inspectionMass',2.16);document.activeElement=node('inspectionMass');
const currentDevice=run('deviceId'),currentDeviceVersion=run('state.devices[0].version');
const remoteRequest={action:'deviceUpdate',operator:'Remote coordinator',args:{device:currentDevice,expectedVersion:currentDeviceVersion,name:'Inspection tablet',person:mover,mode:'finalMover'},requestId:'remote-'+Date.now(),runId:run('state.runId'),revision:run('state.revision')};
const remoteResponse=await fetch(base+'/api/action',{method:'POST',headers:{Cookie:cookie,'Content-Type':'application/json','X-TrailMix':'1'},body:JSON.stringify(remoteRequest)});assert(remoteResponse.ok);const remote=await remoteResponse.json();
streams[0].listeners.state({data:JSON.stringify(remote.state)});assert.equal(run('stationMode'),'inspection');assert.equal(node('inspectionMass').value,'2.16');assert.equal(node('operator').value,'Before assignment');assert(run('deviceAssignmentPending'));
await action("doAction('weigh',"+run("q('inspection')[0].id")+")");assert.equal(run('stationMode'),'finalMover');assert.equal(node('operator').value,'Mover One');assert.equal(run("state.events[state.events.length-1].operator"),'Before assignment');assert(!run('deviceAssignmentPending'));
// A transient remote storage error retains the original request ID for a safe retry.
const realFetch=context.fetch;let simulatedUnavailable=false;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions'))assert(node('mode').textContent.startsWith('SAVING'));const response=await realFetch(path,options);if((path==='/api/action'||path==='/api/actions')&&!simulatedUnavailable){assert(response.ok);await response.json();simulatedUnavailable=true;return {ok:false,status:503,json:async()=>({error:'Save not confirmed',retryable:true})}}return response};
const labelsBeforeRetry=run('state.labels');await run("send('label',{qty:1})");assert(run('outboxPaused'));const preservedRequest=run('outbox[0].request.requestId');context.fetch=realFetch;
await run('retryOutbox()');assert(!run('outbox.length'));assert.equal(run('state.labels'),labelsBeforeRetry+1);assert(node('toast').textContent.startsWith('Saved'));assert(preservedRequest);
// Delay actual HTTP replies: production controls must advance before any reply.
run('exitStationMode()');
await run("send('label',{qty:10})");await run("send('labelPickup',{station:1,qty:10})");
const rapidMovement=run('state.moves[state.moves.length-1].id');
await run(`send('receiveLabels',{movement:${rapidMovement}})`);
await run("send('assignBatch',{station:1,batch:'RAPID'})");
run("enterStation('fill:1')");val('operator','Rapid operator');
let releaseGate;let gatedCalls=0;
const gate=new Promise(resolve=>{releaseGate=resolve});
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&gatedCalls++===0)await gate;return realFetch(path,options)};
const beforeRapid=run('state.bags.length');
const firstRapid=run("send('fill',{station:1})");
run("doAction('fill',1)");run("doAction('fill',1)");
assert.equal(run('state.bags.length'),beforeRapid+3);assert.equal(run('outbox.length'),3);
assert.equal(run('outboxSending'),true);assert.equal(gatedCalls,1);
assert.equal(JSON.parse(prefs.get('trailmix-desk-v1-outbox')).length,3);
const pickedCount=run("count('filled',1)");val('fillPickup1',pickedCount);
run("doAction('fillPickup',1)");
assert.equal(run("count('filled',1)"),0);assert.equal(run('outbox.length'),4);
// Receipt is available immediately on the same device's next route.
run("send('receive',{source:'inspectionTransit',qty:"+pickedCount+",station:1})");
assert.equal(run("count('inspectionTransit',1)"),0);assert.equal(run('outbox.length'),5);
val('operator','Changed name after entry');
releaseGate();await firstRapid;await settled();context.fetch=realFetch;
assert(!run('outboxPaused'),run('outboxError'));assert.equal(run('outbox.length'),0);assert.equal(gatedCalls,2,'Five queued actions should use two HTTP batches, not five requests');
assert.equal(run('state.bags.length'),beforeRapid+3);
assert(run("state.events.filter(e=>e.action==='fill').slice(-3).every(e=>e.operator==='Rapid operator'&&e.recordedAt&&e.requestId)"));
assert(run("state.bags.slice(-3).every(b=>typeof b.id==='number'&&b.status==='inspection')"));
// Save an old reading while the next weight is already being typed.
run('exitStationMode()');run("enterStation('inspection')");
let releaseWeight;const weightGate=new Promise(resolve=>{releaseWeight=resolve});let weightCalls=0;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&weightCalls++===0)await weightGate;return realFetch(path,options)};
const weighedId=run("q('inspection')[0].id");val('inspectionMass',2.1);
const weightSave=run(`send('weigh',{bag:${weighedId},mass:2.1,unit:'oz',labelOK:true})`);
const nextWeightId=run("q('inspection')[0].id");assert.notEqual(nextWeightId,weighedId);
assert.equal(document.activeElement.id,'inspectionMass');assert.equal(node('inspectionMass').value,'');
val('inspectionMass',2.21);releaseWeight();await weightSave;await settled();context.fetch=realFetch;
assert.equal(node('inspectionMass').value,'2.21');assert.equal(document.activeElement.id,'inspectionMass');assert.equal(run("q('inspection')[0].id"),nextWeightId);
// Three weights advance independently of cloud confirmation.
let releaseWeights;const weightsGate=new Promise(resolve=>{releaseWeights=resolve});let weightsCalls=0;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&weightsCalls++===0)await weightsGate;return realFetch(path,options)};
const weightIds=[];let rapidWeightSave;
for(const mass of [2.15,2.4,1.7]){
 const id=run("q('inspection')[0].id");weightIds.push(id);val('inspectionMass',mass);
 const saving=run(`send('weigh',{bag:${id},mass:${mass},unit:'oz',labelOK:true})`);if(!rapidWeightSave)rapidWeightSave=saving;
}
assert.equal(new Set(weightIds).size,3);assert.equal(run('outbox.length'),3);
releaseWeights();await rapidWeightSave;await settled();context.fetch=realFetch;assert(!run('outboxPaused'),run('outboxError'));
assert.equal(run(`state.bags.find(b=>b.id===${weightIds[0]}).status`),'ready');
for(const id of weightIds.slice(1))assert.equal(run(`state.bags.find(b=>b.id===${id}).status`),'hold');
// Rework advances immediately and retains Enter's next input focus.
await run("send('move',{source:'hold',qty:"+run("count('hold')")+"})");
await run("send('receive',{source:'reworkTransit',qty:"+run("count('reworkTransit')")+"})");
run('exitStationMode()');run("enterStation('rework')");
let releaseRework;const reworkGate=new Promise(resolve=>{releaseRework=resolve});let reworkCalls=0;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&reworkCalls++===0)await reworkGate;return realFetch(path,options)};
const reworkId=run("q('rework')[0].id");val('reworkMass',2.1);
const reworkSave=run(`send('clear',{bag:${reworkId},mass:2.1,unit:'oz'})`);
assert.notEqual(run("q('rework')[0].id"),reworkId);assert.equal(node('reworkMass').value,'');assert.equal(document.activeElement.id,'reworkMass');
val('reworkMass',2.17);releaseRework();await reworkSave;await settled();context.fetch=realFetch;
assert.equal(node('reworkMass').value,'2.17');assert.equal(document.activeElement.id,'reworkMass');
// A lost fill response survives a simulated reload, including its dependent pickup.
run('exitStationMode()');run("enterStation('fill:1')");
let releaseLost;const lostGate=new Promise(resolve=>{releaseLost=resolve});let loseOnce=true;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&loseOnce){loseOnce=false;await lostGate;const r=await realFetch(path,options);assert(r.ok);await r.json();throw Error('Simulated lost confirmation')}return realFetch(path,options)};
const beforeLost=run('state.bags.length');
const lostFill=run("send('fill',{station:1})");val('fillPickup1',run("count('filled',1)"));run("doAction('fillPickup',1)");
releaseLost();await lostFill;await settled();assert(run('outboxPaused'));assert.equal(run('outbox.length'),2);
const lostId=run('outbox[0].request.requestId');context.fetch=realFetch;
run("outbox=JSON.parse(localStorage.getItem(KEY+'-outbox'));outboxPaused=true;outboxRecovering=true;outboxBase=null;state=fresh();connected=false");
await run('refresh(true)');assert(node('content').innerHTML.includes('held'));assert.equal(run('outbox[0].request.requestId'),lostId);
assert.throws(()=>run("enqueueStationAction('label',{qty:1})"),/Confirm recovered saves/);
await run('retryOutbox()');await settled();assert.equal(run('outbox.length'),0);assert(!run('outboxPaused'),run('outboxError'));
assert.equal(run('state.bags.length'),beforeLost+1);assert.equal(run('state.bags[state.bags.length-1].status'),'inspectionTransit');
assert.equal(run(`state.events.filter(e=>e.requestId==='${lostId}').length`),1);
assert.equal(JSON.parse(prefs.get('trailmix-desk-v1-outbox')).length,0);


// A conflicting receipt holds subsequent entries instead of targeting new bags.
let releaseConflict;const conflictGate=new Promise(resolve=>{releaseConflict=resolve});let conflictCalls=0;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&conflictCalls++===0)await conflictGate;return realFetch(path,options)};
const labelsBeforeConflict=run('state.labels'),transitCount=run("count('inspectionTransit',1)");
const heldReceipt=run(`send('receive',{source:'inspectionTransit',station:1,qty:${transitCount}})`);
run("send('label',{qty:1})");
const remoteStateResponse=await realFetch('/api/state'),remoteCurrent=await remoteStateResponse.json();
const otherReceipt=await realFetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-TrailMix':'1'},body:JSON.stringify({requestId:'other-receipt-'+Date.now(),runId:remoteCurrent.runId,revision:remoteCurrent.revision,operator:'Other delivery operator',action:'receive',args:{source:'inspectionTransit',station:1,qty:transitCount}})});assert(otherReceipt.ok);
releaseConflict();await heldReceipt;await settled();context.fetch=realFetch;
assert(run('outboxPaused'));assert(run('outboxRejected'));assert.equal(run('outbox.length'),2);assert.equal(conflictCalls,1);
promptAnswer='Another mover already received these bags. Reconciled the physical delivery; the extra label entry was not performed.';
await run('resolveHeldEntries()');await settled();assert.equal(run('outbox.length'),0);
assert.equal(run('state.labels'),labelsBeforeConflict);
assert.equal(run('state.events[state.events.length-1].data.heldEntries.length'),2);
assert.equal(run('state.events[state.events.length-1].action'),'note');
// Anyone can report a safety issue even while a production save is pending.
let releaseSafety;const safetyGate=new Promise(resolve=>{releaseSafety=resolve});let safetyCalls=0;
context.fetch=async(path,options)=>{if((path==='/api/action'||path==='/api/actions')&&safetyCalls++===0)await safetyGate;return realFetch(path,options)};
const beforeSafetyFill=run('state.bags.length');const heldFill=run("send('fill',{station:1})");
await run("send('safetyReport',{note:'Practice safety stop while a save is pending'})");
assert(run('state.safetyHold'));assert(run('outboxPaused'));
releaseSafety();await heldFill;await settled();context.fetch=realFetch;
assert(run('outboxPaused'));assert.equal(run('outbox.length'),1);
await run("send('safetyRelease',{note:'Practice stop reviewed',safetyPin:'123456'})");await settled();
assert(!run('state.safetyHold'));await run('retryOutbox()');await settled();
assert.equal(run('outbox.length'),0);assert.equal(run('state.bags.length'),beforeSafetyFill+1);

run('exitStationMode()');view('Operators');
const originalSelector=document.querySelectorAll;
document.querySelectorAll=selector=>selector==='#newOperatorAssignments input:checked'?[{value:'rework'},{value:'inspection'}]:[];
val('newOperatorNames','Prepared Operator One\nPrepared Operator Two');await action('addPeopleAhead()');
assert(run("state.people.filter(p=>p.name.startsWith('Prepared Operator')).every(p=>p.assignments.includes('rework')&&p.assignments.includes('inspection'))"));
assert.equal(run("state.people.filter(p=>p.name.startsWith('Prepared Operator')).length"),2);
val('newPersonName','Prepared Single');await action('addPerson()');assert(run("state.people.find(p=>p.name==='Prepared Single').assignments.includes('inspection')"));
document.querySelectorAll=originalSelector;

// Full Labeling uses the queue too: 50 taps require only two HTTP batches.
run('exitStationMode()');view('Labeling');
let releaseFullLabels;const fullLabelGate=new Promise(resolve=>{releaseFullLabels=resolve});let fullLabelRequests=0;
context.fetch=async(path,options)=>{if(path==='/api/actions'&&fullLabelRequests++===0)await fullLabelGate;return realFetch(path,options)};
const labelsBeforeFull=run('state.labels');val('newLabels',1);
const firstFullLabel=run("send('label',{qty:1})");
for(let i=0;i<49;i++){val('newLabels',1);run("doAction('label')")}
assert.equal(run('stationMode'),'');assert.equal(run('state.labels'),labelsBeforeFull+50);assert.equal(run('outbox.length'),50);
releaseFullLabels();await firstFullLabel;await settled();context.fetch=realFetch;
assert.equal(run('outbox.length'),0);assert(!run('outboxPaused'),run('outboxError'));assert.equal(fullLabelRequests,2);
// Full-page delivery and a receipt on another page also advance before reply.
let releaseFullDelivery;const fullDeliveryGate=new Promise(resolve=>{releaseFullDelivery=resolve});let fullDeliveryRequests=0;
context.fetch=async(path,options)=>{if(path==='/api/actions'&&fullDeliveryRequests++===0)await fullDeliveryGate;return realFetch(path,options)};
const emptyBeforeFull=run('state.stations.find(s=>s.id===2).empty');
const firstFullPickup=run("send('labelPickup',{station:2,qty:3})");
const pendingFullMove=run('state.moves[state.moves.length-1].id');view('Filling stations');
run("send('receiveLabels',{movement:"+JSON.stringify(pendingFullMove)+"})");
assert.equal(run('state.stations.find(s=>s.id===2).empty'),emptyBeforeFull+3);assert.equal(run('outbox.length'),2);
releaseFullDelivery();await firstFullPickup;await settled();context.fetch=realFetch;
assert.equal(run('outbox.length'),0);assert(!run('outboxPaused'),run('outboxError'));
// Deletion hides the person and restoration returns them without losing events.
view('Operators');const deleteUiId=run("state.people.find(p=>p.name==='Prepared Single').id");
await action(`removePerson('${deleteUiId}')`);assert(!run(`activePeople().some(p=>p.id==='${deleteUiId}')`));
assert(node('content').innerHTML.includes('Restore operator'));
await run(`send('personRestore',{person:'${deleteUiId}'})`);await settled();assert(run(`activePeople().some(p=>p.id==='${deleteUiId}')`));
// The quick live script works against the existing roster APIs, with no deployment.
context.crypto=require('crypto').webcrypto;const savedConsole=context.console;
context.console={...console,table(){},log(){},error(...args){throw Error(args.join(' '))}};
await run(fs.readFileSync(require('path').join(__dirname,'APPLY_ROSTER_NOW.js'),'utf8'));
context.console=savedConsole;await run('refresh(true)');await settled();
assert(run("['Lucas','Blake','Charlie'].every(name=>state.people.find(p=>p.name===name).assignments.join(',')==='safety')"));


// Personal delivery routing filters both leg and filling station, and still queues.
run('exitStationMode()');
await run("send('personAdd',{name:'Route Operator A',assignments:['delivery:filled:1']})");await settled();
await run("send('personAdd',{name:'Route Operator B',assignments:['delivery:filled:2']})");await settled();
await run("send('label',{qty:3})");await run("send('labelPickup',{station:1,qty:3})");
const routeSupply=run('state.moves[state.moves.length-1].id');await run(`send('receiveLabels',{movement:${routeSupply}})`);
await run("send('assignBatch',{station:1,batch:'ROUTE-QA'})");for(let i=0;i<3;i++)await run("send('fill',{station:1})");await settled();
val('operator','Route Operator A');view('My deliveries');
assert(node('content').innerHTML.includes('Filling station 1'));assert(!node('content').innerHTML.includes('Filling station 2'));
let releaseRoute;const routeGate=new Promise(resolve=>{releaseRoute=resolve});let routeCalls=0;
context.fetch=async(path,options)=>{if(path==='/api/actions'&&routeCalls++===0)await routeGate;return realFetch(path,options)};
const beforeRoute=run("count('inspection',1)");val('routeQty-filled-1',2);
const routeSave=run("pickupRoute('filled',1)");const pendingPackage=run('state.deliveries[state.deliveries.length-1].id');
assert(node('content').innerHTML.includes('Delivered 2 bags'));
val('operator','Route Operator B');node('operator').onchange();assert(!node('content').innerHTML.includes('Filling station 1'));assert(node('content').innerHTML.includes('Filling station 2'));
const queueBeforeWrong=run('outbox.length');await run(`deliverPackage('${pendingPackage}')`);assert.equal(run('outbox.length'),queueBeforeWrong);
val('operator','Route Operator A');run('render()');run(`deliverPackage('${pendingPackage}')`);
assert.equal(run("count('inspection',1)"),beforeRoute+2);
releaseRoute();await routeSave;await settled();context.fetch=realFetch;
assert(!run('outboxPaused'),run('outboxError'));assert.equal(run('outbox.length'),0);assert.equal(run("state.deliveries[state.deliveries.length-1].status"),'delivered');
// An invalid rework value warns locally, preserving the typed value and queue.
run("send('weigh',{bag:"+run("q('inspection')[0].id")+",mass:2.4,unit:'oz',labelOK:true})");await settled();
await run("send('move',{source:'hold',qty:"+run("count('hold')")+"})");await run("send('receive',{source:'reworkTransit',qty:"+run("count('reworkTransit')")+"})");await settled();view('Rework');
const reworkBeforeWarning=run('state.events.length');val('reworkMass',1.9);run("doAction('clear',"+run("q('rework')[0].id")+")");
assert.equal(run('outbox.length'),0);assert.equal(run('state.events.length'),reworkBeforeWarning);assert.equal(node('reworkMass').value,'1.9');assert(node('reworkWarning').textContent.includes('before delivery'));
assert.equal(document.activeElement.id,'reworkMass');val('reworkMass',2.1);assert(run('validateReworkInput(true)'));


// Coordinators can assign from a full station page and from station mode.
run('exitStationMode()');
await run("send('personAdd',{name:'UI Coordinator',assignments:['coordinator']})");await settled();
await run("send('personAdd',{name:'UI Assigned Worker',assignments:[]})");await settled();
const assignedUiWorker=run("state.people.find(p=>p.name==='UI Assigned Worker').id");
val('operator','UI Coordinator');view('Inspection');
assert(node('content').innerHTML.includes('Coordinator · assign an operator'));
val('assignPerson-inspection',assignedUiWorker);node('replaceStation-inspection').checked=false;node('assignDevice-inspection').checked=false;
val('inspectionMass',2.15);document.activeElement=node('inspectionMass');
await run("assignPersonHere('inspection')");await settled();
assert(run("state.people.find(p=>p.name==='UI Assigned Worker').assignments.includes('inspection')"));
assert.equal(node('inspectionMass').value,'2.15');assert.equal(node('operator').value,'UI Coordinator');
run("enterStation('rework')");assert(node('content').innerHTML.includes('data-coordinator'));
val('assignPerson-rework',assignedUiWorker);await run("assignPersonHere('rework')");await settled();
assert(run("state.people.find(p=>p.name==='UI Assigned Worker').assignments.includes('rework')"));
run('exitStationMode()');view('My deliveries');val('assignPerson-delivery',assignedUiWorker);val('assignRole-delivery','delivery:hold:*');
await run("assignPersonHere('delivery')");await settled();
assert(run("state.people.find(p=>p.name==='UI Assigned Worker').assignments.includes('delivery:hold:*')"));
// Config save is available from Settings and is offered for the next run.
view('Settings');val('configName','Saved UI setup');await run('saveConfiguration()');await settled();
assert(run("state.configs.some(c=>c.name==='Saved UI setup')"));assert(node('content').innerHTML.includes('id="newRunConfig"'));assert(node('content').innerHTML.includes('Saved UI setup'));
val('operator','UI Assigned Worker');view('Rework');assert(!node('content').innerHTML.includes('data-coordinator'));
// The shared route picker does not duplicate downstream routes by filling origin.
assert.equal(run("deliveryAssignmentOptions().filter(([id])=>id.startsWith('delivery:hold:')).length"),1);
assert.equal(run("routeTitle('returnReady',1)"),'Rework → Sealing');
assert(!run("routeTitle('hold',1)").includes('Filling'));

const data=run('JSON.stringify(state)');assert(!data.includes('123456'));
console.log(JSON.stringify({result:'PASS',liveHttp:true,domHarness:true,views:10,weights:run('state.bags[0].measurements.length'),events:run('state.events.length'),overweightRework:true,directReworkToSealing:true,authAndOriginChecks:true,stationModes:11,stationActionsSaved:true,stationPreferenceSaved:true,rapidQueue:true,nextWeightPreserved:true,reloadRecovery:true,dependentDeliveryIds:true,visualRendering:'Not tested: browser binary unavailable'}));
})().catch(e=>{console.error(e);process.exit(1)});

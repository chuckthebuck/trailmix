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
async function settled(){for(let i=0;i<200;i++){if(!run('busy')&&!run('polling'))return;await sleep(10)}throw Error('Request did not settle')}
async function action(code){run(code);await settled();assert(!run('pending'),'Unconfirmed request');if(node('toast').textContent&&!node('toast').textContent.startsWith('Saved'))throw Error(node('toast').textContent)}
function val(id,v){node(id).value=String(v)}
function view(p){run(`go(${JSON.stringify(p)})`);assert(node('content').innerHTML.includes('<h1>'));for(const m of node('content').innerHTML.matchAll(/onclick="([^"]*)"/g))new Function(m[1]);return node('content').innerHTML}
(async()=>{
await sleep(100);await settled();assert(node('content').innerHTML.includes('Connect to production'));
val('accessCode','partly-entered');const idleRequests=requestCount;for(let i=0;i<10;i++){for(const tick of intervals)tick();await run('refresh()')}assert.equal(requestCount,idleRequests);assert.equal(node('accessCode').value,'partly-entered');assert.equal(streams.length,0);val('accessCode','test-access');await run('login()');await settled();assert(run('connected'));assert.equal(streams.length,1);const liveRequests=requestCount;for(let i=0;i<10;i++)for(const tick of intervals)tick();assert.equal(requestCount,liveRequests);val('operator','DOM flow test');
view('Labeling');val('newLabels',4);await action("doAction('label')");val('dest',1);val('labelPickup',4);await action("doAction('labelPickup')");
view('Filling stations');const movement=run('state.moves[0].id');await action(`doAction('receiveLabels',${movement})`);val('mixBatch1','MIX-DOM-01');await action("doAction('assignBatch',1)");await action("doAction('fill',1)");const bag=run('state.bags[0].id');val('fillPickup1',1);await action("doAction('fillPickup',1)");
let html=view('Inspection');assert(html.includes('Receive 1 bags'));await action("doAction('receive','inspectionTransit')");assert.equal(document.activeElement.id,'inspectionMass');assert(!node('content').innerHTML.includes('id="labelOK"'));val('inspectionMass',1.9);run(`toggleLabelFailure(${bag})`);assert.equal(run('labelFailBag'),bag);assert.equal(node('inspectionMass').value,'1.9');assert.equal(document.activeElement.id,'inspectionMass');run(`toggleLabelFailure(${bag})`);assert.equal(run('labelFailBag'),null);val('inspectionMass',1.9);await action(`doAction('weigh',${bag})`);assert.equal(run('state.bags[0].status'),'hold');
run("enterStation('inspection')");assert(node('content').innerHTML.includes('stationReworkPickup'));val('stationReworkPickup',1);await action("doAction('move','hold','','stationReworkPickup')");run('exitStationMode()');html=view('Rework');assert(html.includes('Receive 1 bags'));await action("doAction('receive','reworkTransit')");val('reworkMass',1.6);await action(`doAction('clear',${bag})`);assert.equal(run('state.bags[0].status'),'returnReady');
run("enterStation('rework')");assert(node('content').innerHTML.includes('stationReturnPickup'));val('stationReturnPickup',1);await action("doAction('move','returnReady','','stationReturnPickup')");run('exitStationMode()');view('Inspection');await action("doAction('receive','returnTransit')");val('inspectionMass',1.62);await action(`doAction('weigh',${bag})`);assert.equal(run('state.bags[0].status'),'ready');
run("enterStation('inspection')");assert(node('content').innerHTML.includes('stationSealPickup'));val('stationSealPickup',1);await action("doAction('move','ready','','stationSealPickup')");run('exitStationMode()');view('Sealing');await action("doAction('receive','sealTransit')");await action(`doAction('seal',${bag})`);
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
val('inspectionMass',1.63);run('toggleLabelFailure('+secondBag+')');
await action("doAction('weigh',"+secondBag+")");assert.equal(run('state.bags[1].status'),'discarded');assert.equal(run('state.events[state.events.length-1].data.labelOK'),false);assert.equal(run('labelFailBag'),null);
run('exitStationMode()');


// Keyboard Enter submits exactly once and focuses the empty input for the next bag.
run("enterStation('fill:1')");await action("doAction('fill',1)");await action("doAction('fill',1)");
await run("send('move',{source:'filled',station:1,qty:2})");await settled();
await run("send('receive',{source:'inspectionTransit',qty:2})");await settled();
run("enterStation('inspection')");const queued=JSON.parse(run("JSON.stringify(q('inspection'))"));assert.equal(queued.length,2);
val('inspectionMass',1.61);const firstWeightInput=node('inspectionMass');document.activeElement=firstWeightInput;
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
run("enterStation('inspection')");val('inspectionMass',1.67);document.activeElement=node('inspectionMass');
await run("send('messageAcknowledge',{message:'"+privateMessage+"'})");await settled();assert.equal(node('inspectionMass').value,'1.67');assert.equal(document.activeElement.id,'inspectionMass');
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
run("enterStation('inspection')");val('operator','Before assignment');val('inspectionMass',1.66);document.activeElement=node('inspectionMass');
const currentDevice=run('deviceId'),currentDeviceVersion=run('state.devices[0].version');
const remoteRequest={action:'deviceUpdate',operator:'Remote coordinator',args:{device:currentDevice,expectedVersion:currentDeviceVersion,name:'Inspection tablet',person:mover,mode:'finalMover'},requestId:'remote-'+Date.now(),runId:run('state.runId'),revision:run('state.revision')};
const remoteResponse=await fetch(base+'/api/action',{method:'POST',headers:{Cookie:cookie,'Content-Type':'application/json','X-TrailMix':'1'},body:JSON.stringify(remoteRequest)});assert(remoteResponse.ok);const remote=await remoteResponse.json();
streams[0].listeners.state({data:JSON.stringify(remote.state)});assert.equal(run('stationMode'),'inspection');assert.equal(node('inspectionMass').value,'1.66');assert.equal(node('operator').value,'Before assignment');assert(run('deviceAssignmentPending'));
await action("doAction('weigh',"+run("q('inspection')[0].id")+")");assert.equal(run('stationMode'),'finalMover');assert.equal(node('operator').value,'Mover One');assert.equal(run("state.events[state.events.length-1].operator"),'Before assignment');assert(!run('deviceAssignmentPending'));
// A transient remote storage error retains the original request ID for a safe retry.
const realFetch=context.fetch;let simulatedUnavailable=false;
context.fetch=async(path,options)=>{const response=await realFetch(path,options);if(path==='/api/action'&&!simulatedUnavailable){assert(response.ok);await response.json();simulatedUnavailable=true;return {ok:false,status:503,json:async()=>({error:'Save not confirmed',retryable:true})}}return response};
const labelsBeforeRetry=run('state.labels');await run("send('label',{qty:1})");assert(run('pending'));const preservedRequest=run('pending.requestId');context.fetch=realFetch;
await run('retryPending()');assert(!run('pending'));assert.equal(run('state.labels'),labelsBeforeRetry+1);assert(node('toast').textContent.includes('no duplicate'));assert(preservedRequest);
const data=run('JSON.stringify(state)');assert(!data.includes('123456'));
console.log(JSON.stringify({result:'PASS',liveHttp:true,domHarness:true,views:10,weights:run('state.bags[0].measurements.length'),events:run('state.events.length'),overweightRework:true,independentReinspection:true,authAndOriginChecks:true,stationModes:11,stationActionsSaved:true,stationPreferenceSaved:true,visualRendering:'Not tested: browser binary unavailable'}));
})().catch(e=>{console.error(e);process.exit(1)});

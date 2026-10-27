const fs=require('fs'),vm=require('vm'),assert=require('assert');
const prefs=new Map(),listeners=[];
const fixed=new Map(),elements=[],badges=[];let contentElements=[],activeDialog=false,context;
function element(tag,attrs={}){return {tagName:tag.toUpperCase(),id:attrs.id||'',type:attrs.type||'',value:attrs.value||'',hidden:!!attrs.hidden,disabled:'disabled'in attrs,isContentEditable:false,style:{},attrs:{...attrs},children:[],clicks:0,closest(sel){if(sel==='[data-dashboard]')return 'data-dashboard'in this.attrs?this:null;if(sel==='label')return this.parentLabel||(this.parentLabel=element('label'));if(sel==='details')return this.detail||null;return null},getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},appendChild(v){this.children.push(v);badges.push(v)},remove(){const i=badges.indexOf(this);if(i>=0)badges.splice(i,1)},focus(){document.activeElement=this},select(){this.selected=true},click(){this.clicks++}}}
function parse(html){const result=[];for(const m of html.matchAll(/<(button|input|select|summary)\b([^>]*)>/g)){const attrs={};for(const a of m[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=a[2]??'';result.push(element(m[1],attrs))}return result}
for(const id of ['operator','exitStationButton','stationModeButton','keyBindingsButton','bindingList','bindingStatus','keyBindingsDialog','batchTitle','mode','nav','toast','content','stationDialog'])fixed.set(id,element(id==='operator'?'input':'div',{id}));
const safety=element('button',{onclick:'reportSafety()'});fixed.get('exitStationButton').tagName='BUTTON';fixed.get('exitStationButton').attrs.onclick='exitStationMode()';fixed.get('keyBindingsButton').tagName='BUTTON';fixed.get('keyBindingsButton').attrs.onclick='openKeyBindings()';
const document={body:{classList:{toggle(){}}},activeElement:{tagName:'BODY'},addEventListener(type,fn){if(type==='keydown')listeners.push(fn)},getElementById(id){return fixed.get(id)||contentElements.find(e=>e.id===id)||null},querySelector(){return activeDialog?{}:null},querySelectorAll(sel){if(sel==='.station-key')return [...badges];if(sel==='[data-station-keyboard]')return [safety,...fixed.values(),...contentElements].filter(e=>e.attrs?.['data-station-keyboard']);if(sel.includes('.topbar button'))return [safety,fixed.get('operator'),fixed.get('keyBindingsButton'),fixed.get('exitStationButton'),...contentElements];return []},createElement:tag=>element(tag)};
const content=fixed.get('content');Object.defineProperty(content,'innerHTML',{get(){return this.html||''},set(v){this.html=v;contentElements=parse(v)}});
context=vm.createContext({document,window:{addEventListener(type,fn){if(type==='keydown')listeners.push(fn)}},confirm:()=>true,localStorage:{getItem(k){return prefs.get(k)||''},setItem(k,v){prefs.set(k,v)},removeItem(k){prefs.delete(k)}},fetch:()=>new Promise(()=>{}),AbortSignal,Date,Math,Number,JSON,Error,URL,setTimeout,clearTimeout,setInterval(){}});
const run=js=>vm.runInContext(js,context);
run(fs.readFileSync(require('path').join(__dirname,'trailmix.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1]);
fixed.get('operator').value='Keyboard test';
run(`state.limits.confirmed=true;state.runId='test';state.labels=12;state.stations.forEach(s=>{s.empty=5;s.batch='MIX-KEY'});state.moves=[{id:100,station:1,qty:3,received:false},{id:101,station:1,qty:4,received:false}];let kbId=1;for(const status of ['filled','inspectionTransit','returnTransit','inspection','hold','ready','reworkTransit','rework','returnReady','sealTransit','sealing','sealed','finalTransit','final'])state.bags.push({id:kbId++,station:1,status,created:1,updated:1,holdAt:1,measurements:[{oz:2.1}],sealFails:0})`);
function event(key,options={}){return {key,altKey:false,shiftKey:false,ctrlKey:false,metaKey:false,repeat:false,isComposing:false,prevented:false,stopped:false,preventDefault(){this.prevented=true},stopImmediatePropagation(){this.stopped=true},...options}}
let checked=0;
for(const mode of ['label','stocker','fill:1','inspection','rework','sealing','final','firstMover','finalMover']){
 run(`stationMode=${JSON.stringify(mode)};render()`);
 const registry=run('stationKeys');const targets=[safety,fixed.get('operator'),fixed.get('keyBindingsButton'),fixed.get('exitStationButton'),...contentElements].filter(e=>!e.hidden&&!e.closest('[data-dashboard]'));
 assert.equal(registry.size,targets.length,mode+' missing a shortcut');
 for(const [key,target]of registry){
  assert(target.getAttribute('aria-keyshortcuts'),mode+' missing accessible key');const badgeParent=['INPUT','SELECT'].includes(target.tagName)?target.closest('label'):target;assert(badgeParent.children.some(b=>b.className==='station-key'),mode+' missing visible label');
  document.activeElement=key==='enter'?contentElements.find(e=>['inspectionMass','reworkMass'].includes(e.id)):{tagName:'BODY'};
  const before=target.clicks;
  const e=event(key.replace('shift+',''),{altKey:key!=='enter',shiftKey:key.startsWith('shift+')});context.testEvent=e;run('stationKeyboard(testEvent)');assert(e.prevented&&e.stopped);
  if(target.disabled)assert.equal(target.clicks,before);else if(['INPUT','SELECT','TEXTAREA'].includes(target.tagName))assert.equal(document.activeElement,target);else assert.equal(target.clicks,before+1);
  if(key!=='enter'){document.activeElement={tagName:'BODY'};const simple=event(key.replace('shift+',''),{shiftKey:key.startsWith('shift+')});for(const listener of listeners){listener(simple);if(simple.stopped)break}assert(simple.prevented,'Plain shortcut not dispatched: '+key)}
  checked++;
 }
}
run("stationMode='inspection';render()");const input=contentElements.find(e=>e.id==='inspectionMass');document.activeElement=input;
for(const key of ['1','.','e','+','-','Tab','ArrowLeft']){const e=event(key);context.testEvent=e;run('stationKeyboard(testEvent)');assert(!e.prevented,'Typing was intercepted: '+key)}
const submit=run("stationKeys.get('enter')"),before=submit.clicks;
let e=event('Enter',{repeat:true});context.testEvent=e;run('stationKeyboard(testEvent)');assert(e.prevented);assert.equal(submit.clicks,before);
e=event('Enter');context.testEvent=e;run('stationKeyboard(testEvent)');assert.equal(submit.clicks,before+1);
activeDialog=true;e=event('x',{altKey:true});context.testEvent=e;run('stationKeyboard(testEvent)');assert(!e.prevented);activeDialog=false;

// Shifted keyboard symbols are resolved from their physical digit key.
run("stationMode='fill:1';render()");
const fill=run("stationControls.find(c=>c.action==='fill')"),fillId=fill.id;
context.fillControlId=fillId;
run("setKeyBinding(fillControlId,'z')");
assert.equal(run("stationKeys.get('z')"),fill.el);assert(!run("stationKeys.has('f')"));
assert.equal(JSON.parse(prefs.get('trailmix-desk-v1-keybindings'))['fill:1'][fillId],'z');
run("render()");assert(run("stationKeys.get('z').getAttribute('onclick').includes(\"'fill'\")"));
run("stationMode='fill:2';render()");assert(!run("stationKeys.has('z')"));
run("stationMode='fill:1';render()");assert(run("stationKeys.has('z')"));
assert.throws(()=>run("setKeyBinding(fillControlId,'q')"),/already used/);
run("setKeyBinding(fillControlId,'alt+shift+2')");const remapped=run("stationKeys.get('alt+shift+2')");document.activeElement={tagName:'BODY'};
context.testEvent=event('@',{code:'Digit2',altKey:true,shiftKey:true});run('stationKeyboard(testEvent)');assert.equal(remapped.clicks,1);
run('clearKeyBinding(fillControlId)');assert(!run("stationKeys.has('alt+shift+2')"));
run('resetStationBindings()');assert(run("stationKeys.has('f')"));
// Rebinding is captured from a keyboard event, not a typed command string.
run('beginKeyBinding(fillControlId)');context.testEvent=event('j');run('stationKeyboard(testEvent)');assert(run("stationKeys.has('j')"));assert.equal(run('bindingCapture'),'');
run('resetStationBindings()');
// Number keys in quantity/name fields remain ordinary input, even if mapped elsewhere.
document.activeElement=fixed.get('operator');context.testEvent=event('f');run('stationKeyboard(testEvent)');assert(!context.testEvent.prevented);
context.testEvent=event('f',{altKey:true});run('stationKeyboard(testEvent)');assert(context.testEvent.prevented);

run("stationMode='';setupStationKeyboard()");assert.equal(run('stationKeys.size'),0);assert.equal(badges.length,0);

// Station picker defaults to the screen being viewed rather than Labeling.
run('connected=true');fixed.set('stationChoice',element('select',{id:'stationChoice'}));fixed.get('stationDialog').showModal=()=>{};
for(const [screen,expected]of [['Inspection','inspection'],['Rework','rework'],['Sealing','sealing'],['Final inspection','final'],['Labeling','label'],['My deliveries','delivery']]){
 context.screenName=screen;run('page=screenName;openStationPicker()');assert.equal(fixed.get('stationChoice').value,expected);
}
// Route descriptions and binding IDs use action, leg and station, not bag counts.
run(`state.people=[{id:'kb-person',name:'Keyboard test',assignments:['delivery:filled:1','delivery:filled:2','delivery:hold:2'],status:'working'}];state.deliveries=[{id:'package-one',source:'filled',station:2,status:'transit',personId:'kb-person',pickedBy:'Keyboard test',pickedAt:1,remainingBagIds:[2],qty:1}];stationMode='delivery';render()`);
let route=run("stationControls.find(c=>c.id==='action:routePickup:filled:1')");
assert(route&&route.title.includes('Filling station 1')&&route.title.includes('inspection'));
assert(run("stationControls.find(c=>c.id==='action:routePickup:filled:2').title.includes('Filling station 2')"));
const stableTitle=route.title;run("setKeyBinding('action:routePickup:filled:1','alt+shift+9')");
run("state.bags.push({id:999,station:1,status:'filled',updated:2,measurements:[]});render()");
route=run("stationControls.find(c=>c.id==='action:routePickup:filled:1')");assert.equal(route.title,stableTitle);assert.equal(route.key,'alt+shift+9');
run("setKeyBinding('action:routeDeliver:filled:2','alt+shift+8');state.deliveries[0].id='package-two';render()");
const delivered=run("stationControls.find(c=>c.id==='action:routeDeliver:filled:2')");assert.equal(delivered.key,'alt+shift+8');assert(delivered.title.includes('Filling station 2'));assert(delivered.el.getAttribute('onclick').includes('package-two'));
console.log(JSON.stringify({result:'PASS',shortcutControls:checked,stationLayouts:9,typingProtected:true,repeatProtected:true,disabledProtected:true,modalProtected:true,badgesAndAria:true,plainKeys:true,customBindings:true,persistence:true,registeredListenerDispatch:true,shiftedDigitHandling:true}));

const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'trailmix.html'),'utf8');
const context=vm.createContext({JSON,Error});
const begin=source.indexOf('let confirmedServerState=null;'),end=source.indexOf('async function api(path,body)',begin);
vm.runInContext(source.slice(begin,end),context);
const run=s=>vm.runInContext(s,context),fixture={runId:'run-a',started:10,revision:0,labels:0,bags:[],stations:[{id:1,empty:1}],events:[],devices:[],people:[],moves:[],messages:[],deliveries:[],configs:[]};
context.fixture=fixture;run('acceptServerState(fixture)');
context.patch={runId:'run-a',baseRevision:0,revision:1,core:{runId:'run-a',revision:1,labels:1},entities:{bags:{upsert:[{id:1,status:'filled'}],remove:[]}},eventsAppend:[{id:'event-1',action:'fill'}]};
let next=run('mergeServerPatch(patch)');assert.equal(next.bags[0].status,'filled');assert.equal(next.events.length,1);
// HTTP acknowledgement after the identical SSE patch must not duplicate it.
next=run('mergeServerPatch(patch)');assert.equal(next.bags.length,1);assert.equal(next.events.length,1);
next.bags[0].status='optimistic-local-change';assert.equal(run('confirmedServerState.bags[0].status'),'filled');
context.patch2={runId:'run-a',baseRevision:1,revision:2,core:{revision:2},entities:{bags:{upsert:[{id:1,status:'inspection'},{id:2,status:'filled'}],remove:[]}},eventsAppend:[{id:'event-2'}]};
next=run('mergeServerPatch(patch2)');assert.equal(next.bags.length,2);assert.equal(next.bags[0].status,'inspection');assert.equal(next.events.length,2);
// An older acknowledgement cannot undo a later confirmed update.
next=run('mergeServerPatch(patch)');assert.equal(next.revision,2);assert.equal(next.bags[0].status,'inspection');
assert.throws(()=>run('mergeServerPatch({...patch,baseRevision:5,revision:6})'),/missed/);
assert.throws(()=>run("mergeServerPatch({...patch,runId:'other'})"),/missed/);
next=run("mergeServerPatch({reset:{...fixture,runId:'run-b',started:20}})");assert.equal(next.runId,'run-b');assert.equal(next.bags.length,0);
next=run('acceptServerState(fixture)');assert.equal(next.runId,'run-b');
// Removal and string/numeric ID equivalence use the same reconciliation rule.
run('confirmedServerState={...fixture,revision:2,bags:[{id:1},{id:2}]}');
next=run("mergeServerPatch({runId:'run-a',baseRevision:2,revision:3,core:{revision:3},entities:{bags:{upsert:[{id:'2',status:'done'}],remove:['1']}},eventsAppend:[]})");
assert.equal(next.bags.length,1);assert.equal(next.bags[0].status,'done');
console.log(JSON.stringify({result:'PASS',revisionedPatches:true,duplicateAcknowledgements:true,missedUpdateDetection:true,resetProtection:true,localPreviewIsolation:true}));

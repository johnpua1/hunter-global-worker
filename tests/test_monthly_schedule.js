const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('bridge/Gateway.gs', 'utf8');
function scenario({committed=false, dispatch='started', readError=false, date='2026-11-02'}={}) {
  const c = vm.createContext({}); vm.runInContext(source,c);
  const scheduled=[], events=[]; let launches=0;
  c.PropertiesService={getScriptProperties:()=>({getProperty:k=>k==='HUNTER_MONTH_EXPECTED'?'2026-11':'root'})};
  c.hunterMonthlyRetry_=m=>{scheduled.push(m);events.push('retry');return {expectedMonth:m}};
  c.installMonthlyTriggerAt=(d,m)=>{scheduled.push(m); return {expectedMonth:m}};
  c.DriveApp={getFolderById:()=>{events.push('read');if(readError)throw Error('read failed');return {}}};
  const blob = doc=>({getBlob:()=>({getDataAsString:()=>JSON.stringify(doc)})});
  c.bridgeFile_=()=>committed?blob({month_file:'MONTH_2026-11',hk_month_file:'HK_MONTH_2026-11'}):null;
  c.bridgeFolder_=()=>({getFilesByName:()=>({hasNext:()=>true,next:()=>blob({last_completed_date:date})})});
  c.runMonthly=()=>{launches++;if(dispatch==='failed')throw Error('dispatch failed');return {ok:true,skipped:dispatch==='running'}};
  let error;try{c.monthlyV2()}catch(e){error=e}
  return {scheduled,events,launches,error};
}
for(const dispatch of ['started','running','failed']) {
  const r=scenario({dispatch}); assert.deepEqual(r.scheduled,['2026-11']);assert.equal(r.launches,1);
  assert.equal(Boolean(r.error),dispatch==='failed');assert.deepEqual(r.events,['retry','read']);
}
let r=scenario({readError:true});assert.deepEqual(r.scheduled,['2026-11']);assert.equal(r.launches,0);assert.ok(r.error);
r=scenario({date:'2026-10-30'});assert.deepEqual(r.scheduled,['2026-11']);assert.equal(r.launches,0);
// A completed month may be acknowledged even when DAILY has moved ahead.
r=scenario({committed:true,date:'2026-12-01'});assert.deepEqual(r.scheduled,['2026-11','2026-12']);assert.equal(r.launches,0);
// Failed trigger creation must preserve the previously installed trigger.
const c=vm.createContext({});vm.runInContext(source,c);let deleted=0;
c.ScriptApp={AuthMode:{FULL:'full'},requireScopes(){},getProjectTriggers:()=>[{getHandlerFunction:()=> 'monthlyV2'}],deleteTrigger(){deleted++},newTrigger:()=>({timeBased:()=>({at:()=>({create(){throw Error('quota')}})})})};
assert.throws(()=>c.installMonthlyTriggerAt(new Date(),'2026-11'),/quota/);assert.equal(deleted,0);
console.log('Monthly scheduling recovery tests passed');

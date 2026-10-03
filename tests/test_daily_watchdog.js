const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('bridge/Gateway.gs','utf8') + '\n' + fs.readFileSync('bridge/Watchdog.gs','utf8');
function setup(hour=19) {
  const data = new Map(), launches = [], alerts=[];
  const ctx=vm.createContext({console:{log(){},error(){}}}); vm.runInContext(source,ctx);
  ctx.PropertiesService={getScriptProperties:()=>({getProperty:k=>data.get(k)||null,setProperty:(k,v)=>data.set(k,v),deleteProperty:k=>data.delete(k)})};
  ctx.Utilities={formatDate:(_,tz,fmt)=>{assert.equal(tz,'Asia/Kuala_Lumpur');return fmt==='HH'?String(hour):'2026-10-03';}};
  ctx.LockService={getScriptLock:()=>({tryLock:()=>true,releaseLock(){}})};
  ctx.ScriptApp={getOAuthToken:()=> 'test'};
  ctx.UrlFetchApp={fetch:(_,args)=>{alerts.push(JSON.parse(args.payload));return {getResponseCode:()=>200};}};
  ctx.status=()=>({running:false});
  ctx.listHunterDailyTriggers=()=>({counts:{dailyUS:1,dailyHK:1},timeZone:'Asia/Kuala_Lumpur'});
  ctx.listHunterDailyWatchdog=()=>({count:1});
  ctx.hunterCloudRequest_=(method,path)=>{
    if(method==='post') {launches.push(path);return {name:'operation',metadata:{name:path.replace(':run','/executions/test')}};}
    return {completionTime:'2026-10-03T12:00:00Z',conditions:[{type:'Completed',state:'CONDITION_SUCCEEDED'}]};
  };
  return {ctx,data,launches,alerts};
}
let cases=0;
for(const [hour,count] of [[6,0],[7,1],[18,1],[19,2],[23,2]]) {
  const {ctx,launches}=setup(hour);ctx.hunterDailyWatchdog();assert.equal(launches.length,count);cases++;
}
{
  const {ctx,launches,data}=setup();ctx.hunterDailyWatchdog();ctx.hunterDailyWatchdog();
  assert.equal(launches.length,2);
  assert.equal(JSON.parse(data.get('HUNTER_COMPENSATION_hunter-us-daily')).count,1);cases++;
}
{
  const {ctx,launches}=setup();ctx.status=()=>({running:true});ctx.hunterDailyWatchdog();assert.equal(launches.length,0);cases++;
}
{
  const {ctx,launches,alerts}=setup();const original=ctx.hunterCloudRequest_;
  ctx.hunterCloudRequest_=(method,path)=>method==='get'?{completionTime:'done',conditions:[{type:'Completed',state:'CONDITION_FAILED'}]}:original(method,path);
  ctx.hunterDailyWatchdog();ctx.hunterDailyWatchdog();
  assert.throws(()=>ctx.hunterDailyWatchdog(),/HUNTER_DAILY_WATCHDOG_FAILED/);
  assert.equal(launches.length,4);assert.ok(alerts.length>=3);cases++;
}
{
  const {ctx,data,launches}=setup();data.set('HUNTER_LAUNCH_hunter-us-daily',JSON.stringify({mytDate:'2026-10-03',operation:null}));
  assert.throws(()=>ctx.hunterDailyWatchdog(),/HUNTER_DAILY_WATCHDOG_FAILED/);
  assert.equal(launches.length,1);assert.match(launches[0],/hunter-hk-daily/);cases++;
}
{
  const {ctx,data}=setup();data.set('HUNTER_COMPENSATION_hunter-us-daily',JSON.stringify({mytDate:'2026-10-02',count:2}));
  ctx.hunterDailyWatchdog();assert.equal(JSON.parse(data.get('HUNTER_COMPENSATION_hunter-us-daily')).count,1);cases++;
}
{
  const {ctx,alerts}=setup(6);ctx.listHunterDailyTriggers=()=>({counts:{dailyUS:0,dailyHK:1},timeZone:'UTC'});
  assert.throws(()=>ctx.hunterDailyWatchdog(),/HUNTER_DAILY_WATCHDOG_FAILED/);
  assert.match(alerts[0].entries[0].jsonPayload.reason,/DAILY_TRIGGER_DRIFT/);cases++;
}
{
  const {ctx}=setup();let triggers=[],created=0;
  ctx.ScriptApp.getProjectTriggers=()=>triggers;
  ctx.ScriptApp.newTrigger=name=>({timeBased(){return this;},everyHours(n){assert.equal(n,1);return this;},inTimezone(tz){assert.equal(tz,'Asia/Kuala_Lumpur');return this;},create(){created++;triggers.push({getHandlerFunction:()=>name,getUniqueId:()=> 'watchdog-id'});}});
  ctx.Session={getScriptTimeZone:()=> 'Asia/Kuala_Lumpur'};
  vm.runInContext(source.match(/^function listHunterDailyWatchdog\(\) \{[\s\S]*?^\}/m)[0],ctx);
  assert.equal(ctx.installHunterDailyWatchdog().count,1);assert.equal(ctx.installHunterDailyWatchdog().count,1);assert.equal(created,1);cases++;
}
console.log('daily watchdog: '+cases+' MYT windows, bounded retry, duplicate prevention and drift scenarios PASS');

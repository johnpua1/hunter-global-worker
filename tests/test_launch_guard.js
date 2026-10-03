const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('bridge/Gateway.gs', 'utf8');
const resource = 'projects/rgs-hunter-global/locations/us-central1/jobs/hunter-us-daily';
const execution = resource + '/executions/us-123';
const operation = 'projects/rgs-hunter-global/locations/us-central1/operations/op-123';
const key = 'HUNTER_LAUNCH_hunter-us-daily';
const terminal = state => ({name: execution, completionTime: '2026-10-03T01:00:00Z',
  conditions: [{type: 'Completed', state: 'CONDITION_' + state}]});

function setup() {
  const data = new Map(), events = [];
  let held = false;
  const ctx = vm.createContext({console: {log() {}, error() {}}});
  vm.runInContext(source, ctx);
  ctx.PropertiesService = {getScriptProperties: () => ({
    getProperty: key => data.get(key) || null,
    setProperty: (key, value) => {data.set(key, value); events.push(['save', key]);},
    deleteProperty: key => data.delete(key)
  })};
  ctx.LockService = {getScriptLock: () => ({
    tryLock() {if (held) return false; held = true; events.push(['lock']); return true;},
    releaseLock() {assert.ok(held); held = false; events.push(['unlock']);}
  })};
  ctx.ScriptApp = {getOAuthToken: () => 'test-token'};
  ctx.Utilities = {formatDate: () => '2026-10-03', sleep() {}};
  ctx.UrlFetchApp = {fetch: () => ({getResponseCode: () => 200})};
  ctx.status = () => ({running: false, executions: []});
  ctx.hunterCloudRequest_ = (method, path) => {
    events.push([method, path]);
    if (method === 'post') return {name: operation, metadata: {name: execution}};
    if (path === execution) return terminal('SUCCEEDED');
    throw new Error('UNEXPECTED_REQUEST');
  };
  return {ctx, data, events, locked: () => held};
}
let scenarios = 0;
{
  const {ctx, data, events, locked} = setup();
  assert.equal(ctx.runUS().skipped, false);
  assert.equal(JSON.parse(data.get(key)).execution, execution);
  assert.ok(events.findIndex(x => x[0] === 'save') < events.findIndex(x => x[0] === 'post'));
  assert.equal(ctx.runUS().reason, 'ALREADY_SUCCEEDED_TODAY');
  assert.equal(events.filter(x => x[0] === 'post').length, 1);
  assert.equal(locked(), false);
  scenarios++;
}
{
  const {ctx, events} = setup();
  ctx.status = () => ({running: true});
  assert.equal(ctx.runUS().reason, 'ALREADY_RUNNING');
  assert.equal(events.some(x => x[0] === 'post'), false);
  scenarios++;
}
{
  const {ctx, events} = setup();
  ctx.LockService = {getScriptLock: () => ({tryLock: () => false, releaseLock() {assert.fail();}})};
  ctx.status = () => assert.fail('must acquire lock before checking status');
  assert.throws(() => ctx.runUS(), /LAUNCH_LOCK_BUSY/);
  assert.equal(events.some(x => x[0] === 'post'), false);
  scenarios++;
}
{
  const {ctx, data, locked} = setup();
  let posts = 0;
  ctx.hunterCloudRequest_ = () => {posts++; throw new Error('NETWORK_TIMEOUT');};
  assert.throws(() => ctx.runUS(), /NETWORK_TIMEOUT/);
  assert.equal(JSON.parse(data.get(key)).operation, null);
  assert.equal(locked(), false);
  assert.throws(() => ctx.runUS(), /LAUNCH_OUTCOME_UNKNOWN/);
  assert.equal(posts, 1);
  scenarios++;
}
{
  const {ctx, data} = setup();
  ctx.hunterCloudRequest_ = () => {throw Object.assign(new Error('CLOUD_RUN_HTTP_403'), {httpStatus: 403});};
  assert.throws(() => ctx.runUS(), /403/);
  assert.equal(data.has(key), false);
  scenarios++;
}
for (const state of ['FAILED', 'SUCCEEDED']) {
  const {ctx, data, events} = setup();
  data.set(key, JSON.stringify({operation, execution, mytDate: '2026-10-02'}));
  const request = ctx.hunterCloudRequest_;
  ctx.hunterCloudRequest_ = (method, path) => method === 'get' ? terminal(state) : request(method, path);
  assert.equal(ctx.runUS().skipped, false);
  assert.equal(events.filter(x => x[0] === 'post').length, 1);
  scenarios++;
}
{
  const {ctx, data} = setup();
  data.set(key, JSON.stringify({operation, mytDate: '2026-10-03'}));
  ctx.hunterCloudRequest_ = (method, path) => {
    assert.equal(method, 'get'); assert.equal(path, operation); return {done: false};
  };
  assert.equal(ctx.runUS().reason, 'LAUNCH_PENDING');
  scenarios++;
}
{
  const {ctx, data} = setup();
  data.set(key, JSON.stringify({operation, mytDate: '2026-10-03'}));
  ctx.hunterCloudRequest_ = (method, path) => {
    assert.equal(method, 'get');
    return path === operation ? {done: true, response: {name: execution}} : terminal('SUCCEEDED');
  };
  assert.equal(ctx.runUS().reason, 'ALREADY_SUCCEEDED_TODAY');
  assert.equal(JSON.parse(data.get(key)).execution, execution);
  scenarios++;
}
{
  const {ctx, data} = setup();
  data.set(key, JSON.stringify({operation, execution, mytDate: '2026-10-02'}));
  ctx.hunterCloudRequest_ = () => ({name: execution});
  assert.equal(ctx.runUS().reason, 'ALREADY_RUNNING');
  scenarios++;
}
{
  const {ctx, data} = setup();
  data.set(key, JSON.stringify({operation, mytDate: '2026-10-03'}));
  const request = ctx.hunterCloudRequest_;
  ctx.hunterCloudRequest_ = (method, path) => method === 'get' ? {done: true, error: {code: 7}} : request(method, path);
  assert.equal(ctx.runUS().skipped, false);
  scenarios++;
}
{
  const {ctx, data} = setup();
  data.set(key, '{broken');
  assert.throws(() => ctx.runUS());
  assert.equal(data.get(key), '{broken');
  scenarios++;
}
// Exercise the actual HTTP retry behavior, including ambiguous POST responses.
for (const method of ['get', 'post']) {
  const ctx = vm.createContext({});
  vm.runInContext(source, ctx);
  let requests = 0, sleeps = 0;
  ctx.ScriptApp = {getOAuthToken: () => 'test'};
  ctx.Utilities = {sleep: () => sleeps++};
  ctx.UrlFetchApp = {fetch() {requests++; throw new Error('TIMEOUT');}};
  assert.throws(() => ctx.hunterCloudRequest_(method, resource, {}), /TIMEOUT/);
  assert.equal(requests, method === 'get' ? 3 : 1);
  assert.equal(sleeps, method === 'get' ? 2 : 0);
  scenarios++;
}
{
  const {ctx} = setup();
  // Reinstall the real status function, leaving service mocks intact.
  vm.runInContext(source.match(/^function status\(job\) \{[\s\S]*?^\}/m)[0], ctx);
  const paths = [];
  ctx.hunterCloudRequest_ = (_, path) => {
    paths.push(path);
    return paths.length === 1 ? {executions: Array(100).fill(terminal('SUCCEEDED')), nextPageToken: 'page 2'} :
      {executions: [{name: resource + '/executions/older-active'}]};
  };
  assert.equal(ctx.status('US').running, true);
  assert.equal(paths.length, 2);
  assert.match(paths[1], /pageToken=page%202/);
  scenarios++;
}
console.log('launch guard: ' + scenarios + ' concurrency, ambiguity, retry and pagination scenarios PASS');

const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('bridge/Gateway.gs', 'utf8');

function setup(options = {}) {
  const events = [];
  const ctx = vm.createContext({});
  vm.runInContext(source, ctx);
  ctx.PropertiesService = {getScriptProperties() { return {
    getProperty(key) {
      if (key === 'HUNTER_MONTH_EXPECTED') return '2026-10';
      if (key === 'HUNTER_GLOBAL_FOLDER_ID') return 'root';
      return null;
    }
  }; }};
  ctx.hunterMonthlyRetry_ = month => {
    events.push(['retry', month]);
    return {expectedMonth: month, myt: '2026-10-04 08:00'};
  };
  ctx.DriveApp = {getFolderById() {
    if (options.driveError) throw new Error('DRIVE_UNAVAILABLE');
    return {};
  }};
  const file = value => ({getBlob() { return {
    getDataAsString() { return JSON.stringify(value); }
  }; }});
  ctx.bridgeFolder_ = (_, path) => ({getFilesByName() { return {
    hasNext: () => !options.missingCheckpoint,
    next: () => file({last_completed_date:
      path.startsWith('HK') ? (options.hkDate || '2026-10-02') : '2026-10-01'})
  }; }});
  ctx.bridgeFile_ = () => options.pointer ? file(options.pointer) : null;
  ctx.runMonthly = () => {
    events.push(['launch']);
    if (options.launchError) throw new Error('CLOUD_RUN_UNAVAILABLE');
    return {ok: true, skipped: !!options.running};
  };
  ctx.installMonthlyTriggerAt = (at, month) => {
    events.push(['advance', month, at.toISOString()]);
    return {expectedMonth: month};
  };
  return {ctx, events};
}

for (const options of [{driveError: true}, {missingCheckpoint: true}, {launchError: true}]) {
  const {ctx, events} = setup(options);
  assert.throws(() => ctx.monthlyV2());
  assert.deepEqual(events[0], ['retry', '2026-10']);
  assert.equal(events.some(x => x[0] === 'advance'), false);
}
{
  const {ctx, events} = setup({hkDate: '2026-09-30'});
  assert.equal(ctx.monthlyV2().reason, 'WAIT_FIRST_US_AND_HK_SESSION_DAILY');
  assert.deepEqual(events, [['retry', '2026-10']]);
}
for (const options of [{}, {running: true}, {pointer: {month_file: 'MONTH_2026-10'}}]) {
  const {ctx, events} = setup(options);
  const result = ctx.monthlyV2();
  assert.equal(result.reason, 'WAIT_MONTH_COMMIT');
  assert.equal(result.next.expectedMonth, '2026-10');
  assert.equal(result.skipped, !!options.running);
  assert.deepEqual(events, [['retry', '2026-10'], ['launch']]);
}
{
  const {ctx, events} = setup({pointer: {
    month_file: 'MONTH_2026-10', hk_month_file: 'HK_MONTH_2026-10'
  }});
  assert.equal(ctx.monthlyV2().reason, 'MONTH_ALREADY_COMMITTED');
  assert.deepEqual(events, [
    ['retry', '2026-10'], ['advance', '2026-11', '2026-11-02T00:00:00.000Z']
  ]);
}
console.log('monthly trigger: 8 failure, pending and committed scenarios PASS');

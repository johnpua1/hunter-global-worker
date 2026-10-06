const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const source = fs.readFileSync('bridge/Gateway.gs', 'utf8');
const singletonFunctions = [
  'doGet',
  'doPost',
  'installHunterDailyTriggers',
  'dailyUS',
  'dailyHK',
  'hunterDailyWatchdog',
  'installFirstMonthlyTrigger',
  'monthlyV2',
  'listMonthlyTrigger',
];
for (const name of singletonFunctions) {
  const hits = source.match(new RegExp('function\\s+' + name + '\\b', 'g')) || [];
  assert.equal(hits.length, 1, 'duplicate Apps Script function: ' + name);
}
assert.equal(source.includes('2027Q1'), false);
assert.equal(source.includes('2027, 1, 5'), false);
assert.equal(source.includes("HUNTER_DAILY_STATE_V1_"), true);
assert.equal(source.includes("HUNTER_DAILY_WATCHDOG_MAX_ATTEMPTS = 3"), true);
assert.equal(source.includes("handler === 'hunterDailyWatchdog'"), true);
assert.equal(source.includes("newTrigger('hunterDailyWatchdog').timeBased().everyHours(1)"), true);
assert.equal(source.includes("TODAY_EXECUTION_SUCCEEDED"), true);
assert.equal(source.includes("STATUS_UNREADABLE"), true);
assert.equal(source.includes("op === 'enforce_daily_triggers'"), true);
assert.equal(source.includes("op === 'daily_trigger_status'"), true);
const context = vm.createContext({});
vm.runInContext(source, context);
const scopedProps = {getProperty(name) {
  return ({
    BRIDGE_US_KEY: 'us-key',
    BRIDGE_HK_KEY: 'hk-key',
    BRIDGE_MAINT_KEY: 'maint-key',
    BRIDGE_MONTH_KEY: 'month-key',
    BRIDGE_SHARED_KEY: 'legacy-key',
  })[name] || null;
}};
assert.equal(context.bridgeAuthScope_(scopedProps, 'us-key'), 'US');
assert.equal(context.bridgeAuthScope_(scopedProps, 'hk-key'), 'HK');
assert.equal(context.bridgeAuthScope_(scopedProps, 'maint-key'), 'MAINT');
assert.equal(context.bridgeAuthScope_(scopedProps, 'month-key'), 'MONTH');
assert.equal(context.bridgeAuthScope_(scopedProps, 'legacy-key'), 'LEGACY');
assert.throws(() => context.bridgeAuthScope_(scopedProps, 'wrong-key'), /UNAUTHORIZED/);
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('US', 'read', 'US/CURRENT_UNIVERSE.json'));
assert.throws(() => context.bridgeScopeAuthorize_('US', 'read', 'HK/CURRENT_UNIVERSE.json'), /SCOPE_PATH_DENIED/);
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('HK', 'read', 'HK/CURRENT_UNIVERSE.json'));
assert.throws(() => context.bridgeScopeAuthorize_('HK', 'read', 'US/CURRENT_UNIVERSE.json'), /SCOPE_PATH_DENIED/);
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MAINT', 'read', 'US/CURRENT_UNIVERSE.json'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MAINT', 'read', 'HK/CURRENT_UNIVERSE.json'));
assert.throws(() => context.bridgeScopeAuthorize_('US', 'append', '_BRIDGE_TEST/DAILY/US/x.ndjson.gz'), /SCOPE_PATH_DENIED/);
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read_chunk', 'SNAPSHOT_2026-10-01/US_ACTIVE_OHLC.csv.gz'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read', 'US/CONTROL/DAILY_CHECKPOINT.json'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read', 'HK/CURRENT_UNIVERSE.json'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read', 'HK_SNAPSHOT_2026-10/MANIFEST.json'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read', 'HK_MONTH_2026-10/RESULT.json'));
assert.doesNotThrow(() => context.bridgeScopeAuthorize_('MONTH', 'read', 'HKEX_DESIGNATED_SHORT_SELLING_20260929.csv'));
assert.doesNotThrow(() => context.bridgeWritePolicy_('ACTIVE_POINTER', 'put', 'MONTH'));
assert.doesNotThrow(() => context.bridgeWritePolicy_('HK_SNAPSHOT_2026-10/MANIFEST.json', 'put', 'MONTH'));
assert.doesNotThrow(() => context.bridgeWritePolicy_('HK_MONTH_2026-10/RESULT.json', 'put', 'MONTH'));
assert.doesNotThrow(() => context.bridgeWritePolicy_('HKEX_DESIGNATED_SHORT_SELLING_20260929.csv', 'put', 'MONTH'));
assert.throws(() => context.bridgeWritePolicy_('ACTIVE_POINTER', 'put', 'LEGACY'), /MONTH_WRITER_ONLY/);
assert.throws(() => context.bridgeWritePolicy_('US/BASE/batch-0001.ndjson.gz', 'put'), /BASE_SEALED/);
assert.throws(() => context.bridgeWritePolicy_('HK/BASE/batch-0001.ndjson.gz', 'append'), /BASE_SEALED/);
assert.throws(() => context.bridgeWritePolicy_('HK/DAILY/2026-09-27/part-0001.ndjson.gz', 'put'), /DAILY_APPEND_ONLY/);
assert.doesNotThrow(() => context.bridgeWritePolicy_('HK/DAILY/2026-09-27/part-0001.ndjson.gz', 'append'));
assert.doesNotThrow(() => context.bridgeWritePolicy_('_BRIDGE_TEST/DAILY/US/mini.ndjson.gz', 'append'));

const bytes = b => Array.from(Buffer.from(b));
const blob = (content, mime = 'application/x-gzip', name = '') => ({
  content: Buffer.from(content), mime, name,
  getBytes() { return bytes(this.content); },
  getDataAsString() { return this.content.toString('utf8'); },
});
let counter = 0;
context.Utilities = {
  DigestAlgorithm: {SHA_256: 'SHA_256'},
  computeDigest(_algorithm, data) { return bytes(crypto.createHash('sha256').update(Buffer.from(data)).digest()); },
  newBlob(data, mime, name) { return blob(data, mime, name); },
  ungzip(value) {
    assert.equal(value.mime, 'application/x-gzip');
    assert.equal(value.name, 'daily-payload.ndjson.gz');
    return blob(zlib.gunzipSync(Buffer.from(value.getBytes())));
  },
  base64Decode(data) { return bytes(Buffer.from(data, 'base64')); },
  getUuid() { return String(++counter); },
};
context.ContentService = {MimeType: {JSON: 'JSON'}, createTextOutput(value) {
  return {value, setMimeType() { return this; }};
}};
let held = false;
context.LockService = {getScriptLock() { return {
  waitLock() { assert.equal(held, false); held = true; },
  releaseLock() { assert.equal(held, true); held = false; },
}; }};
function folder(name) {
  const dirs = new Map(), files = [];
  const iterator = items => { let i = 0; return {hasNext: () => i < items.length, next: () => items[i++]}; };
  return {
    getName: () => name,
    getFoldersByName: value => iterator(dirs.has(value) ? [dirs.get(value)] : []),
    createFolder(value) { const next = folder(value); dirs.set(value, next); return next; },
    getFilesByName: value => iterator(files.filter(f => f.getName() === value && !f.trashed)),
    getFiles: () => iterator(files.filter(f => !f.trashed)),
    createFile(value) {
      const file = {name: value.name, trashed: false,
        getName() { return this.name; }, getBlob: () => value,
        getMimeType: () => value.mime, getSize: () => value.content.length,
        setName(next) { this.name = next; }, setTrashed(next) { this.trashed = next; }};
      files.push(file);
      return file;
    },
  };
}
const root = folder('HUNTER_GLOBAL');
const row = {security_id: 'US-000001', date: '2026-09-25', trade_date: '2026-09-25', close: 10};
const payload = zlib.gzipSync(JSON.stringify(row) + '\n');
const fields = {sha256: crypto.createHash('sha256').update(payload).digest('hex'),
  data_base64: payload.toString('base64'), mime: 'application/x-gzip'};
const first = 'US/DAILY/2026-09-25/part-0001.ndjson.gz';
const second = 'US/DAILY/2026-09-25/part-0002.ndjson.gz';
assert.equal(context.bridgePath_(first), first);
context.bridgePut_(root, first, fields, 'append');
assert.throws(() => context.bridgePut_(root, second, fields, 'append'), /DAILY_DUPLICATE_KEY/);
const dateFolder = context.bridgeFolder_(root, 'US/DAILY/2026-09-25', false);
const remaining = dateFolder.getFiles();
assert.equal(remaining.next().getName(), 'part-0001.ndjson.gz');
assert.equal(remaining.hasNext(), false);
assert.throws(() => context.bridgeDailyKeys_(first, bytes(zlib.gzipSync(
  JSON.stringify(row) + '\n' + JSON.stringify(row) + '\n'))), /DAILY_DUPLICATE_KEY/);
const jsonFields = value => {
  const data = Buffer.from(JSON.stringify(value));
  return {
    sha256: crypto.createHash('sha256').update(data).digest('hex'),
    data_base64: data.toString('base64'),
    mime: 'application/json',
  };
};
const queuePath = 'REPAIR_QUEUE.json';
const q0 = {items: [
  {market: 'US', security_id: 'US-000001', category: 'FETCH_FAILED'},
  {market: 'HK', security_id: 'HK-000001', category: 'FETCH_FAILED'},
]};
context.bridgePut_(root, queuePath, jsonFields(q0), 'put', 'LEGACY');
const qUsAllowed = {items: [
  {market: 'US', security_id: 'US-000001', category: 'DATA_SUSPECT'},
  {market: 'HK', security_id: 'HK-000001', category: 'FETCH_FAILED'},
]};
assert.doesNotThrow(() => context.bridgePut_(root, queuePath, jsonFields(qUsAllowed), 'put', 'US'));
const qUsForbidden = {items: [
  {market: 'US', security_id: 'US-000001', category: 'DATA_SUSPECT'},
  {market: 'HK', security_id: 'HK-000001', category: 'IDENTITY_REVIEW'},
]};
assert.throws(
  () => context.bridgePut_(root, queuePath, jsonFields(qUsForbidden), 'put', 'US'),
  /SCOPE_FOREIGN_QUEUE_MUTATION/
);
console.log('bridge sealed path, scoped auth, queue isolation and row dedup PASS');

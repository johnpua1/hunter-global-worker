const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const source = fs.readFileSync('bridge/Gateway.gs', 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
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
  ungzip(value) { return blob(zlib.gunzipSync(Buffer.from(value.getBytes()))); },
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
console.log('bridge sealed path and row dedup PASS');

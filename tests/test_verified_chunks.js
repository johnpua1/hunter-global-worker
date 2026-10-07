const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const crypto=require('node:crypto'),zlib=require('node:zlib');
const ctx=vm.createContext({});vm.runInContext(fs.readFileSync('bridge/Gateway.gs','utf8'),ctx);
const bytes=Buffer.from('{"repeated":"financial history"}\n'.repeat(200000));
let stamp=1;
const file={getId:()=> 'history',getLastUpdated:()=>new Date(stamp),getSize:()=>bytes.length,
            getBlob:()=>({getBytes:()=>Array.from(bytes)})};
ctx.bridgeJson_=x=>x;
ctx.Utilities={DigestAlgorithm:{SHA_256:'sha'},
 computeDigest:(_,raw)=>Array.from(crypto.createHash('sha256').update(Buffer.from(raw)).digest()),
 newBlob:raw=>({getBytes:()=>raw}),gzip:blob=>({getBytes:()=>Array.from(zlib.gzipSync(Buffer.from(blob.getBytes())))}),
 base64Encode:raw=>Buffer.from(raw).toString('base64')};
const first=ctx.bridgeVerifiedChunk_(file,{revision:'history:1',offset:0,length:131072});
assert.equal(first.file_sha256,crypto.createHash('sha256').update(bytes).digest('hex'));
assert.deepEqual(zlib.gunzipSync(Buffer.from(first.data_base64,'base64')),bytes.subarray(0,131072));
assert.ok(first.data_base64.length < 4000);
assert.throws(()=>ctx.bridgeVerifiedChunk_(file,{revision:'history:0',offset:0,length:131072}),/SOURCE_CHANGED/);
assert.throws(()=>ctx.bridgeVerifiedChunk_(file,{revision:'history:1',offset:0,length:262144}),/RANGE_INVALID/);
assert.doesNotThrow(()=>ctx.bridgeScopeAuthorize_('US','read_verified_chunk','US/PHASE2/EARNINGS_HISTORY.json'));
assert.throws(()=>ctx.bridgeScopeAuthorize_('US','read_verified_chunk','HK/PHASE2/EARNINGS_HISTORY.json'),/PATH_DENIED/);
file.getBlob=()=>{stamp++;return {getBytes:()=>Array.from(bytes)}};
assert.throws(()=>ctx.bridgeVerifiedChunk_(file,{revision:'history:1',offset:0,length:131072}),/SOURCE_CHANGED/);
console.log('Compressed verified chunks, revision binding, bounds and market isolation PASS');

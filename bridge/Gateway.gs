/** HUNTER_GLOBAL Drive bridge. All paths resolve below the configured root. */
function setupBridgeKey() {
  var props = PropertiesService.getScriptProperties();
  if (props.getProperty('BRIDGE_SHARED_KEY')) throw new Error('KEY_ALREADY_CONFIGURED');
  props.setProperty('BRIDGE_SHARED_KEY', Utilities.getUuid() + Utilities.getUuid());
  return 'KEY_CREATED';
}
function doGet() {
  return bridgeJson_({ok: true, service: 'HUNTER_GLOBAL_BRIDGE'});
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error('EMPTY_REQUEST');
    var body = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
    var scope = bridgeAuthScope_(props, String(body.key || ''));
    var rootId = props.getProperty('HUNTER_GLOBAL_FOLDER_ID');
    if (!rootId) throw new Error('ROOT_NOT_CONFIGURED');
    var root = DriveApp.getFolderById(rootId);
    if (root.getName() !== 'HUNTER_GLOBAL') throw new Error('ROOT_ID_MISMATCH');
    var op = String(body.op || '');
    if (op === 'bootstrap_month_key') {
      if (scope !== 'LEGACY') throw new Error('MONTH_KEY_BOOTSTRAP_LEGACY_ONLY');
      var monthKey = props.getProperty('BRIDGE_MONTH_KEY');
      var created = false;
      if (!monthKey) {
        monthKey = Utilities.getUuid() + Utilities.getUuid();
        props.setProperty('BRIDGE_MONTH_KEY', monthKey);
        created = true;
      }
      return bridgeJson_({ok: true, key: monthKey, created: created});
    }
    if (op === 'install_month_trigger') {
      if (scope !== 'LEGACY') throw new Error('MONTH_TRIGGER_INSTALL_LEGACY_ONLY');
      return bridgeJson_({ok: true, trigger: installFirstMonthlyTrigger()});
    }
    if (op === 'month_status') {
      if (scope !== 'LEGACY') throw new Error('MONTH_STATUS_LEGACY_ONLY');
      var cfg = hunterCloudConfig_();
      return bridgeJson_({ok: true, trigger: listMonthlyTrigger(),
                          job: status(cfg.monthJob)});
    }
    if (op === 'topology_status') {
      if (scope !== 'LEGACY') throw new Error('TOPOLOGY_STATUS_LEGACY_ONLY');
      return bridgeJson_({ok:true, daily:listHunterDailyTriggers(),
                          monthly:listMonthlyTrigger(), jobs:inspectHunterJobs()});
    }
    if (op === 'daily_trigger_status') {
      if (scope !== 'LEGACY') throw new Error('DAILY_TRIGGER_STATUS_LEGACY_ONLY');
      return bridgeJson_({ok:true, daily:listHunterDailyTriggers()});
    }
    if (op === 'enforce_daily_triggers') {
      if (scope !== 'LEGACY') throw new Error('DAILY_TRIGGER_ENFORCE_LEGACY_ONLY');
      return bridgeJson_({ok:true, daily:installHunterDailyTriggers()});
    }
    if (op === 'daily_watchdog_once') {
      if (scope !== 'LEGACY') throw new Error('DAILY_WATCHDOG_LEGACY_ONLY');
      return bridgeJson_({ok:true, watchdog:hunterDailyWatchdog()});
    }
    if (op === 'enforce_topology_triggers') {
      if (scope !== 'LEGACY') throw new Error('TOPOLOGY_ENFORCE_LEGACY_ONLY');
      var daily = installHunterDailyTriggers();
      var monthly = installFirstMonthlyTrigger();
      return bridgeJson_({ok:true, daily:daily, monthly:monthly});
    }
    var path = bridgePath_(body.path || '');
    bridgeScopeAuthorize_(scope, op, path);
    if (op === 'source_inventory') {
      if (!/^(US|HK)$/.test(path)) throw new Error('INVENTORY_MARKET_REQUIRED');
      var inventory = bridgeSourceInventory_(root, path);
      var packed = Utilities.gzip(Utilities.newBlob(JSON.stringify(inventory))).getBytes();
      return bridgeJson_({ok:true, sha256:bridgeSha_(packed),
                          data_base64:Utilities.base64Encode(packed)});
    }
    if (op === 'folder') {
      if (body.create === true && /^(US|HK)\/BASE(?:\/|$)/.test(path))
        throw new Error('BASE_SEALED');
      if (body.create === true &&
          (/^SNAPSHOT_\d{4}-\d{2}-\d{2}(?:\/|$)/.test(path) ||
           /^MONTH_\d{4}-\d{2}(?:\/|$)/.test(path) ||
           /^HK_SNAPSHOT_\d{4}-\d{2}(?:\/|$)/.test(path) ||
           /^HK_MONTH_\d{4}-\d{2}(?:\/|$)/.test(path)) &&
          scope !== 'MONTH') throw new Error('MONTH_WRITER_ONLY');
      var folder = bridgeFolder_(root, path, body.create === true);
      return bridgeJson_({ok: true, folder: {id: path, name: folder.getName()}});
    }
    if (op === 'list') {
      var parent = bridgeFolder_(root, path, false);
      var entries = [], it = parent.getFiles();
      while (it.hasNext()) {
        var file = it.next();
        if (body.name == null || file.getName() === body.name) {
          entries.push(bridgeInfo_(file, path ? path + '/' + file.getName() : file.getName()));
        }
        if (entries.length > 1000) throw new Error('LIST_LIMIT');
      }
      var dirs = parent.getFolders();
      while (dirs.hasNext()) {
        var dir = dirs.next();
        if (body.name == null || dir.getName() === body.name) {
          entries.push({id: path ? path + '/' + dir.getName() : dir.getName(),
                        name: dir.getName(), mimeType: 'application/vnd.google-apps.folder'});
        }
        if (entries.length > 1000) throw new Error('LIST_LIMIT');
      }
      return bridgeJson_({ok: true, files: entries});
    }
    if (op === 'file' || op === 'read' || op === 'read_chunk' || op === 'read_verified_chunk') {
      var file = bridgeFile_(root, path);
      if (!file) {
        if (op === 'file') return bridgeJson_({ok: true, file: null, read_protocol: 'verified-chunks-v2'});
        throw new Error('FILE_NOT_FOUND');
      }
      if (op === 'file') return bridgeJson_({ok: true, file: bridgeInfo_(file, path), read_protocol: 'verified-chunks-v2'});
      if (op === 'read_chunk') return bridgeReadChunk_(file, body);
      if (op === 'read_verified_chunk') return bridgeVerifiedChunk_(file, body);
      if (file.getSize() > 10000000) throw new Error('READ_SIZE_LIMIT');
      var raw = file.getBlob().getBytes();
      return bridgeJson_({ok: true, sha256: bridgeSha_(raw),
                          data_base64: Utilities.base64Encode(raw)});
    }
    if (op === 'put' || op === 'append') return bridgePut_(root, path, body, op, scope);
    throw new Error('UNKNOWN_OP');
  } catch (err) {
    return bridgeJson_({ok: false, error: String(err.message || err).slice(0, 160)});
  }
}

function bridgeJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
      .setMimeType(ContentService.MimeType.JSON);
}
function bridgeEqual_(a, b) {
  var diff = a.length ^ b.length;
  for (var i = 0; i < Math.max(a.length, b.length); i++)
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}
function bridgeAuthScope_(props, presented) {
  var keys = [
    ['US', props.getProperty('BRIDGE_US_KEY')],
    ['HK', props.getProperty('BRIDGE_HK_KEY')],
    ['MAINT', props.getProperty('BRIDGE_MAINT_KEY')],
    ['MONTH', props.getProperty('BRIDGE_MONTH_KEY')],
    ['LEGACY', props.getProperty('BRIDGE_SHARED_KEY')]
  ];
  for (var i = 0; i < keys.length; i++) {
    if (keys[i][1] && bridgeEqual_(presented, keys[i][1])) return keys[i][0];
  }
  throw new Error('UNAUTHORIZED');
}
function bridgeScopeAuthorize_(scope, op, path) {
  if (scope === 'LEGACY') return;
  if (['folder','list','file','read','read_chunk','read_verified_chunk','source_inventory','put','append'].indexOf(op) < 0)
    throw new Error('SCOPE_OP_DENIED');
  if (!path) throw new Error('SCOPE_ROOT_DENIED');
  if (scope === 'MONTH') {
    if (/^(US|HK)(?:\/|$)/.test(path) ||
        /^SNAPSHOT_\d{4}-\d{2}-\d{2}(?:\/|$)/.test(path) ||
        /^MONTH_\d{4}-\d{2}(?:\/|$)/.test(path) ||
        /^HK_SNAPSHOT_\d{4}-\d{2}(?:\/|$)/.test(path) ||
        /^HK_MONTH_\d{4}-\d{2}(?:\/|$)/.test(path) ||
        /^HKEX_DESIGNATED_SHORT_SELLING_\d{8}\.csv$/.test(path) ||
        path === 'ACTIVE_POINTER') return;
    throw new Error('SCOPE_PATH_DENIED');
  }
  if (scope === 'US') {
    if (/^US(?:\/|$)/.test(path) || path === 'REPAIR_QUEUE.json') return;
    throw new Error('SCOPE_PATH_DENIED');
  }
  if (scope === 'HK') {
    if (/^HK(?:\/|$)/.test(path) || path === 'REPAIR_QUEUE.json') return;
    throw new Error('SCOPE_PATH_DENIED');
  }
  if (scope === 'MAINT') {
    if (/^(US|HK)(?:\/|$)/.test(path) ||
        path === 'REPAIR_QUEUE.json' || path === 'BASE_COMPLETE.json') return;
    throw new Error('SCOPE_PATH_DENIED');
  }
  throw new Error('SCOPE_UNKNOWN');
}
function bridgeQueueForeignView_(doc, ownMarket) {
  if (!doc || !Array.isArray(doc.items)) throw new Error('REPAIR_QUEUE_INVALID');
  var top = {};
  Object.keys(doc).sort().forEach(function (key) {
    if (key !== 'items') top[key] = doc[key];
  });
  var foreign = doc.items.filter(function (item) {
    return !item || item.market !== ownMarket;
  });
  return JSON.stringify({top: top, foreign: foreign});
}
function bridgeRepairQueueScopeGuard_(scope, oldFile, bytes) {
  if (scope !== 'US' && scope !== 'HK') return;
  if (!oldFile) throw new Error('REPAIR_QUEUE_SCOPE_REQUIRES_EXISTING');
  var oldDoc, newDoc;
  try {
    oldDoc = JSON.parse(oldFile.getBlob().getDataAsString('UTF-8'));
    newDoc = JSON.parse(Utilities.newBlob(bytes).getDataAsString('UTF-8'));
  } catch (err) {
    throw new Error('REPAIR_QUEUE_INVALID');
  }
  if (bridgeQueueForeignView_(oldDoc, scope) !== bridgeQueueForeignView_(newDoc, scope))
    throw new Error('SCOPE_FOREIGN_QUEUE_MUTATION');
}
function bridgePath_(path) {
  if (typeof path !== 'string' || path.length > 240) throw new Error('INVALID_PATH');
  if (!path) return '';
  if (path === 'REPAIR_QUEUE.json' || path === 'BASE_COMPLETE.json' ||
      path === 'ACTIVE_POINTER' ||
      /^HKEX_DESIGNATED_SHORT_SELLING_\d{8}\.csv$/.test(path)) return path;
  var parts = path.split('/');
  var root = parts[0];
  var monthlyRoot = /^SNAPSHOT_\d{4}-\d{2}-\d{2}$/.test(root) ||
      /^MONTH_\d{4}-\d{2}$/.test(root) ||
      /^HK_SNAPSHOT_\d{4}-\d{2}$/.test(root) ||
      /^HK_MONTH_\d{4}-\d{2}$/.test(root);
  if ((!/^(US|HK|_BRIDGE_TEST)$/.test(root) && !monthlyRoot) ||
      parts.length > 8 || parts.some(function (p) {
        return !p || p === '.' || p === '..' || !/^[A-Za-z0-9_.-]+$/.test(p);
      })) throw new Error('PATH_OUTSIDE_ROOT');
  return path;
}
function bridgeFolder_(root, path, create) {
  if (!path) return root;
  var folder = root, parts = path.split('/');
  for (var i = 0; i < parts.length; i++) {
    var matches = folder.getFoldersByName(parts[i]);
    if (matches.hasNext()) {
      folder = matches.next();
      if (matches.hasNext()) throw new Error('DUPLICATE_FOLDER');
    } else {
      if (!create) throw new Error('FOLDER_NOT_FOUND');
      folder = folder.createFolder(parts[i]);
    }
  }
  return folder;
}
function bridgeFile_(root, path) {
  if (!path) throw new Error('INVALID_FILE_PATH');
  var split = path.lastIndexOf('/');
  if (split < 0 && path !== 'REPAIR_QUEUE.json' && path !== 'BASE_COMPLETE.json' &&
      path !== 'ACTIVE_POINTER' &&
      !/^HKEX_DESIGNATED_SHORT_SELLING_\d{8}\.csv$/.test(path))
    throw new Error('INVALID_FILE_PATH');
  var folder;
  try { folder = bridgeFolder_(root, split < 0 ? '' : path.slice(0, split), false); }
  catch (err) { if (String(err.message) === 'FOLDER_NOT_FOUND') return null; throw err; }
  var matches = folder.getFilesByName(path.slice(split + 1));
  if (!matches.hasNext()) return null;
  var file = matches.next();
  if (matches.hasNext()) throw new Error('DUPLICATE_FILE');
  return file;
}
function bridgeInfo_(file, path) {
  var info = {id: path, name: file.getName(), mimeType: file.getMimeType(), size: file.getSize()};
  // Drive File methods are required, not optional test-double capabilities.
  // Never silently publish metadata without the revision needed by readers.
  info.revision = file.getId() + ':' + file.getLastUpdated().getTime();
  return info;
}

// Small compressed responses with a stable file revision and full-content hash.
// Existing read/read_chunk clients remain backward compatible during deployment.
function bridgeVerifiedChunk_(file, body) {
  var revision = file.getId() + ':' + file.getLastUpdated().getTime();
  if (typeof body.revision !== 'string' || body.revision !== revision)
    throw new Error('READ_SOURCE_CHANGED');
  var size = Number(file.getSize()), offset = Number(body.offset), length = Number(body.length);
  if (size > 30000000 || !Number.isInteger(offset) || !Number.isInteger(length) ||
      offset < 0 || offset >= size || length < 1 || length > 131072)
    throw new Error('READ_VERIFIED_RANGE_INVALID');
  var bytes = file.getBlob().getBytes();
  if (bytes.length !== size || revision !== file.getId() + ':' + file.getLastUpdated().getTime())
    throw new Error('READ_SOURCE_CHANGED');
  var part = bytes.slice(offset, Math.min(offset + length, size));
  var zipped = Utilities.gzip(Utilities.newBlob(part, 'application/octet-stream')).getBytes();
  return bridgeJson_({ok:true, revision:revision, size:size, offset:offset, length:part.length,
    eof:offset + part.length === size, encoding:'gzip', sha256:bridgeSha_(part),
    file_sha256:bridgeSha_(bytes), compressed_sha256:bridgeSha_(zipped),
    data_base64:Utilities.base64Encode(zipped)});
}

// Batch metadata requests inside the existing Bridge. No source blob reads,
// new permissions, or new service: callers remain restricted to their market.
function bridgeSourceInventory_(root, market) {
  var folderMime = 'application/vnd.google-apps.folder';
  var pending = [{id:bridgeFolder_(root, market, false).getId(), path:market}];
  var entries = [], seen = {}, visited = {}, pages = 0;
  while (pending.length) {
    var batch = pending.splice(0, 10), parents = {};
    batch.forEach(function(p) {
      if (visited[p.id]) throw new Error('INVENTORY_FOLDER_CYCLE');
      visited[p.id] = true;
      parents[p.id] = p.path;
    });
    var q = 'trashed = false and (' + batch.map(function(p) {
      if (!/^[A-Za-z0-9_-]+$/.test(p.id)) throw new Error('INVENTORY_FOLDER_ID_INVALID');
      return "'" + p.id + "' in parents";
    }).join(' or ') + ')';
    var token = '';
    do {
      if (++pages > 500) throw new Error('INVENTORY_PAGE_LIMIT');
      var url = 'https://www.googleapis.com/drive/v3/files?pageSize=1000&q=' + encodeURIComponent(q) +
        '&fields=' + encodeURIComponent('nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,parents)') +
        (token ? '&pageToken=' + encodeURIComponent(token) : '');
      var response = UrlFetchApp.fetch(url, {headers:{Authorization:'Bearer ' + ScriptApp.getOAuthToken()},
                                           muteHttpExceptions:true});
      if (response.getResponseCode() !== 200) throw new Error('INVENTORY_HTTP_' + response.getResponseCode());
      var doc = JSON.parse(response.getContentText());
      if (doc.incompleteSearch || !Array.isArray(doc.files)) throw new Error('INVENTORY_INCOMPLETE');
      doc.files.forEach(function(file) {
        var parent = parents[(file.parents || [])[0]];
        if (!parent) throw new Error('INVENTORY_PARENT_MISMATCH');
        if (file.name.indexOf('._') === 0) return; // Bridge transactional staging.
        if (parent === market && ['BASE','DAILY','REPAIR_PATCH','CORPORATE_ACTIONS'].indexOf(file.name) < 0) return;
        if (parent === market && file.mimeType !== folderMime) throw new Error('INVENTORY_SOURCE_FOLDER_REQUIRED');
        if (!/^[A-Za-z0-9_.-]+$/.test(file.name)) throw new Error('INVENTORY_NAME_INVALID');
        var path = parent + '/' + file.name;
        if (seen[path]) throw new Error('INVENTORY_DUPLICATE_PATH');
        seen[path] = true;
        if (path.split('/').length > 6 || entries.length >= 50000) throw new Error('INVENTORY_SIZE_LIMIT');
        var entry = {id:path, name:file.name, mimeType:file.mimeType};
        if (file.mimeType === folderMime) pending.push({id:file.id,path:path});
        else {
          if (!/^[a-f0-9]{32}$/.test(file.md5Checksum || '')) throw new Error('INVENTORY_FINGERPRINT_MISSING');
          entry.size = Number(file.size); entry.md5 = file.md5Checksum;
        }
        entries.push(entry);
      });
      token = doc.nextPageToken || '';
    } while (token);
  }
  return {schema:1,market:market,entries:entries};
}

function bridgeReadChunk_(file, body) {
  var offset = Number(body.offset || 0);
  var length = Number(body.length || 0);
  var size = Number(file.getSize());
  if (!Number.isInteger(offset) || !Number.isInteger(length) ||
      offset < 0 || length < 1 || length > 6000000 || offset >= size)
    throw new Error('READ_CHUNK_RANGE_INVALID');
  var end = Math.min(size - 1, offset + length - 1);
  var url = 'https://www.googleapis.com/drive/v3/files/' +
      encodeURIComponent(file.getId()) + '?alt=media';
  var response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: {
      Authorization: 'Bearer ' + ScriptApp.getOAuthToken(),
      Range: 'bytes=' + offset + '-' + end
    },
    muteHttpExceptions: true
  });
  var code = response.getResponseCode();
  if (code !== 206 && !(code === 200 && offset === 0 && end === size - 1))
    throw new Error('READ_CHUNK_HTTP_' + code);
  var bytes = response.getBlob().getBytes();
  if (bytes.length !== end - offset + 1) throw new Error('READ_CHUNK_LENGTH_MISMATCH');
  return bridgeJson_({ok: true, offset: offset, length: bytes.length, size: size,
                      eof: end + 1 >= size, sha256: bridgeSha_(bytes),
                      data_base64: Utilities.base64Encode(bytes)});
}
function bridgeSha_(bytes) {
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  return digest.map(function (b) { return ('0' + (b & 255).toString(16)).slice(-2); }).join('');
}
function bridgeWritePolicy_(path, op, scope) {
  // Check the path before creating folders, staging a blob, or accepting an
  // identical-byte no-op. BASE is sealed even against an idempotent write.
  var monthlyControlled = path === 'ACTIVE_POINTER' ||
      /^SNAPSHOT_\d{4}-\d{2}-\d{2}(?:\/|$)/.test(path) ||
      /^MONTH_\d{4}-\d{2}(?:\/|$)/.test(path) ||
      /^HK_SNAPSHOT_\d{4}-\d{2}(?:\/|$)/.test(path) ||
      /^HK_MONTH_\d{4}-\d{2}(?:\/|$)/.test(path) ||
      /^HKEX_DESIGNATED_SHORT_SELLING_\d{8}\.csv$/.test(path) ||
      path === 'US/CONTROL/MONTH_NOTICE.json';
  if (monthlyControlled && scope !== 'MONTH') throw new Error('MONTH_WRITER_ONLY');
  if (scope === 'MONTH' && !monthlyControlled) throw new Error('MONTH_SCOPE_WRITE_DENIED');
  if (/^(US|HK)\/BASE\//.test(path)) throw new Error('BASE_SEALED');
  if (/^(US|HK)\/DAILY\//.test(path) && op !== 'append')
    throw new Error('DAILY_APPEND_ONLY');
  if (op === 'append' && !(/^(US|HK)\/(DAILY|REPAIR_PATCH|CORPORATE_ACTIONS)\//.test(path) ||
                           /^_BRIDGE_TEST\/DAILY\//.test(path)))
    throw new Error('APPEND_PATH_DENIED');
}
function bridgeDailyKeys_(path, bytes) {
  var match = /^(US|HK)\/DAILY\/(\d{4}-\d{2}-\d{2})\/[^/]+\.ndjson\.gz$/.exec(path) ||
      /^_BRIDGE_TEST\/DAILY\/(US|HK)\/(\d{4}-\d{2}-\d{2})\/[^/]+\/[^/]+\.ndjson\.gz$/.exec(path);
  if (!match) return null;
  var market = match[1], date = match[2];
  var rows;
  try {
    rows = Utilities.ungzip(
        Utilities.newBlob(bytes, 'application/x-gzip', 'daily-payload.ndjson.gz'))
        .getDataAsString('UTF-8')
        .split('\n').filter(function (line) { return line.length > 0; })
        .map(function (line) { return JSON.parse(line); });
  } catch (err) { throw new Error('DAILY_PAYLOAD_INVALID'); }
  if (!rows.length) throw new Error('DAILY_PAYLOAD_EMPTY');
  var keys = {};
  rows.forEach(function (row) {
    var sid = row.security_id, tradeDate = row.trade_date || row.date;
    if (typeof sid !== 'string' || sid.indexOf(market + '-') !== 0 ||
        tradeDate !== date || row.date !== date)
      throw new Error('DAILY_ROW_IDENTITY_INVALID');
    var key = sid + '|' + tradeDate;
    if (keys[key]) throw new Error('DAILY_DUPLICATE_KEY');
    keys[key] = true;
  });
  return keys;
}
function bridgePut_(root, path, body, op, scope) {
  scope = scope || 'LEGACY';
  bridgeWritePolicy_(path, op, scope);
  if (!path || (path.indexOf('/') < 0 && path !== 'REPAIR_QUEUE.json' && path !== 'BASE_COMPLETE.json' &&
      path !== 'ACTIVE_POINTER' &&
      !/^HKEX_DESIGNATED_SHORT_SELLING_\d{8}\.csv$/.test(path)))
    throw new Error('INVALID_FILE_PATH');
  if (!/^[a-f0-9]{64}$/.test(body.sha256 || '') ||
      typeof body.data_base64 !== 'string' || body.data_base64.length > 14000000)
    throw new Error('INVALID_PAYLOAD');
  var bytes = Utilities.base64Decode(body.data_base64);
  if (bytes.length > 10000000 || bridgeSha_(bytes) !== body.sha256)
    throw new Error('SHA_MISMATCH');
  var mime = String(body.mime || 'application/json');
  if (['application/json', 'application/x-gzip', 'application/octet-stream', 'text/plain'].indexOf(mime) < 0)
    throw new Error('INVALID_MIME');
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  var staged = null, old = null, oldName = null;
  try {
    var split = path.lastIndexOf('/');
    var parent = bridgeFolder_(root, split < 0 ? '' : path.slice(0, split), true);
    var name = split < 0 ? path : path.slice(split + 1);
    old = bridgeFile_(root, path);
    if (path === 'REPAIR_QUEUE.json') bridgeRepairQueueScopeGuard_(scope, old, bytes);
    if (op === 'append' && old) throw new Error('APPEND_CONFLICT');
    if (op === 'append') {
      var dailyKeys = bridgeDailyKeys_(path, bytes);
      if (dailyKeys) {
        var peers = parent.getFiles();
        while (peers.hasNext()) {
          var peer = peers.next();
          if (!/\.ndjson\.gz$/.test(peer.getName())) continue;
          var storedKeys = bridgeDailyKeys_(path.slice(0, split + 1) + peer.getName(),
                                            peer.getBlob().getBytes());
          Object.keys(dailyKeys).forEach(function (key) {
            if (storedKeys[key]) throw new Error('DAILY_DUPLICATE_KEY');
          });
        }
      }
    }
    if (Object.prototype.hasOwnProperty.call(body, 'expected_sha256')) {
      var actual = old ? bridgeSha_(old.getBlob().getBytes()) : null;
      if (actual !== body.expected_sha256) throw new Error('STALE_WRITE');
    }
    if (old && bridgeSha_(old.getBlob().getBytes()) === body.sha256)
      return bridgeJson_({ok: true, file: bridgeInfo_(old, path), sha256: body.sha256});
    if (old && body.immutable === true) throw new Error('IMMUTABLE_CONFLICT');
    staged = parent.createFile(Utilities.newBlob(bytes, mime,
        '._bridge_' + Utilities.getUuid()));
    if (bridgeSha_(staged.getBlob().getBytes()) !== body.sha256)
      throw new Error('STAGE_READBACK_MISMATCH');
    if (old) {
      oldName = '._backup_' + Utilities.getUuid();
      old.setName(oldName);
    }
    staged.setName(name);
    if (bridgeSha_(staged.getBlob().getBytes()) !== body.sha256)
      throw new Error('WRITE_READBACK_MISMATCH');
    if (old) old.setTrashed(true);
    return bridgeJson_({ok: true, file: bridgeInfo_(staged, path), sha256: body.sha256});
  } catch (err) {
    if (staged) staged.setTrashed(true);
    if (old && oldName) old.setName(path.slice(path.lastIndexOf('/') + 1));
    throw err;
  } finally {
    lock.releaseLock();
  }
}


/** Cloud Run DAILY control plane. Installable triggers run as the script owner. */
function hunterCloudConfig_() {
  var props = PropertiesService.getScriptProperties();
  return {
    project: props.getProperty('GCP_PROJECT_ID') || 'rgs-hunter-global',
    region: props.getProperty('GCP_REGION') || 'us-central1',
    usJob: props.getProperty('HUNTER_US_JOB') || 'hunter-us-daily',
    hkJob: props.getProperty('HUNTER_HK_JOB') || 'hunter-hk-daily',
    maintJob: props.getProperty('HUNTER_MAINT_JOB') || 'hunter-maintenance',
    monthJob: props.getProperty('HUNTER_MONTH_JOB') || 'hunter-monthly-v2'
  };
}

function hunterCloudRequest_(method, resource, payload) {
  var url = 'https://run.googleapis.com/v2/' + resource;
  var options = {
    method: String(method || 'get').toLowerCase(),
    headers: {Authorization: 'Bearer ' + ScriptApp.getOAuthToken()},
    muteHttpExceptions: true
  };
  if (payload != null) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  var last;
  for (var attempt = 0; attempt < 3; attempt++) {
    try {
      var response = UrlFetchApp.fetch(url, options);
      var code = response.getResponseCode();
      var text = response.getContentText();
      var body = text ? JSON.parse(text) : {};
      if (code >= 200 && code < 300) return body;
      last = new Error('CLOUD_RUN_HTTP_' + code + ':' + text.slice(0, 500));
      if ([408, 429, 500, 502, 503, 504].indexOf(code) < 0) break;
    } catch (err) {
      last = err;
    }
    Utilities.sleep(Math.pow(2, attempt) * 1000);
  }
  throw last || new Error('CLOUD_RUN_REQUEST_FAILED');
}

function hunterJobName_(marketOrJob) {
  var cfg = hunterCloudConfig_();
  var value = String(marketOrJob || '').toUpperCase();
  if (value === 'US') return cfg.usJob;
  if (value === 'HK') return cfg.hkJob;
  if (value === 'MAINT') return cfg.maintJob;
  if (value === 'MONTH') return cfg.monthJob;
  if (marketOrJob === cfg.usJob || marketOrJob === cfg.hkJob ||
      marketOrJob === cfg.maintJob || marketOrJob === cfg.monthJob) return String(marketOrJob);
  throw new Error('UNKNOWN_HUNTER_JOB:' + marketOrJob);
}

function hunterJobResource_(job) {
  var cfg = hunterCloudConfig_();
  return 'projects/' + encodeURIComponent(cfg.project) +
      '/locations/' + encodeURIComponent(cfg.region) +
      '/jobs/' + encodeURIComponent(hunterJobName_(job));
}

function hunterExecutionState_(execution) {
  var conditions = execution.conditions || [];
  var completed = null;
  for (var i = 0; i < conditions.length; i++) {
    if (conditions[i].type === 'Completed') { completed = conditions[i]; break; }
  }
  // Prefer explicit terminal signals over completionTime. Cloud Run can report
  // a terminal Completed condition before completionTime is populated.
  if (completed && completed.state === 'CONDITION_SUCCEEDED') return 'SUCCEEDED';
  if (completed && completed.state === 'CONDITION_FAILED') return 'FAILED';
  if (execution.cancelledCount > 0) return 'CANCELLED';
  if (!execution.completionTime) return 'RUNNING';
  return completed ? String(completed.state || 'COMPLETED') : 'COMPLETED';
}

function status(job) {
  var name = hunterJobName_(job);
  var resource = hunterJobResource_(name);
  var response = hunterCloudRequest_('get', resource + '/executions?pageSize=3', null);
  var executions = (response.executions || []).slice(0, 3).map(function (x) {
    return {
      name: x.name || null,
      status: hunterExecutionState_(x),
      createTime: x.createTime || null,
      startTime: x.startTime || null,
      endTime: x.completionTime || null,
      succeededCount: x.succeededCount || 0,
      failedCount: x.failedCount || 0,
      cancelledCount: x.cancelledCount || 0
    };
  });
  return {
    job: name,
    running: executions.some(function (x) { return x.status === 'RUNNING'; }),
    executions: executions
  };
}

function hunterJobConfig_(job) {
  var name = hunterJobName_(job);
  var doc = hunterCloudRequest_('get', hunterJobResource_(name), null);
  var task = (((doc.template || {}).template || {}));
  var containers = task.containers || [];
  var container = containers.length ? containers[0] : {};
  return {
    job: name,
    generation: doc.generation || null,
    latestCreatedExecution: doc.latestCreatedExecution || null,
    image: container.image || null,
    command: container.command || [],
    args: container.args || [],
    envNames: (container.env || []).map(function (x) { return x.name; }),
    updateTime: doc.updateTime || null
  };
}

function inspectHunterJobs() {
  return {US: hunterJobConfig_('US'), HK: hunterJobConfig_('HK'),
          MAINT: hunterJobConfig_('MAINT'), MONTH: hunterJobConfig_('MONTH')};
}

function hunterDurationMs_(value, fallbackMs) {
  var match = /^(\d+(?:\.\d+)?)s$/.exec(String(value || ''));
  if (!match) return fallbackMs;
  return Math.max(1000, Math.round(Number(match[1]) * 1000));
}

function hunterJobStaleAfterMs_(job) {
  var doc = hunterCloudRequest_('get', hunterJobResource_(job), null);
  var task = (((doc.template || {}).template || {}));
  var timeoutMs = hunterDurationMs_(task.timeout, 60 * 60 * 1000);
  // A Cloud Run execution that is still RUNNING after its configured task
  // timeout plus 15 minutes is no longer allowed to block DAILY forever.
  return timeoutMs + 15 * 60 * 1000;
}

function hunterExecutionAgeMs_(execution, now) {
  var stamp = execution && (execution.startTime || execution.createTime);
  if (!stamp) return null;
  var started = new Date(stamp).getTime();
  if (!Number.isFinite(started)) return null;
  return Math.max(0, now.getTime() - started);
}

function hunterRunningSplit_(statusDoc, staleAfterMs, now) {
  var fresh = [], stale = [];
  ((statusDoc || {}).executions || []).forEach(function (execution) {
    if (execution.status !== 'RUNNING') return;
    var age = hunterExecutionAgeMs_(execution, now);
    if (age != null && age > staleAfterMs) stale.push(execution);
    else fresh.push(execution);
  });
  return {fresh: fresh, stale: stale};
}

function hunterCancelExecution_(execution) {
  if (!execution || !execution.name) throw new Error('EXECUTION_NAME_MISSING');
  try {
    return hunterCloudRequest_('post', execution.name + ':cancel', {});
  } catch (err) {
    var message = String(err && err.message ? err.message : err);
    // A stale read can race with Cloud Run completing the execution between
    // status() and :cancel. "not running" is therefore benign; force a fresh
    // status read instead of turning a completed execution into a hard fault.
    if (message.indexOf('cannot be cancelled because it is not running') >= 0) {
      console.log('STALE_CANCEL_ALREADY_TERMINAL execution=' + String(execution.name));
      return {alreadyTerminal: true};
    }
    throw err;
  }
}

function hunterRecoverStaleExecutions_(job, before) {
  var staleAfterMs = hunterJobStaleAfterMs_(job);
  var split = hunterRunningSplit_(before, staleAfterMs, new Date());
  if (split.fresh.length) {
    return {ready: false, reason: 'ALREADY_RUNNING', status: before,
            fresh: split.fresh.length, stale: split.stale.length};
  }
  if (!split.stale.length) {
    return {ready: true, recovered: false, status: before};
  }

  split.stale.forEach(function (execution) {
    hunterCancelExecution_(execution);
    console.log('STALE_EXECUTION_CANCEL_REQUESTED job=' + job +
                ' execution=' + String(execution.name));
  });

  // Cancellation is asynchronous. Poll briefly; if Cloud Run has not yet
  // acknowledged completion, fail closed and let the hourly watchdog retry.
  var last = before;
  for (var attempt = 0; attempt < 10; attempt++) {
    Utilities.sleep(2000);
    last = status(job);
    var again = hunterRunningSplit_(last, staleAfterMs, new Date());
    if (again.fresh.length) {
      return {ready: false, reason: 'ALREADY_RUNNING_AFTER_STALE_CANCEL',
              status: last, fresh: again.fresh.length, stale: again.stale.length};
    }
    if (!again.stale.length) {
      return {ready: true, recovered: true, cancelled: split.stale.length,
              status: last};
    }
  }
  return {ready: false, reason: 'STALE_CANCEL_PENDING', status: last,
          stale: split.stale.length};
}

function runHunterJob_(job) {
  var name = hunterJobName_(job);
  var before = status(name);
  var recovery = hunterRecoverStaleExecutions_(name, before);
  if (!recovery.ready) {
    console.log('SKIP_' + recovery.reason + ' job=' + name);
    return {ok: true, job: name, skipped: true, reason: recovery.reason,
            status: recovery.status, stale: recovery.stale || 0};
  }
  var op = hunterCloudRequest_('post', hunterJobResource_(name) + ':run', {});
  console.log('STARTED job=' + name + ' operation=' + String(op.name || 'UNKNOWN') +
              ' stale_recovered=' + String(Boolean(recovery.recovered)));
  return {ok: true, job: name, skipped: false, operation: op.name || null,
          stale_recovered: Boolean(recovery.recovered),
          stale_cancelled: recovery.cancelled || 0};
}

function runUS() { return runHunterJob_('US'); }
function runHK() { return runHunterJob_('HK'); }
function runMonthly() { return runHunterJob_('MONTH'); }

var HUNTER_DAILY_WATCHDOG_MAX_ATTEMPTS = 3;
var HUNTER_DAILY_WATCHDOG_COOLDOWN_MS = 45 * 60 * 1000;
var HUNTER_DAILY_STATE_PREFIX = 'HUNTER_DAILY_STATE_V1_';

function hunterDailyMytDate_(dateObj) {
  return Utilities.formatDate(dateObj || new Date(), 'Asia/Kuala_Lumpur', 'yyyy-MM-dd');
}

function hunterDailyStateKey_(market, dateText) {
  return HUNTER_DAILY_STATE_PREFIX + String(market).toUpperCase() + '_' +
      String(dateText).replace(/-/g, '');
}

function hunterDailyLoadState_(props, market, dateText) {
  var raw = props.getProperty(hunterDailyStateKey_(market, dateText));
  if (!raw) return {market: market, myt_date: dateText, watchdog_attempts: 0};
  try {
    var state = JSON.parse(raw);
    if (state.market !== market || state.myt_date !== dateText)
      throw new Error('DAILY_STATE_IDENTITY_MISMATCH');
    state.watchdog_attempts = Number(state.watchdog_attempts || 0);
    return state;
  } catch (err) {
    throw new Error('DAILY_STATE_INVALID:' + market + ':' + dateText);
  }
}

function hunterDailySaveState_(props, state) {
  state.updated_at_myt = Utilities.formatDate(
      new Date(), 'Asia/Kuala_Lumpur', "yyyy-MM-dd'T'HH:mm:ssXXX");
  props.setProperty(hunterDailyStateKey_(state.market, state.myt_date),
                    JSON.stringify(state));
}

function hunterDailyExecutionMytDate_(execution) {
  var stamp = execution && (execution.startTime || execution.createTime || execution.endTime);
  if (!stamp) return null;
  try {
    return Utilities.formatDate(new Date(stamp), 'Asia/Kuala_Lumpur', 'yyyy-MM-dd');
  } catch (err) {
    return null;
  }
}

function hunterDailyLatestToday_(statusDoc, dateText) {
  var rows = (statusDoc && statusDoc.executions) || [];
  for (var i = 0; i < rows.length; i++) {
    if (hunterDailyExecutionMytDate_(rows[i]) === dateText) return rows[i];
  }
  return null;
}

function hunterDailyWindowClosed_(market, now) {
  var hh = Number(Utilities.formatDate(now, 'Asia/Kuala_Lumpur', 'HH'));
  var mm = Number(Utilities.formatDate(now, 'Asia/Kuala_Lumpur', 'mm'));
  var minutes = hh * 60 + mm;
  return minutes >= (market === 'US' ? 7 * 60 : 19 * 60);
}

function hunterDailyPrimary_(market) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var now = new Date();
    var dateText = hunterDailyMytDate_(now);
    var props = PropertiesService.getScriptProperties();
    var state = hunterDailyLoadState_(props, market, dateText);
    state.primary_seen_at_myt = Utilities.formatDate(
        now, 'Asia/Kuala_Lumpur', "yyyy-MM-dd'T'HH:mm:ssXXX");
    try {
      var result = runHunterJob_(market);
      state.primary_result = result.skipped ? String(result.reason || 'SKIPPED') : 'DISPATCHED';
      state.primary_operation = result.operation || null;
      hunterDailySaveState_(props, state);
      return result;
    } catch (err) {
      state.primary_result = 'ERROR';
      state.primary_error = String(err.message || err).slice(0, 160);
      hunterDailySaveState_(props, state);
      throw err;
    }
  } finally {
    lock.releaseLock();
  }
}

function dailyUS() { return hunterDailyPrimary_('US'); }
function dailyHK() { return hunterDailyPrimary_('HK'); }

function hunterDailyWatchdogMarket_(market) {
  var now = new Date();
  var dateText = hunterDailyMytDate_(now);
  if (!hunterDailyWindowClosed_(market, now))
    return {market: market, action: 'BEFORE_WINDOW_END'};

  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var props = PropertiesService.getScriptProperties();
    var state = hunterDailyLoadState_(props, market, dateText);
    var cloud;
    try {
      cloud = status(market);
    } catch (err) {
      state.watchdog_last_result = 'STATUS_UNREADABLE';
      state.watchdog_last_error = String(err.message || err).slice(0, 160);
      hunterDailySaveState_(props, state);
      return {market: market, action: 'STATUS_UNREADABLE'};
    }

    // Never treat every RUNNING execution as healthy. An execution older
    // than the Cloud Run task timeout + grace must be recovered here before
    // the watchdog decides to skip. Fresh executions still fail closed.
    var recovery = hunterRecoverStaleExecutions_(market, cloud);
    if (!recovery.ready) {
      state.watchdog_last_result = String(recovery.reason || 'ALREADY_RUNNING');
      state.watchdog_stale_seen = Number(recovery.stale || 0);
      hunterDailySaveState_(props, state);
      return {market: market, action: state.watchdog_last_result,
              stale: state.watchdog_stale_seen};
    }
    if (recovery.recovered) {
      state.watchdog_stale_recovered = true;
      state.watchdog_stale_cancelled = Number(recovery.cancelled || 0);
      cloud = recovery.status || cloud;
    }

    var latestToday = hunterDailyLatestToday_(cloud, dateText);
    if (latestToday && latestToday.status === 'SUCCEEDED') {
      state.watchdog_last_result = 'TODAY_EXECUTION_SUCCEEDED';
      state.last_success_execution = latestToday.name || null;
      hunterDailySaveState_(props, state);
      return {market: market, action: 'TODAY_EXECUTION_SUCCEEDED'};
    }

    if (state.watchdog_attempts >= HUNTER_DAILY_WATCHDOG_MAX_ATTEMPTS) {
      state.watchdog_last_result = 'ATTEMPTS_EXHAUSTED';
      hunterDailySaveState_(props, state);
      return {market: market, action: 'ATTEMPTS_EXHAUSTED',
              attempts: state.watchdog_attempts};
    }

    if (state.watchdog_last_dispatch_epoch_ms &&
        now.getTime() - Number(state.watchdog_last_dispatch_epoch_ms) <
            HUNTER_DAILY_WATCHDOG_COOLDOWN_MS) {
      return {market: market, action: 'COOLDOWN',
              attempts: state.watchdog_attempts};
    }

    state.watchdog_attempts += 1;
    state.watchdog_last_dispatch_epoch_ms = now.getTime();
    state.watchdog_last_dispatch_at_myt = Utilities.formatDate(
        now, 'Asia/Kuala_Lumpur', "yyyy-MM-dd'T'HH:mm:ssXXX");
    hunterDailySaveState_(props, state);

    try {
      var launched = runHunterJob_(market);
      state.watchdog_last_result = launched.skipped ?
          String(launched.reason || 'SKIPPED') : 'DISPATCHED';
      state.watchdog_last_operation = launched.operation || null;
      hunterDailySaveState_(props, state);
      return {market: market, action: state.watchdog_last_result,
              attempts: state.watchdog_attempts,
              operation: state.watchdog_last_operation};
    } catch (err2) {
      state.watchdog_last_result = 'DISPATCH_ERROR';
      state.watchdog_last_error = String(err2.message || err2).slice(0, 160);
      hunterDailySaveState_(props, state);
      return {market: market, action: 'DISPATCH_ERROR',
              attempts: state.watchdog_attempts};
    }
  } finally {
    lock.releaseLock();
  }
}

function hunterDailyWatchdog() {
  return {
    US: hunterDailyWatchdogMarket_('US'),
    HK: hunterDailyWatchdogMarket_('HK')
  };
}

function installHunterDailyTriggers() {
  ScriptApp.requireScopes(ScriptApp.AuthMode.FULL, [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/script.scriptapp'
  ]);
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    var handler = trigger.getHandlerFunction();
    if (handler === 'dailyUS' || handler === 'dailyHK' ||
        handler === 'hunterDailyWatchdog') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('dailyUS').timeBased().atHour(6).everyDays(1)
      .inTimezone('Asia/Kuala_Lumpur').create();
  ScriptApp.newTrigger('dailyHK').timeBased().atHour(18).everyDays(1)
      .inTimezone('Asia/Kuala_Lumpur').create();
  ScriptApp.newTrigger('hunterDailyWatchdog').timeBased().everyHours(1).create();
  return listHunterDailyTriggers();
}

function listHunterDailyTriggers() {
  var rows = ScriptApp.getProjectTriggers().filter(function (trigger) {
    var handler = trigger.getHandlerFunction();
    return handler === 'dailyUS' || handler === 'dailyHK' ||
        handler === 'hunterDailyWatchdog';
  }).map(function (trigger) {
    return {
      handler: trigger.getHandlerFunction(),
      triggerId: trigger.getUniqueId(),
      eventType: String(trigger.getEventType()),
      source: String(trigger.getTriggerSource())
    };
  });
  var counts = {dailyUS: 0, dailyHK: 0, hunterDailyWatchdog: 0};
  rows.forEach(function (x) { counts[x.handler] = (counts[x.handler] || 0) + 1; });
  return {timeZone: Session.getScriptTimeZone(), counts: counts, triggers: rows};
}

function waitHunterJob_(job, timeoutMs) {
  var deadline = Date.now() + Math.min(Number(timeoutMs || 240000), 240000);
  var last = status(job);
  while (last.running && Date.now() < deadline) {
    Utilities.sleep(5000);
    last = status(job);
  }
  return last;
}

function hunterNextWindow_(handler) {
  var now = new Date();
  var tz = 'Asia/Kuala_Lumpur';
  var y = Number(Utilities.formatDate(now, tz, 'yyyy'));
  var m = Number(Utilities.formatDate(now, tz, 'MM')) - 1;
  var d = Number(Utilities.formatDate(now, tz, 'dd'));
  var hour = handler === 'dailyUS' ? 6 : 18;
  var localNowMinutes = Number(Utilities.formatDate(now, tz, 'HH')) * 60 +
      Number(Utilities.formatDate(now, tz, 'mm'));
  var targetMinutes = hour * 60;
  var base = new Date(Date.UTC(y, m, d, hour - 8, 0, 0));
  if (localNowMinutes >= targetMinutes + 60) base = new Date(base.getTime() + 24 * 60 * 60 * 1000);
  var dateText = Utilities.formatDate(base, tz, 'yyyy-MM-dd');
  return dateText + ' ' + ('0' + hour).slice(-2) + ':00–' +
      ('0' + (hour + 1)).slice(-2) + ':00 MYT';
}

function waitHunterJobsTogether_(timeoutMs) {
  var deadline = Date.now() + Math.min(Number(timeoutMs || 300000), 300000);
  var us = status('US');
  var hk = status('HK');
  while ((us.running || hk.running) && Date.now() < deadline) {
    Utilities.sleep(5000);
    us = status('US');
    hk = status('HK');
  }
  return {US: us, HK: hk, timedOut: us.running || hk.running};
}

function hunterLatestOk_(state) {
  if (!state || !state.executions || !state.executions.length) return false;
  return state.executions[0].status === 'SUCCEEDED';
}

function installAndVerify() {
  Logger.log('=== HUNTER DAILY 安装与验证开始 ===');
  var triggers = installHunterDailyTriggers();
  Logger.log('已重建触发器：dailyUS=' + triggers.counts.dailyUS +
             '，dailyHK=' + triggers.counts.dailyHK +
             '，时区=' + triggers.timeZone);

  var usRun = runUS();
  var hkRun = runHK();
  Logger.log('US触发：' + JSON.stringify(usRun));
  Logger.log('HK触发：' + JSON.stringify(hkRun));

  var states = waitHunterJobsTogether_(300000);
  var finalTriggers = listHunterDailyTriggers();

  var okUS = hunterLatestOk_(states.US);
  var okHK = hunterLatestOk_(states.HK);
  var okTriggers = finalTriggers.timeZone === 'Asia/Kuala_Lumpur' &&
      finalTriggers.counts.dailyUS === 1 &&
      finalTriggers.counts.dailyHK === 1 &&
      finalTriggers.counts.hunterDailyWatchdog === 1;

  Logger.log((okUS ? '✓' : '✗') + ' hunter-us-daily：' +
             (states.US.executions.length ? states.US.executions[0].status : 'NO_EXECUTION'));
  Logger.log((okHK ? '✓' : '✗') + ' hunter-hk-daily：' +
             (states.HK.executions.length ? states.HK.executions[0].status : 'NO_EXECUTION'));
  Logger.log((okTriggers ? '✓' : '✗') +
             ' 触发器：dailyUS=' + finalTriggers.counts.dailyUS +
             '，dailyHK=' + finalTriggers.counts.dailyHK +
             '，watchdog=' + finalTriggers.counts.hunterDailyWatchdog);

  // Apps Script does not expose the randomized exact minute chosen by atHour().
  // Therefore report the truthful next execution window, not a fabricated minute.
  Logger.log('dailyUS 下次执行窗口：' + hunterNextWindow_('dailyUS'));
  Logger.log('dailyHK 下次执行窗口：' + hunterNextWindow_('dailyHK'));

  if (states.timedOut) {
    Logger.log('✗ 验证超时：Cloud Run Job 仍在运行；请稍后执行 status(\'US\') / status(\'HK\') 复核。');
    throw new Error('VERIFY_TIMEOUT_JOB_STILL_RUNNING');
  }
  if (!okUS || !okHK || !okTriggers) {
    throw new Error('HUNTER_DAILY_VERIFY_FAILED');
  }

  Logger.log('=== ✓ HUNTER DAILY 上线验证完成 ===');
  return {
    ok: true,
    jobs: states,
    triggers: finalTriggers,
    nextWindows: {
      dailyUS: hunterNextWindow_('dailyUS'),
      dailyHK: hunterNextWindow_('dailyHK')
    }
  };
}

function verifyHunterDaily() {
  return installAndVerify();
}

/**
 * V2 monthly trigger. A one-shot trigger starts at 08:00 MYT on calendar day 2
 * of the target month and retries each morning until US DAILY proves that the
 * first completed US session of that month has been written.
 */
function hunterMonthlyTriggerDate_(year, month1, day) {
  return new Date(Date.UTC(year, month1 - 1, day, 0, 0, 0)); // 08:00 MYT
}

function clearMonthlyTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    var handler = trigger.getHandlerFunction();
    // Retire any pre-deployment monthly trigger as part of the monthly cutover.
    if (handler === 'monthlyV2' || handler === 'quarterlyV2') ScriptApp.deleteTrigger(trigger);
  });
}

function installMonthlyTriggerAt(dateObj, expectedMonth) {
  ScriptApp.requireScopes(ScriptApp.AuthMode.FULL, [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/script.scriptapp'
  ]);
  if (!/^\d{4}-\d{2}$/.test(String(expectedMonth || '')))
    throw new Error('MONTH_EXPECTED_INVALID');
  // Create the replacement before removing the existing retry.
  var previous = ScriptApp.getProjectTriggers().filter(function (trigger) {
    var handler = trigger.getHandlerFunction();
    return handler === 'monthlyV2' || handler === 'quarterlyV2';
  });
  var trigger = ScriptApp.newTrigger('monthlyV2').timeBased().at(dateObj).create();
  previous.forEach(function (old) { ScriptApp.deleteTrigger(old); });
  var props = PropertiesService.getScriptProperties();
  props.setProperty('HUNTER_MONTH_NEXT_TRIGGER', dateObj.toISOString());
  props.setProperty('HUNTER_MONTH_EXPECTED', expectedMonth);
  return {handler:'monthlyV2', triggerId:trigger.getUniqueId(),
          expectedMonth:expectedMonth, at:dateObj.toISOString(),
          myt:Utilities.formatDate(dateObj,'Asia/Kuala_Lumpur','yyyy-MM-dd HH:mm')};
}

function hunterMonthlyCandidate_(year, month1) {
  var y = Number(year), m = Number(month1);
  if (m > 12) { m -= 12; y += 1; }
  if (m < 1) { m += 12; y -= 1; }
  var expected = String(y) + '-' + ('0' + m).slice(-2);
  return {expectedMonth:expected, at:hunterMonthlyTriggerDate_(y, m, 2)};
}

function installFirstMonthlyTrigger() {
  // Initial validation writes the current month. Arm the following month.
  var now = new Date(), tz = 'Asia/Kuala_Lumpur';
  var y = Number(Utilities.formatDate(now, tz, 'yyyy'));
  var m = Number(Utilities.formatDate(now, tz, 'MM')) + 1;
  var next = hunterMonthlyCandidate_(y, m);
  return installMonthlyTriggerAt(next.at, next.expectedMonth);
}

function hunterMonthlyRetry_(expectedMonth) {
  var retry = new Date(Date.now() + 24 * 60 * 60 * 1000);
  retry.setUTCHours(0,0,0,0); // 08:00 MYT
  return installMonthlyTriggerAt(retry, expectedMonth);
}

function hunterNextMonthCandidate_(checkpointDate) {
  var y = Number(checkpointDate.slice(0,4));
  var m = Number(checkpointDate.slice(5,7)) + 1;
  return hunterMonthlyCandidate_(y, m);
}

function monthlyV2() {
  var props = PropertiesService.getScriptProperties();
  var expectedMonth = String(props.getProperty('HUNTER_MONTH_EXPECTED') || '');
  if (!/^\d{4}-\d{2}$/.test(expectedMonth)) throw new Error('MONTH_EXPECTED_NOT_CONFIGURED');

  // Keep this month scheduled even if reads or asynchronous dispatch fail.
  var retry = hunterMonthlyRetry_(expectedMonth);
  var rootId = props.getProperty('HUNTER_GLOBAL_FOLDER_ID');
  if (!rootId) throw new Error('ROOT_NOT_CONFIGURED');
  var root = DriveApp.getFolderById(rootId);

  var active = bridgeFile_(root, 'ACTIVE_POINTER');
  if (active) {
    var pointer = JSON.parse(active.getBlob().getDataAsString('UTF-8'));
    var noticeFile = bridgeFile_(root, 'US/CONTROL/MONTH_NOTICE.json');
    var notice = noticeFile ? JSON.parse(noticeFile.getBlob().getDataAsString('UTF-8')) : null;
    // The worker commits the notice after ACTIVE_POINTER. A pointer alone
    // must not suppress retries when that final write was interrupted.
    if (pointer.month_file === 'MONTH_' + expectedMonth &&
        pointer.hk_month_file === 'HK_MONTH_' + expectedMonth && notice &&
        notice.month_file === pointer.month_file &&
        notice.hk_month_file === pointer.hk_month_file &&
        notice.pointer_version === pointer.version) {
      var alreadyNext = hunterNextMonthCandidate_(expectedMonth + '-01');
      return {ok:true, skipped:true, reason:'MONTH_ALREADY_COMMITTED',
              expectedMonth:expectedMonth,
              next:installMonthlyTriggerAt(alreadyNext.at, alreadyNext.expectedMonth)};
    }
  }

  function checkpointDate_(market) {
    var folder = bridgeFolder_(root, market + '/CONTROL', false);
    var files = folder.getFilesByName('DAILY_CHECKPOINT.json');
    if (!files.hasNext()) throw new Error(market + '_DAILY_CHECKPOINT_MISSING');
    var checkpoint = JSON.parse(files.next().getBlob().getDataAsString('UTF-8'));
    var value = String(checkpoint.last_completed_date || checkpoint.as_of || '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value))
      throw new Error(market + '_DAILY_CHECKPOINT_DATE_INVALID');
    return value;
  }

  var usDate = checkpointDate_('US');
  var hkDate = checkpointDate_('HK');
  if (usDate.slice(0,7) !== expectedMonth || hkDate.slice(0,7) !== expectedMonth) {
    return {ok:true, skipped:true, reason:'WAIT_FIRST_US_AND_HK_SESSION_DAILY',
            expectedMonth:expectedMonth, US:usDate, HK:hkDate,
            next:retry};
  }

  var result = runMonthly();
  return {ok:true, skipped:Boolean(result.skipped), reason:'WAIT_MONTH_COMMIT',
          expectedMonth:expectedMonth, US:usDate, HK:hkDate, run:result, next:retry};
}

function listMonthlyTrigger() {
  var rows = ScriptApp.getProjectTriggers().filter(function (trigger) {
    return trigger.getHandlerFunction() === 'monthlyV2';
  }).map(function (trigger) {
    return {handler:trigger.getHandlerFunction(), triggerId:trigger.getUniqueId(),
            eventType:String(trigger.getEventType()), source:String(trigger.getTriggerSource())};
  });
  var props = PropertiesService.getScriptProperties();
  return {count:rows.length, triggers:rows,
          expectedMonth:props.getProperty('HUNTER_MONTH_EXPECTED') || null,
          configuredAt:props.getProperty('HUNTER_MONTH_NEXT_TRIGGER') || null};
}

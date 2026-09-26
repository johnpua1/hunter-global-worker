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
    var key = props.getProperty('BRIDGE_SHARED_KEY');
    if (!key || !bridgeEqual_(String(body.key || ''), key)) throw new Error('UNAUTHORIZED');
    var rootId = props.getProperty('HUNTER_GLOBAL_FOLDER_ID');
    if (!rootId) throw new Error('ROOT_NOT_CONFIGURED');
    var root = DriveApp.getFolderById(rootId);
    if (root.getName() !== 'HUNTER_GLOBAL') throw new Error('ROOT_ID_MISMATCH');
    var op = String(body.op || '');
    var path = bridgePath_(body.path || '');
    if (op === 'folder') {
      if (body.create === true && /^(US|HK)\/BASE(?:\/|$)/.test(path))
        throw new Error('BASE_SEALED');
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
    if (op === 'file' || op === 'read') {
      var file = bridgeFile_(root, path);
      if (!file) {
        if (op === 'file') return bridgeJson_({ok: true, file: null});
        throw new Error('FILE_NOT_FOUND');
      }
      if (op === 'file') return bridgeJson_({ok: true, file: bridgeInfo_(file, path)});
      if (file.getSize() > 10000000) throw new Error('READ_SIZE_LIMIT');
      var raw = file.getBlob().getBytes();
      return bridgeJson_({ok: true, sha256: bridgeSha_(raw),
                          data_base64: Utilities.base64Encode(raw)});
    }
    if (op === 'put' || op === 'append') return bridgePut_(root, path, body, op);
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
function bridgePath_(path) {
  if (typeof path !== 'string' || path.length > 240) throw new Error('INVALID_PATH');
  if (!path) return '';
  if (path === 'REPAIR_QUEUE.json' || path === 'BASE_COMPLETE.json') return path;
  var parts = path.split('/');
  if (!/^(US|HK|_BRIDGE_TEST)$/.test(parts[0]) ||
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
  if (split < 0 && path !== 'REPAIR_QUEUE.json' && path !== 'BASE_COMPLETE.json')
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
  return {id: path, name: file.getName(), mimeType: file.getMimeType(), size: file.getSize()};
}
function bridgeSha_(bytes) {
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  return digest.map(function (b) { return ('0' + (b & 255).toString(16)).slice(-2); }).join('');
}
function bridgeWritePolicy_(path, op) {
  // Check the path before creating folders, staging a blob, or accepting an
  // identical-byte no-op. BASE is sealed even against an idempotent write.
  if (/^(US|HK)\/BASE\//.test(path)) throw new Error('BASE_SEALED');
  if (/^(US|HK)\/DAILY\//.test(path) && op !== 'append')
    throw new Error('DAILY_APPEND_ONLY');
  if (op === 'append' && !(/^(US|HK)\/(DAILY|REPAIR_PATCH|CORPORATE_ACTIONS)\//.test(path) ||
                           /^_BRIDGE_TEST\/DAILY\//.test(path)))
    throw new Error('APPEND_PATH_DENIED');
}
function bridgePut_(root, path, body, op) {
  bridgeWritePolicy_(path, op);
  if (!path || (path.indexOf('/') < 0 && path !== 'REPAIR_QUEUE.json' && path !== 'BASE_COMPLETE.json'))
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
    if (op === 'append' && old) throw new Error('APPEND_CONFLICT');
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

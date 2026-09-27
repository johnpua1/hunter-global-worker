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
function bridgeDailyKeys_(path, bytes) {
  var match = /^(US|HK)\/DAILY\/(\d{4}-\d{2}-\d{2})\/[^/]+\.ndjson\.gz$/.exec(path) ||
      /^_BRIDGE_TEST\/DAILY\/(US|HK)\/(\d{4}-\d{2}-\d{2})\/[^/]+\/[^/]+\.ndjson\.gz$/.exec(path);
  if (!match) return null;
  var market = match[1], date = match[2];
  var rows;
  try {
    rows = Utilities.ungzip(Utilities.newBlob(bytes)).getDataAsString('UTF-8')
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
    hkJob: props.getProperty('HUNTER_HK_JOB') || 'hunter-hk-daily'
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
  if (marketOrJob === cfg.usJob || marketOrJob === cfg.hkJob) return String(marketOrJob);
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
  if (!execution.completionTime) return 'RUNNING';
  if (completed && completed.state === 'CONDITION_SUCCEEDED') return 'SUCCEEDED';
  if (completed && completed.state === 'CONDITION_FAILED') return 'FAILED';
  if (execution.cancelledCount > 0) return 'CANCELLED';
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
  return {US: hunterJobConfig_('US'), HK: hunterJobConfig_('HK')};
}

function runHunterJob_(job) {
  var name = hunterJobName_(job);
  var before = status(name);
  if (before.running) {
    console.log('SKIP_ALREADY_RUNNING job=' + name);
    return {ok: true, job: name, skipped: true, reason: 'ALREADY_RUNNING', status: before};
  }
  var op = hunterCloudRequest_('post', hunterJobResource_(name) + ':run', {});
  console.log('STARTED job=' + name + ' operation=' + String(op.name || 'UNKNOWN'));
  return {ok: true, job: name, skipped: false, operation: op.name || null};
}

function runUS() { return runHunterJob_('US'); }
function runHK() { return runHunterJob_('HK'); }

function dailyUS() { return runUS(); }
function dailyHK() { return runHK(); }

function installHunterDailyTriggers() {
  ScriptApp.requireScopes(ScriptApp.AuthMode.FULL, [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/script.scriptapp'
  ]);
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    var handler = trigger.getHandlerFunction();
    if (handler === 'dailyUS' || handler === 'dailyHK') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('dailyUS').timeBased().atHour(6).everyDays(1)
      .inTimezone('Asia/Kuala_Lumpur').create();
  ScriptApp.newTrigger('dailyHK').timeBased().atHour(18).everyDays(1)
      .inTimezone('Asia/Kuala_Lumpur').create();
  return listHunterDailyTriggers();
}

function listHunterDailyTriggers() {
  var rows = ScriptApp.getProjectTriggers().filter(function (trigger) {
    var handler = trigger.getHandlerFunction();
    return handler === 'dailyUS' || handler === 'dailyHK';
  }).map(function (trigger) {
    return {
      handler: trigger.getHandlerFunction(),
      triggerId: trigger.getUniqueId(),
      eventType: String(trigger.getEventType()),
      source: String(trigger.getTriggerSource())
    };
  });
  var counts = {dailyUS: 0, dailyHK: 0};
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

function verifyHunterDaily() {
  var triggers = installHunterDailyTriggers();
  var usRun = runUS();
  var hkRun = runHK();
  var us = waitHunterJob_('US', 240000);
  var hk = waitHunterJob_('HK', 240000);
  var finalTriggers = listHunterDailyTriggers();
  var okTriggers = finalTriggers.timeZone === 'Asia/Kuala_Lumpur' &&
      finalTriggers.counts.dailyUS === 1 && finalTriggers.counts.dailyHK === 1;
  var okUS = us.executions.length > 0 && us.executions[0].status === 'SUCCEEDED';
  var okHK = hk.executions.length > 0 && hk.executions[0].status === 'SUCCEEDED';
  return {
    ok: okTriggers && okUS && okHK,
    initialTriggers: triggers,
    runUS: usRun,
    runHK: hkRun,
    statusUS: us,
    statusHK: hk,
    triggers: finalTriggers
  };
}

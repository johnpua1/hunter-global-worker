/** Bounded daily catch-up; launches Cloud Run only through the guarded entry. */
function listHunterDailyWatchdog() {
  var rows = ScriptApp.getProjectTriggers().filter(function(t) {
    return t.getHandlerFunction() === 'hunterDailyWatchdog';
  }).map(function(t) { return {triggerId:t.getUniqueId(), handler:t.getHandlerFunction()}; });
  return {count:rows.length, triggers:rows, timeZone:Session.getScriptTimeZone(),
    timezoneBasis:'Asia/Kuala_Lumpur', USAfterHour:7, HKAfterHour:19, maxExtraLaunchesPerDay:2,
    lastCheck:PropertiesService.getScriptProperties().getProperty('HUNTER_WATCHDOG_LAST_CHECK')};
}

function installHunterDailyWatchdog() {
  var state = listHunterDailyWatchdog();
  if (state.count > 1) throw new Error('WATCHDOG_TRIGGER_DRIFT');
  if (!state.count) ScriptApp.newTrigger('hunterDailyWatchdog').timeBased().everyHours(1)
      .inTimezone('Asia/Kuala_Lumpur').create();
  return listHunterDailyWatchdog();
}

function removeHunterDailyWatchdog() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'hunterDailyWatchdog') ScriptApp.deleteTrigger(t);
  });
  return listHunterDailyWatchdog();
}

function hunterWatchdogAlert_(reason) {
  var project = hunterCloudConfig_().project;
  var event = {event:'HUNTER_DAILY_WATCHDOG_FAILED', reason:reason,
    at_myt:Utilities.formatDate(new Date(), 'Asia/Kuala_Lumpur', 'yyyy-MM-dd HH:mm:ss')};
  console.error(JSON.stringify(event));
  var response = UrlFetchApp.fetch('https://logging.googleapis.com/v2/entries:write', {
    method:'post', contentType:'application/json', muteHttpExceptions:true,
    headers:{Authorization:'Bearer ' + ScriptApp.getOAuthToken()},
    payload:JSON.stringify({entries:[{logName:'projects/' + project + '/logs/hunter-control',
      severity:'ERROR', resource:{type:'global',labels:{project_id:project}}, jsonPayload:event}]})
  });
  if (response.getResponseCode() >= 300) throw new Error('HUNTER_ALERT_LOG_WRITE_FAILED');
}

function hunterDailyWatchdog() {
  var hour = Number(Utilities.formatDate(new Date(), 'Asia/Kuala_Lumpur', 'HH'));
  var failures = [], results = {};
  var daily = listHunterDailyTriggers();
  if (daily.counts.dailyUS !== 1 || daily.counts.dailyHK !== 1 ||
      ['Asia/Kuala_Lumpur','Asia/Singapore'].indexOf(daily.timeZone) < 0) {
    failures.push('DAILY_TRIGGER_DRIFT');
  }
  if (listHunterDailyWatchdog().count !== 1) failures.push('WATCHDOG_TRIGGER_DRIFT');
  [['US',7],['HK',19]].forEach(function(entry) {
    if (hour < entry[1]) return;
    try {
      results[entry[0]] = runHunterJob_(entry[0], true);
    } catch (err) {
      // runHunterJob_ emits a sanitized control alert. Always check the other market.
      failures.push(entry[0] + '_CHECK_FAILED');
    }
  });
  PropertiesService.getScriptProperties().setProperty('HUNTER_WATCHDOG_LAST_CHECK',
    JSON.stringify({at_myt:Utilities.formatDate(new Date(), 'Asia/Kuala_Lumpur', 'yyyy-MM-dd HH:mm:ss'),
      ok:failures.length === 0, markets:Object.keys(results), failures:failures}));
  if (failures.length) {
    hunterWatchdogAlert_(failures.join(','));
    throw new Error('HUNTER_DAILY_WATCHDOG_FAILED');
  }
  return {ok:true, results:results};
}

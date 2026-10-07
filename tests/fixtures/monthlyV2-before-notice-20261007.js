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
    if (pointer.month_file === 'MONTH_' + expectedMonth &&
        pointer.hk_month_file === 'HK_MONTH_' + expectedMonth) {
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

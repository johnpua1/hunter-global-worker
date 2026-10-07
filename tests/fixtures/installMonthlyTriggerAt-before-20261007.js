function installMonthlyTriggerAt(dateObj, expectedMonth) {
  ScriptApp.requireScopes(ScriptApp.AuthMode.FULL, [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/script.scriptapp'
  ]);
  if (!/^\d{4}-\d{2}$/.test(String(expectedMonth || '')))
    throw new Error('MONTH_EXPECTED_INVALID');
  clearMonthlyTriggers_();
  var trigger = ScriptApp.newTrigger('monthlyV2').timeBased().at(dateObj).create();
  var props = PropertiesService.getScriptProperties();
  props.setProperty('HUNTER_MONTH_NEXT_TRIGGER', dateObj.toISOString());
  props.setProperty('HUNTER_MONTH_EXPECTED', expectedMonth);
  return {handler:'monthlyV2', triggerId:trigger.getUniqueId(),
          expectedMonth:expectedMonth, at:dateObj.toISOString(),
          myt:Utilities.formatDate(dateObj,'Asia/Kuala_Lumpur','yyyy-MM-dd HH:mm')};
}

"""Inspect the known failed US execution, then resume once using existing settings.

No deployment, configuration change, HK launch, or cancellation of another
execution. A changed latest execution makes this script a no-op on repeat.
"""
import datetime as dt
import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


recovery = module('us_recovery', 'recover-hk-timeout-20261006.py')
deploy = module('us_deploy', 'deploy-sync-fix-20261007.py')
diagnose = module('us_diagnose', 'diagnose-sync-failure-20261007.py')
JOB = 'hunter-us-daily'
OLD = 'hunter-us-daily-nqxwp'
IMAGE = ('us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
         '1d59d88cf7d6d2a3982d739f787b96830a69653f')
recovery.JOB = JOB


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def validate_job(doc):
    rows = list(recovery.containers(doc))
    if (deploy.daily_runtime(doc) != (7200, 0) or len(rows) != 1
            or rows[0].get('image') != IMAGE
            or rows[0].get('args') != ['--mode', 'auto', '--market', 'US']):
        raise RuntimeError('EXPECTED_US_DEPLOYMENT_REQUIRED_NO_START')
    env = {x['name']: x.get('value') for x in rows[0].get('env', [])}
    if any(env.get(k) != v for k, v in recovery.SETTINGS.items()):
        raise RuntimeError('EXPECTED_BRIDGE_READ_SETTINGS_REQUIRED_NO_START')


def eligible(rows):
    # ACTIVE includes queued/retrying executions. Unknown states fail closed.
    if any(recovery.state(row) == 'ACTIVE' for row in rows):
        print('EXISTING_US_EXECUTION_NO_START', flush=True)
        return False
    if not rows or recovery.name(rows[0]) != OLD or recovery.state(rows[0]) != 'FAILED':
        print('LATEST_EXECUTION_CHANGED_OR_NOT_FAILED_NO_START', flush=True)
        return False
    return True


def main():
    print('OBSERVED_AT_MYT=' + dt.datetime.now(diagnose.MYT).isoformat(), flush=True)
    if not eligible(recovery.executions()):
        return
    old = recovery.gc('run', 'jobs', 'executions', 'describe', OLD)
    finished = old.get('status', {}).get('completionTime')
    if recovery.state(old) != 'FAILED' or not finished:
        raise RuntimeError('OLD_FAILURE_NOT_CONFIRMED_NO_START')
    query = ('resource.type="cloud_run_job" AND resource.labels.job_name="' + JOB +
             '" AND labels."run.googleapis.com/execution_name"="' + OLD + '"')
    logs = recovery.gc('logging', 'read', query, '--limit=60', '--order=desc')
    timeout_confirmed = False
    for entry in reversed(logs):
        message = str(entry.get('textPayload') or entry.get('jsonPayload', {}).get('message', ''))
        when = entry.get('timestamp')
        if when and 'Terminating task because it has reached the maximum timeout of 3600 seconds' in message:
            # The first attempt also timed out. Require evidence immediately
            # preceding final termination, not merely that earlier timeout.
            delta = (timestamp(finished) - timestamp(when)).total_seconds()
            timeout_confirmed |= 0 <= delta <= 120
        message = re.sub(r'https?://\S+', '[URL]', message)
        message = re.sub(r'(?i)(bearer\s+|(?:key|token|password)[\"\x27]?\s*[:=]\s*)[^\s,}]+',
                         r'\1[REDACTED]', message)
        message = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ ', '', message)
        if message.strip():
            print(str(diagnose.myt(when)) + ' ' + message[:4000], flush=True)
    if not timeout_confirmed:
        raise RuntimeError('FINAL_TIMEOUT_NOT_CONFIRMED_NO_START_REVIEW_LOGS')
    validate_job(recovery.gc('run', 'jobs', 'describe', JOB))
    print('FINAL_TIMEOUT_CONFIRMED;NEXT_RUNTIME=7200s;RETRIES=0;CONFIG_UNCHANGED', flush=True)
    if not eligible(recovery.executions()):
        return
    own = recovery.name(recovery.gc('run', 'jobs', 'execute', JOB, '--async'))
    if not re.fullmatch(re.escape(JOB) + r'-[a-z0-9]+', own):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_US=' + own, flush=True)
    # Cloud Shell cannot acquire Apps Script's launcher lock. If a scheduler
    # wins the intervening race, cancel only our own new execution.
    rows = recovery.executions()
    if any(recovery.name(row) != own and recovery.state(row) == 'ACTIVE' for row in rows):
        recovery.gc('run', 'jobs', 'executions', 'cancel', own, '--async')
        print('CONCURRENT_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED', flush=True)
    print('DATA_RECOVERY=NOT_YET_VERIFIED;HK_AND_SUPPORT_JOBS=UNCHANGED', flush=True)


if __name__ == '__main__':
    main()

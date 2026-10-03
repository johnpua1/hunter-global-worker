#!/usr/bin/env python3
"""Deploy reviewed budget fix and daily watchdog; no historical repair rerun."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]
BASE = '617c3602340adddc7e94e709ccc2cec422770f81'
JOB = 'hunter-maintenance'
RUNTIME_FILES = {'hunter-global/maintenance.py', 'hunter-global/repair.py',
                 'hunter-global/runner.py', 'hunter-global/time_budget.py', 'hunter-global/options.py'}


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'cloudrun' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def reviewed_release(deploy):
    release = deploy.command(['git','rev-parse','HEAD'], ROOT).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise RuntimeError('RELEASE_SHA_INVALID')
    changed = deploy.command(['git','diff','--name-only',BASE,release,'--','hunter-global','Dockerfile'], ROOT)
    if set(changed.splitlines()) != RUNTIME_FILES:
        raise RuntimeError('UNREVIEWED_HANDS_OFF_RUNTIME_CHANGE')
    if deploy.command(['git','status','--porcelain','--untracked-files=no'], ROOT).strip():
        raise RuntimeError('DIRTY_RELEASE_CHECKOUT')
    return release


def verify_scheduler(deploy):
    rows = json.loads(deploy.command(['gcloud','scheduler','jobs','list',
        '--project=rgs-hunter-global','--location=us-central1','--format=json']))
    maintenance = []
    for row in rows:
        target = row.get('httpTarget', {})
        uri = target.get('uri', '')
        if any(uri.endswith('/' + job + ':run') for job in
               ('hunter-us-daily', 'hunter-hk-daily', 'hunter-monthly-v2')):
            if row.get('state') != 'PAUSED':
                raise RuntimeError('FORBIDDEN_DAILY_OR_MONTHLY_SCHEDULER_ACTIVE')
        if uri.endswith('/hunter-maintenance:run'):
            maintenance.append(row)
    if len(maintenance) != 1:
        raise RuntimeError('MAINTENANCE_SCHEDULER_NOT_UNIQUE')
    row = maintenance[0]
    if (row.get('state') != 'ENABLED' or row.get('schedule') != '0 20 * * *'
            or row.get('timeZone') != 'Asia/Kuala_Lumpur'
            or row.get('httpTarget', {}).get('oauthToken', {}).get('serviceAccountEmail') !=
            'hunter-scheduler@rgs-hunter-global.iam.gserviceaccount.com'):
        raise RuntimeError('MAINTENANCE_SCHEDULER_DRIFT')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-idle', action='store_true')
    args = parser.parse_args()
    deploy = module('hands_off_script', 'deploy-monthly-retry.py')
    repair = module('hands_off_repair', 'deploy-maintenance-repair.py')
    guard = module('hands_off_guard', 'enroll-production-guard.py')
    mutex = module('hands_off_mutex', 'deploy-maintenance-lock.py')
    watchdog = module('hands_off_watchdog', 'deploy-daily-watchdog.py')
    release = reviewed_release(deploy)
    verify_scheduler(deploy)
    guard.PREVIOUS[JOB] = (BASE, 'MAINT', '2')
    before = guard.gc('run','jobs','describe',JOB)
    guard.validate_previous(JOB, before, release)
    if 'HUNTER_MAINTENANCE_LOCK_PROBE' in guard.parts(before)[3]:
        raise RuntimeError('PERSISTENT_LOCK_PROBE_FORBIDDEN')
    image = guard.REGION + '-docker.pkg.dev/' + guard.PROJECT + '/hunter-worker/runner:' + release
    already = guard.parts(before)[2]['image'] == image
    if not already:
        repair.ensure_image(image)
    repair.wait_idle(deploy, args.wait_idle)
    if already:
        guard.probe(JOB, 'VERIFY')
    else:
        guard.main(release=release, jobs=[JOB])
    repair.wait_idle(deploy, args.wait_idle)
    try:
        mutex.verify_lock_probe(guard)
    except Exception:
        if not already:
            mutex.rollback(guard, before)
        raise
    repair.stamp('MAINTENANCE_BUDGET_DEPLOYED_GUARD_AND_LOCK_VERIFIED')
    repair.wait_idle(deploy, args.wait_idle)
    watchdog.main()
    verify_scheduler(deploy)
    repair.stamp('HARDENING_DEPLOYED_AND_READ_BACK')
    repair.stamp('NO_EXTRA_MAINTENANCE_EXECUTION_STARTED')
    repair.stamp('NEXT_AUTOMATIC_BUSINESS_CYCLE_STILL_REQUIRES_VERIFICATION')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))

"""Recover the two observed timeout executions using the deployed image only.

No build, deployment, persistent argument changes, or daily/ranking replay.
Repeated calls preserve newer executions, including queued executions.
"""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'incremental_recovery_base', ROOT / 'cloudrun/deploy-incremental-inputs-20261007.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)
base = deployment.base
gc = base.recovery.gc
IMAGE_SHA = 'f9347e5727434af83634161d5cbe37481a80ef70'
TARGETS = {'US': ('hunter-us-daily-pcdsr', '2026-10-06'),
           'HK': ('hunter-hk-daily-frr69', '2026-10-07')}


def executions(job):
    rows = gc('run', 'jobs', 'executions', 'list', '--job=' + job,
              '--sort-by=~metadata.creationTimestamp')
    if not isinstance(rows, list) or any(not base.recovery.name(r) for r in rows):
        raise RuntimeError('EXECUTION_LIST_INVALID')
    return rows


def eligible(market):
    old, _ = TARGETS[market]
    rows = executions('hunter-' + market.lower() + '-daily')
    active = [base.recovery.name(r) for r in rows if base.recovery.state(r) == 'ACTIVE']
    if active:
        print(market + '_EXISTING_EXECUTION_PRESERVED=' + ','.join(active), flush=True)
        return False
    if not rows or base.recovery.name(rows[0]) != old or base.recovery.state(rows[0]) != 'FAILED':
        print(market + '_LATEST_CHANGED_OR_NOT_FAILED_NO_START', flush=True)
        return False
    return True


def verify_job(doc, market):
    c = deployment.container(doc, market)
    env = {e['name']: e.get('value') for e in c.get('env', [])}
    if (c.get('image') != base.BASE_IMAGE + IMAGE_SHA
            or env.get('HUNTER_SOURCE_SHA') != IMAGE_SHA
            or env.get('HUNTER_INCREMENTAL_INPUTS') != '1'
            or base.deploy.daily_runtime(doc) != (7200, 0)):
        raise RuntimeError('EXPECTED_INCREMENTAL_DEPLOYMENT_REQUIRED')


def preflight(doc, market):
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive, validate_phase2_resume

    class Reader(Drive):
        def _call(self, op, **kwargs):
            if op not in {'file', 'list', 'read', 'read_chunk'}:
                raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op, **kwargs)

    saved = dict(os.environ)
    try:
        env = {v['name']: v for v in deployment.container(doc, market).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = base.deploy.preflight_env_value(doc, env.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
        drive = Reader()
        try:
            cp = validate_phase2_resume(drive, market, TARGETS[market][1])
        finally:
            drive.http.close()
        if cp.get('phase2_completed_date') == TARGETS[market][1]:
            print(market + '_PHASE2_ALREADY_COMMITTED_NO_START', flush=True)
            return False
        print(market + '_DAILY_AND_RANK_VERIFIED=' + TARGETS[market][1], flush=True)
        return True
    finally:
        os.environ.clear()
        os.environ.update(saved)


def recover(market):
    job = 'hunter-' + market.lower() + '-daily'
    if not eligible(market):
        return
    before = gc('run', 'jobs', 'describe', job)
    verify_job(before, market)
    print(market + '_CHECKING_COMMITTED_DAILY_AND_RANK', flush=True)
    if not preflight(before, market):
        return
    current = gc('run', 'jobs', 'describe', job)
    verify_job(current, market)
    if base.configuration(current) != base.configuration(before):
        raise RuntimeError('CONFIGURATION_CHANGED_NO_START')
    if not eligible(market):
        return
    args = ['--mode', 'auto', '--market', market, '--phase2-only', '--as-of', TARGETS[market][1]]
    # If the request response is lost, stop: do not retry an uncertain launch.
    own = base.recovery.name(gc('run', 'jobs', 'execute', job,
                               '--args=' + ','.join(args), '--async'))
    if not re.fullmatch(re.escape(job) + r'-[a-z0-9]+', own):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_' + market + '_PHASE2_ONLY=' + own, flush=True)
    if any(base.recovery.name(r) != own and base.recovery.state(r) == 'ACTIVE'
           for r in executions(job)):
        gc('run', 'jobs', 'executions', 'cancel', own, '--async')
        raise RuntimeError('CONCURRENT_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED')
    print(market + '_DAILY_AND_RANK_NOT_REPLAYED;PHASE2_COMPLETION_NOT_YET_VERIFIED', flush=True)


def main():
    os.umask(0o077)
    sha = sys.argv[1] if len(sys.argv) == 2 else ''
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() != sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    failed = False
    for market in TARGETS:
        try:
            recover(market)
        except Exception as exc:
            # Credentials or Bridge URLs can occur in upstream exceptions.
            reason = str(exc)
            safe = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', reason) else type(exc).__name__
            print(market + '_RECOVERY_STOPPED=' + safe, flush=True)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

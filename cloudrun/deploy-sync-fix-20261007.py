"""Deploy the synchronization repair from a pinned checkout in Cloud Shell."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('recovery', ROOT / 'cloudrun/recover-hk-timeout-20261006.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def main():
    os.umask(0o077)
    sha = sys.argv[1]
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise RuntimeError('PINNED_COMMIT_REQUIRED')
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if head != sha:
        raise RuntimeError('CHECKOUT_SHA_MISMATCH')
    base = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner'
    image = base + ':' + sha
    approved = {'760c2808f786df376575839ac0bb4334989955fe',
                '859323f28a76057b51c5e807feec2db6a9aa3b5f',
                '64dbb5ede192a66f4fd16eebef1f33521f07fb7b', sha}
    jobs = {'hunter-us-daily': ['--mode', 'auto', '--market', 'US'],
            'hunter-hk-daily': ['--mode', 'auto', '--market', 'HK'],
            'hunter-maintenance': ['/app/maintenance.py']}
    work = pathlib.Path(tempfile.mkdtemp(prefix='hunter-sync-repair-'))
    for job, args in jobs.items():
        before = recovery.gc('run', 'jobs', 'describe', job)
        rows = list(recovery.containers(before))
        if (len(rows) != 1 or rows[0].get('args') != args or
                rows[0].get('image') not in {base + ':' + s for s in approved}):
            raise RuntimeError('UNEXPECTED_JOB_CONFIGURATION:' + job)
        (work / (job + '.before.json')).write_text(json.dumps(before))
    config = work / 'build.json'
    config.write_text(json.dumps({'steps': [{'name': 'gcr.io/cloud-builders/docker',
        'args': ['build', '--build-arg', 'HUNTER_SOURCE_SHA=' + sha, '-t', image, '.']}],
        'images': [image], 'timeout': '1800s'}))
    subprocess.run(['gcloud', 'builds', 'submit', str(ROOT), '--config=' + str(config),
                    '--project=' + recovery.PROJECT, '--region=' + recovery.REGION, '--quiet'], check=True)
    for job in jobs:
        settings = {'HUNTER_SOURCE_SHA': sha}
        if job.endswith('-daily'):
            settings.update(DAILY_READ_WORKERS='2', DERIVED_READ_WORKERS='2',
                            HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90')
        recovery.gc('run', 'jobs', 'update', job, '--image=' + image,
                    '--update-env-vars=' + ','.join(k + '=' + v for k, v in settings.items()))
        after = recovery.gc('run', 'jobs', 'describe', job)
        rows = list(recovery.containers(after))
        env = {v['name']: v.get('value') for v in rows[0].get('env', [])} if len(rows) == 1 else {}
        if (len(rows) != 1 or rows[0].get('image') != image or rows[0].get('args') != jobs[job]
                or any(env.get(k) != v for k, v in settings.items())):
            raise RuntimeError('DEPLOY_READBACK_FAILED:' + job)
        print('DEPLOY_VERIFIED=' + job, flush=True)
    for market in ('us', 'hk'):
        recovery.JOB = 'hunter-' + market + '-daily'
        active = [r for r in recovery.executions() if recovery.state(r) == 'ACTIVE']
        if active:
            print('EXISTING_EXECUTION=' + ','.join(recovery.name(r) for r in active), flush=True)
            continue
        # Fresh check immediately before dispatch; cancel our own execution if
        # the scheduled launcher wins the remaining race.
        if any(recovery.state(r) == 'ACTIVE' for r in recovery.executions()):
            continue
        own = recovery.name(recovery.gc('run', 'jobs', 'execute', recovery.JOB, '--async'))
        if not own.startswith(recovery.JOB + '-'):
            raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT')
        active = [r for r in recovery.executions() if recovery.state(r) == 'ACTIVE']
        if any(recovery.name(r) != own for r in active):
            recovery.gc('run', 'jobs', 'executions', 'cancel', own, '--async')
            print('CONCURRENT_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED=' + own, flush=True)
        else:
            print('SYNC_EXECUTION=' + own, flush=True)
    print('SYNC_COMPLETION=REQUIRES_CHECKPOINT_AND_RANK_VERIFICATION', flush=True)


if __name__ == '__main__':
    main()

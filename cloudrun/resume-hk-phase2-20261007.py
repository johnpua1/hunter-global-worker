"""Deploy only HK, then resume only Phase 2 of its committed Oct 6 close.

Uses the existing HK job and unchanged runtime/capacity. Its persistent entry
remains auto/HK; --phase2-only is a one-execution argument override. Never
cancels or launches US, maintenance, monthly, or an existing HK execution.
"""
import copy
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hk_phase2_deploy', ROOT / 'cloudrun/deploy-sync-fix-20261007.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
recovery = deploy.recovery
JOB = 'hunter-hk-daily'
OLD = 'hunter-hk-daily-4vc7t'
DATE = '2026-10-06'
BASE_IMAGE = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
PREVIOUS = 'b630032467f3633d87a781f286afa43cb9b72ead'
ARGS = ['--mode', 'auto', '--market', 'HK']


def eligible():
    rows = recovery.gc('run', 'jobs', 'executions', 'list', '--job=' + JOB,
                       '--sort-by=~metadata.creationTimestamp')
    if not isinstance(rows, list):
        raise RuntimeError('HK_EXECUTION_LIST_INVALID')
    if (not rows or recovery.name(rows[0]) != OLD
            or recovery.state(rows[0]) != 'FAILED'
            or any(recovery.state(row) == 'ACTIVE' for row in rows)):
        print('HK_LATEST_CHANGED_OR_NOT_IDLE_NO_START', flush=True)
        return False
    return True


def container(doc):
    rows = list(recovery.containers(doc))
    if len(rows) != 1 or rows[0].get('args') != ARGS or rows[0].get('command'):
        raise RuntimeError('HK_ENTRYPOINT_CHANGED')
    return rows[0]


def preflight(doc):
    """Read the actual HK bindings without printing or persisting credentials."""
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive, validate_phase2_resume
    class Reader(Drive):
        def _call(self, op, **kwargs):
            if op not in {'file', 'list', 'read', 'read_chunk'}:
                raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op, **kwargs)
    saved = dict(os.environ)
    try:
        values = {v['name']: v for v in container(doc).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = deploy.preflight_env_value(doc, values.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
        drive = Reader()
        try:
            cp = validate_phase2_resume(drive, 'HK', DATE)
        finally:
            drive.http.close()
        if cp.get('phase2_completed_date') == DATE:
            print('HK_PHASE2_ALREADY_COMMITTED_NO_START', flush=True)
            return False
        print('HK_DAILY_AND_RANK_VERIFIED=' + DATE, flush=True)
        return True
    finally:
        os.environ.clear()
        os.environ.update(saved)


def build(sha):
    with tempfile.TemporaryDirectory(prefix='hunter-hk-phase2-') as work:
        config = pathlib.Path(work) / 'build.json'
        image = BASE_IMAGE + sha
        config.write_text(json.dumps({'steps': [{'name': 'gcr.io/cloud-builders/docker',
            'args': ['build', '--build-arg', 'HUNTER_SOURCE_SHA=' + sha, '-t', image, '.']}],
            'images': [image], 'timeout': '1800s'}))
        subprocess.run(['gcloud', 'builds', 'submit', str(ROOT), '--config=' + str(config),
                        '--project=' + recovery.PROJECT, '--region=' + recovery.REGION,
                        '--quiet'], check=True)


def configuration(doc):
    return doc['spec']['template'] if 'spec' in doc else doc['template']


def verify_update(before, after, sha):
    expected = copy.deepcopy(container(before))
    expected['image'] = BASE_IMAGE + sha
    values = {v['name']: v for v in expected.get('env', [])}
    values['HUNTER_SOURCE_SHA'] = {'name': 'HUNTER_SOURCE_SHA', 'value': sha}
    expected['env'] = sorted(values.values(), key=lambda v: v['name'])
    actual = copy.deepcopy(container(after))
    actual['env'] = sorted(actual.get('env', []), key=lambda v: v['name'])
    if (actual != expected or deploy.daily_runtime(after) != deploy.daily_runtime(before)
            or deploy.runtime_capacity(after) != deploy.runtime_capacity(before)):
        raise RuntimeError('HK_DEPLOY_READBACK_MISMATCH_NO_START')


def main():
    os.umask(0o077)
    sha = sys.argv[1]
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or head != sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    if not eligible():
        return
    before = recovery.gc('run', 'jobs', 'describe', JOB)
    c = container(before)
    if (c.get('image') not in {BASE_IMAGE + PREVIOUS, BASE_IMAGE + sha}
            or deploy.daily_runtime(before) != (7200, 0)):
        raise RuntimeError('EXPECTED_HK_CONFIGURATION_REQUIRED')
    env = {v['name']: v.get('value') for v in c.get('env', [])}
    if env.get('DERIVED_READ_WORKERS') != '2':
        raise RuntimeError('EXPECTED_HK_READ_CAPACITY_REQUIRED')
    if not preflight(before):
        return
    if c['image'] != BASE_IMAGE + sha:
        build(sha)  # No configuration mutation or task start on build failure.
    if not eligible():
        return
    current = recovery.gc('run', 'jobs', 'describe', JOB)
    if configuration(current) != configuration(before):
        raise RuntimeError('HK_CONFIGURATION_CHANGED_DURING_BUILD')
    recovery.gc('run', 'jobs', 'update', JOB, '--image=' + BASE_IMAGE + sha,
                '--update-env-vars=HUNTER_SOURCE_SHA=' + sha)
    after = recovery.gc('run', 'jobs', 'describe', JOB)
    verify_update(before, after, sha)
    print('HK_DEPLOY_VERIFIED;RUNTIME_CAPACITY_AND_AUTO_ENTRY_UNCHANGED', flush=True)
    if not preflight(after) or not eligible():
        return
    # Only this execution receives the recovery arguments; the nightly job
    # template continues using the original auto/HK arguments.
    override = ARGS + ['--phase2-only', '--as-of', DATE]
    own = recovery.name(recovery.gc('run', 'jobs', 'execute', JOB,
                                   '--args=' + ','.join(override), '--async'))
    if not re.fullmatch(re.escape(JOB) + r'-[a-z0-9]+', own):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_HK_PHASE2_ONLY=' + own, flush=True)
    rows = recovery.gc('run', 'jobs', 'executions', 'list', '--job=' + JOB,
                       '--sort-by=~metadata.creationTimestamp')
    if any(recovery.name(r) != own and recovery.state(r) == 'ACTIVE' for r in rows):
        recovery.gc('run', 'jobs', 'executions', 'cancel', own, '--async')
        raise RuntimeError('CONCURRENT_HK_LAUNCH_OWN_EXECUTION_CANCEL_REQUESTED')
    print('HK_DAILY_NOT_REPLAYED;US_AND_SUPPORT_UNCHANGED;PHASE2_COMPLETION_NOT_YET_VERIFIED', flush=True)


if __name__ == '__main__':
    main()

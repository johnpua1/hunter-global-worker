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


def secret_aliases(doc):
    """Collect Cloud Run v1 secret lookup names without reading any values."""
    aliases = {}
    def visit(node):
        if isinstance(node, dict):
            annotation = node.get('annotations', {}).get('run.googleapis.com/secrets', '')
            for item in annotation.split(','):
                if item.strip():
                    alias, separator, resource = item.strip().partition(':')
                    if not separator or (alias in aliases and aliases[alias] != resource):
                        raise RuntimeError('PREFLIGHT_SECRET_ALIAS_INVALID')
                    aliases[alias] = resource
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(doc)
    return aliases


def preflight_env_value(doc, entry, name):
    """Resolve only an existing binding; never persist or log the secret value."""
    if isinstance(entry.get('value'), str) and entry['value']:
        return entry['value']
    ref = (entry.get('valueFrom', {}).get('secretKeyRef') or
           entry.get('valueSource', {}).get('secretKeyRef') or {})
    secret = ref.get('name') or ref.get('secret', '')
    version = ref.get('key') or ref.get('version', '')
    if not secret or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', version):
        raise RuntimeError('PREFLIGHT_ENV_BINDING_INVALID:' + name)
    secret = secret_aliases(doc).get(secret, secret)
    match = re.fullmatch(r'projects/([A-Za-z0-9_-]+)/secrets/([A-Za-z0-9_-]+)', secret)
    if match:
        project, secret = match.groups()
    elif re.fullmatch(r'[A-Za-z0-9_-]+', secret):
        project = recovery.PROJECT
    else:
        raise RuntimeError('PREFLIGHT_SECRET_REFERENCE_INVALID:' + name)
    try:
        result = subprocess.run(['gcloud', 'secrets', 'versions', 'access', version,
                                 '--secret=' + secret, '--project=' + project, '--quiet'],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('PREFLIGHT_SECRET_ACCESS_FAILED:' + name) from None
    if result.returncode or not result.stdout:
        raise RuntimeError('PREFLIGHT_SECRET_ACCESS_FAILED:' + name)
    # Keep the exact payload. gcloud streams the stored bytes without a newline.
    return result.stdout


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
                'a6de79fb94a1c4fbd9f99312f4744d4aa0d8d67f',
                '6cbba32e4754868e93da9f00ca19371e92a5e8e5',
                '859323f28a76057b51c5e807feec2db6a9aa3b5f',
                '64dbb5ede192a66f4fd16eebef1f33521f07fb7b', sha}
    jobs = {'hunter-us-daily': ['--mode', 'auto', '--market', 'US'],
            'hunter-hk-daily': ['--mode', 'auto', '--market', 'HK'],
            'hunter-maintenance': ['/app/maintenance.py']}
    if '--daily-only' in sys.argv:
        jobs.pop('hunter-maintenance')
    work = pathlib.Path(tempfile.mkdtemp(prefix='hunter-sync-repair-'))
    for job, args in jobs.items():
        before = recovery.gc('run', 'jobs', 'describe', job)
        rows = list(recovery.containers(before))
        if (len(rows) != 1 or rows[0].get('args') != args or
                rows[0].get('image') not in {base + ':' + s for s in approved}):
            raise RuntimeError('UNEXPECTED_JOB_CONFIGURATION:' + job)
        (work / (job + '.before.json')).write_text(json.dumps(before))
    if '--daily-only' in sys.argv:
        # Verify the exact URLs/credentials currently configured in Cloud Run,
        # not the GitHub diagnostic secret. Values stay in this process only.
        sys.path.insert(0, str(ROOT / 'hunter-global'))
        from runner import Drive
        original_env = dict(os.environ)
        try:
            for job in jobs:
                before = json.loads((work / (job + '.before.json')).read_text())
                values = {v['name']: v for v in list(recovery.containers(before))[0].get('env', [])}
                for name in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
                    os.environ[name] = preflight_env_value(before, values.get(name, {}), name)
                os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
                market = job.split('-')[1].upper()
                doc = Drive().json(market + '/CURRENT_UNIVERSE.json')
                if doc.get('market') != market or not doc.get('securities'):
                    raise RuntimeError('PREFLIGHT_CONTENT_INVALID:' + market)
                print('PRODUCTION_CONFIG_FULL_READ_PASS=' + market + ':' + str(len(doc['securities'])), flush=True)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
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

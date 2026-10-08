"""Update only the US template; preserve every existing execution and HK image."""
import copy
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
US_PREVIOUS = '364b5826e04a3e0bfa74dbad078f7376c67b8fe2'
HK_FIXED = 'f6df22e438c8da201bc1759826bd84b72dda299c'
BASE_IMAGE = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'


def configuration(r, doc):
    result = copy.deepcopy(r.base.configuration(doc))
    # These two annotations identify the CLI issuing the update, not workload
    # configuration. All security/network/service-account annotations remain.
    def clean(node):
        if not isinstance(node, dict):
            return
        meta = node.get('metadata')
        if isinstance(meta, dict):
            ann = meta.get('annotations')
            if isinstance(ann, dict):
                for key in ('run.googleapis.com/client-name', 'run.googleapis.com/client-version'):
                    ann.pop(key, None)
                if not ann:
                    meta.pop('annotations', None)
            if not meta:
                node.pop('metadata', None)
        for value in list(node.values()):
            if isinstance(value, dict):
                clean(value)
            elif isinstance(value, list):
                for item in value:
                    clean(item)
    clean(result)
    for c in r.base.recovery.containers(result):
        if 'env' in c:
            c['env'] = sorted(c['env'], key=lambda e: e['name'])
    return result


def snapshot(r):
    return {m: r.gc('run', 'jobs', 'describe', 'hunter-' + m.lower() + '-daily')
            for m in ('US', 'HK')}


def unchanged(r, before, after, stage):
    for market in ('US', 'HK'):
        if configuration(r, before[market]) != configuration(r, after[market]):
            raise RuntimeError('PACK_FIX_CONFIGURATION_CHANGED_' + stage + ':' + market)


def identity(r, doc, market, sha):
    c = r.deployment.container(doc, market)
    env = c.get('env', [])
    if len({e['name'] for e in env}) != len(env):
        raise RuntimeError('PACK_FIX_DUPLICATE_ENVIRONMENT_NAMES')
    source = next((e.get('value') for e in env if e['name'] == 'HUNTER_SOURCE_SHA'), None)
    allowed = {US_PREVIOUS, sha} if market == 'US' else {HK_FIXED}
    if source not in allowed or c.get('image') != BASE_IMAGE + source:
        raise RuntimeError('PACK_FIX_EXPECTED_DEPLOYMENT_REQUIRED:' + market)
    r.verify(doc, doc, market, source)
    if r.base.deploy.daily_runtime(doc) != (7200, 0):
        raise RuntimeError('PACK_FIX_EXPECTED_RUNTIME_REQUIRED:' + market)
    r.base.deploy.runtime_capacity(doc)
    return source


def deploy_us(r, sha):
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if (not re.fullmatch('[a-f0-9]{40}', sha) or head != sha
            or pathlib.Path(r.base.ROOT).resolve() != ROOT or r.base.BASE_IMAGE != BASE_IMAGE):
        raise RuntimeError('PACK_FIX_PINNED_CHECKOUT_REQUIRED')
    before = snapshot(r)
    old = identity(r, before['US'], 'US', sha)
    identity(r, before['HK'], 'HK', sha)
    for market in ('US', 'HK'):
        active = [r.base.recovery.name(row) for row in r.executions(market)
                  if r.base.recovery.state(row) == 'ACTIVE']
        if active:
            print('PACK_FIX_' + market + '_EXECUTION_PRESERVED=' + ','.join(active), flush=True)
    if old == sha:
        current = snapshot(r)
        unchanged(r, before, current, 'DURING_VERIFY')
        print('US_PACK_FIX_ALREADY_DEPLOYED_VERIFIED=' + sha, flush=True)
        return {'status': 'VERIFIED', 'docs': current}
    print('US_PACK_FIX_BUILD_START=' + sha, flush=True)
    r.base.build(sha)
    current = snapshot(r)
    unchanged(r, before, current, 'DURING_BUILD')
    # A template update does not cancel or replace an active execution. The
    # closeout controller waits for it before launching any bounded resume.
    r.gc('run', 'jobs', 'update', 'hunter-us-daily', '--image=' + BASE_IMAGE + sha,
         '--update-env-vars=HUNTER_SOURCE_SHA=' + sha)
    after = snapshot(r)
    expected = copy.deepcopy(before)
    c = r.deployment.container(expected['US'], 'US')
    c['image'] = BASE_IMAGE + sha
    for e in c['env']:
        if e['name'] == 'HUNTER_SOURCE_SHA':
            e['value'] = sha
    unchanged(r, expected, after, 'AFTER_UPDATE')
    identity(r, after['US'], 'US', sha)
    identity(r, after['HK'], 'HK', sha)
    print('US_PACK_FIX_DEPLOY_VERIFIED=' + sha + ';HK_CONFIG_UNCHANGED', flush=True)
    return {'status': 'VERIFIED', 'docs': after}

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
            labels = meta.get('labels')
            if isinstance(labels, dict):
                labels.pop('client.knative.dev/nonce', None)
                if not labels:
                    meta.pop('labels', None)
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
            print('PACK_FIX_DIFFERENT_PATHS=' + ','.join(different_paths(
                configuration(r, before[market]), configuration(r, after[market]))), flush=True)
            raise RuntimeError('PACK_FIX_CONFIGURATION_CHANGED_' + stage + ':' + market)


def different_paths(left, right, path='template'):
    """Report structure only; never print environment values or secrets."""
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        return [p for k in sorted(set(left) | set(right))
                for p in ([path + '.' + k] if k not in left or k not in right
                          else different_paths(left[k], right[k], path + '.' + k))]
    if isinstance(left, list):
        if len(left) != len(right):
            return [path + '.length']
        return [p for i, (a, b) in enumerate(zip(left, right))
                for p in different_paths(a, b, path + '[' + str(i) + ']')]
    return [] if left == right else [path]


def verify_deployed_us(r, sha):
    """Read-only recovery after the known update; never rebuild or update.

    Compare the current US workload to the immutable pre-update execution,
    permitting only the requested image/source change. Then read both jobs
    again before handing them to the monitor.
    """
    before = snapshot(r)
    if identity(r, before['US'], 'US', sha) != sha:
        raise RuntimeError('PACK_FIX_NEW_IMAGE_NOT_DEPLOYED')
    identity(r, before['HK'], 'HK', sha)
    reference = r.gc('run', 'jobs', 'executions', 'describe', 'hunter-us-daily-55g2b')
    if reference.get('metadata', {}).get('name') != 'hunter-us-daily-55g2b':
        raise RuntimeError('PACK_FIX_REFERENCE_EXECUTION_MISMATCH')
    if 'spec' not in reference or 'spec' not in before['US']:
        raise RuntimeError('PACK_FIX_REFERENCE_SCHEMA_UNSUPPORTED')
    expected = copy.deepcopy(reference['spec'])
    c = r.deployment.container(expected, 'US')
    env = c.get('env', [])
    source = [e.get('value') for e in env if e['name'] == 'HUNTER_SOURCE_SHA']
    if source != [US_PREVIOUS]:
        raise RuntimeError('PACK_FIX_REFERENCE_SOURCE_MISMATCH')
    c['image'] = BASE_IMAGE + sha
    for e in env:
        if e['name'] == 'HUNTER_SOURCE_SHA':
            e['value'] = sha
    expected_doc = {'spec': {'template': {'spec': expected}}}
    actual_doc = {'spec': {'template': {'spec': before['US']['spec']['template']['spec']}}}
    if configuration(r, expected_doc) != configuration(r, actual_doc):
        print('PACK_FIX_DIFFERENT_PATHS=' + ','.join(different_paths(
            configuration(r, expected_doc), configuration(r, actual_doc))), flush=True)
        raise RuntimeError('PACK_FIX_REFERENCE_WORKLOAD_MISMATCH')
    def runtime_annotations(meta):
        # Execution metadata records its creator/last modifier. Those audit
        # identities are not settings inherited from the job's task template.
        ignored = {'run.googleapis.com/client-name', 'run.googleapis.com/client-version',
                   'run.googleapis.com/operation-id', 'run.googleapis.com/creator',
                   'run.googleapis.com/lastModifier'}
        return {k: v for k, v in meta.get('annotations', {}).items()
                if k.startswith('run.googleapis.com/') and k not in ignored}
    old_annotations = runtime_annotations(reference.get('metadata', {}))
    new_annotations = runtime_annotations(before['US']['spec']['template'].get('metadata', {}))
    if old_annotations != new_annotations:
        print('PACK_FIX_DIFFERENT_PATHS=' + ','.join(different_paths(
            old_annotations, new_annotations, 'runtime_annotations')), flush=True)
        raise RuntimeError('PACK_FIX_REFERENCE_ANNOTATIONS_MISMATCH')
    current = snapshot(r)
    unchanged(r, before, current, 'DURING_VERIFY')
    print('US_PACK_FIX_RECOVERY_VERIFIED=' + sha + ';REFERENCE=hunter-us-daily-55g2b;NO_BUILD_OR_UPDATE', flush=True)
    return {'status': 'VERIFIED', 'docs': current}


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

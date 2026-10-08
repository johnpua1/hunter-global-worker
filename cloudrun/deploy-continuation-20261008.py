"""Install one new image on BOTH markets; no worker rollback or mixed launch."""
import copy
import importlib.util
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configuration_compare', ROOT / 'cloudrun/deploy-us-pack-readback-20261008.py')
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)
configuration = compare.configuration
BASE_IMAGE = compare.BASE_IMAGE
# Existing source IDs are accepted solely as update inputs, never as launch targets.
EXISTING = {'US': 'bdc5e299643bc89ebb9a093b411d64fc657cadf2',
            'HK': 'f6df22e438c8da201bc1759826bd84b72dda299c'}


def desired(doc, r, market, sha):
    result = copy.deepcopy(doc)
    c = r.deployment.container(result, market)
    c['image'] = BASE_IMAGE + sha
    env = {e['name']: e for e in c.get('env', [])}
    for key, value in {'HUNTER_SOURCE_SHA': sha, 'HUNTER_CONTINUE_ONLY': '1',
                       'HUNTER_INCREMENTAL_INPUTS': '1', 'HUNTER_RESUMABLE_RUN': '1',
                       'HUNTER_READ_CHECKPOINTS': '1'}.items():
        env[key] = {'name': key, 'value': value}
    c['env'] = list(env.values())
    return result


def verify_current(r, sha):
    """Attach to already deployed new workers; no build/update/cancellation."""
    before = compare.snapshot(r)
    for market, doc in before.items():
        if configuration(r, doc) != configuration(r, desired(doc, r, market, sha)):
            raise RuntimeError('CONTINUATION_NEW_TEMPLATE_REQUIRED:' + market)
        r.verify(doc, doc, market, sha)
        if r.base.deploy.daily_runtime(doc) != (7200, 0):
            raise RuntimeError('CONTINUATION_UNEXPECTED_RUNTIME:' + market)
    current = compare.snapshot(r)
    compare.unchanged(r, before, current, 'DURING_ATTACH')
    print('CONTINUATION_ATTACHED_SAME_WORKER=' + sha + ';NO_BUILD_UPDATE_OR_CANCEL', flush=True)
    return {'status': 'VERIFIED', 'docs': current, 'cancelled': set()}


def deploy_both(r, sha):
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch('[a-f0-9]{40}', sha) or head != sha or pathlib.Path(r.base.ROOT).resolve() != ROOT:
        raise RuntimeError('CONTINUATION_PINNED_CHECKOUT_REQUIRED')
    before = compare.snapshot(r)
    for market, doc in before.items():
        c = r.deployment.container(doc, market)
        env = c.get('env', [])
        if len({e['name'] for e in env}) != len(env):
            raise RuntimeError('CONTINUATION_DUPLICATE_ENV')
        source = next((e.get('value') for e in env if e['name'] == 'HUNTER_SOURCE_SHA'), None)
        if source not in {EXISTING[market], sha} or c['image'] != BASE_IMAGE + source:
            raise RuntimeError('CONTINUATION_UNEXPECTED_TEMPLATE:' + market)
        if r.base.deploy.daily_runtime(doc) != (7200, 0):
            raise RuntimeError('CONTINUATION_UNEXPECTED_RUNTIME:' + market)
    expected = {m: desired(d, r, m, sha) for m, d in before.items()}
    if any(configuration(r, before[m]) != configuration(r, expected[m]) for m in before):
        print('CONTINUATION_BUILD_START=' + sha, flush=True)
        r.base.build(sha)
    compare.unchanged(r, before, compare.snapshot(r), 'DURING_BUILD')
    for market in ('US', 'HK'):
        if configuration(r, before[market]) != configuration(r, expected[market]):
            r.gc('run', 'jobs', 'update', 'hunter-' + market.lower() + '-daily',
                 '--image=' + BASE_IMAGE + sha,
                 '--update-env-vars=HUNTER_SOURCE_SHA=' + sha + ',HUNTER_CONTINUE_ONLY=1,HUNTER_INCREMENTAL_INPUTS=1,HUNTER_RESUMABLE_RUN=1,HUNTER_READ_CHECKPOINTS=1')
        current = r.gc('run', 'jobs', 'describe', 'hunter-' + market.lower() + '-daily')
        if configuration(r, current) != configuration(r, expected[market]):
            raise RuntimeError('CONTINUATION_TEMPLATE_MISMATCH:' + market)
    after = compare.snapshot(r)
    compare.unchanged(r, expected, after, 'AFTER_UPDATE')
    # Both templates are now the same new image. Stop only old executions of
    # these two daily jobs. Never deploy or launch either previous source ID.
    cancelled = set()
    for market in ('US', 'HK'):
        job = 'hunter-' + market.lower() + '-daily'
        for row in r.executions(market):
            if r.base.recovery.state(row) != 'ACTIVE':
                continue
            name = r.base.recovery.name(row)
            if not re.fullmatch(re.escape(job) + r'-[a-z0-9]+', name):
                raise RuntimeError('CONTINUATION_EXECUTION_ID_INVALID')
            execution = r.gc('run', 'jobs', 'executions', 'describe', name)
            containers = list(r.base.recovery.containers(execution))
            if len(containers) != 1:
                raise RuntimeError('CONTINUATION_EXECUTION_CONTAINER_INVALID')
            c = containers[0]
            if any(e.get('name') == 'HUNTER_SOURCE_SHA' and e.get('value') == sha for e in c.get('env', [])):
                continue
            r.gc('run', 'jobs', 'executions', 'cancel', name, '--async')
            cancelled.add(name)
            print('OLD_EXECUTION_CANCEL_REQUESTED=' + name + ';SAVED_DATA_PRESERVED', flush=True)
    print('US_HK_SAME_NEW_VERSION=' + sha + ';NO_ROLLBACK', flush=True)
    return {'status': 'VERIFIED', 'docs': after, 'cancelled': cancelled}

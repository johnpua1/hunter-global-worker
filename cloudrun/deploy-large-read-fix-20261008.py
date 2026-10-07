"""One pinned deployment, guarded Phase 2 recovery, and bounded live follow-up.

Preserves active executions and the existing schedules/resources/7200s budget.
Retries only time-budget interruptions, at most three launches per market.
"""
import copy
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('durable_recovery_base',
    ROOT / 'cloudrun/resume-both-phase2-incremental-20261007.py')
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)
deployment = resume.deployment
base = resume.base
gc = base.recovery.gc
PREVIOUS = 'fbff4716b50c8593180d6bcee3789a5fdb31e950'
PRIOR_LARGE_READ = '2668473a8cb4700134fd22cbd2b490ba3ebd9ac9'


def probe_metadata(reader, market):
    path = market + '/PHASE2/EARNINGS_HISTORY.json'
    reason = 'BRIDGE_VERSION_NOT_SERVING'
    for attempt in range(6):
        response = reader._call('file', path=path, _attempts=2)
        info = response.get('file')
        if response.get('read_protocol') != 'verified-chunks-v2':
            reason = 'BRIDGE_VERSION_NOT_SERVING'
        elif info is None:
            reason = 'PHASE2_HISTORY_FILE_MISSING'
        elif not info.get('revision'):
            raise RuntimeError(market + '_BRIDGE_REVISION_FIELD_MISSING')
        elif int(info.get('size') or 0) <= 0:
            raise RuntimeError(market + '_PHASE2_HISTORY_FILE_EMPTY')
        else:
            return path, info
        if attempt < 5:
            print('BRIDGE_PREFLIGHT_WAIT=' + market + ';reason=' + reason
                  + ';attempt=' + str(attempt + 1), flush=True)
            time.sleep(5)
    raise RuntimeError(market + '_' + reason)


def preflight_bridge(doc, market):
    """Verify the real compressed endpoint before building/enabling the worker."""
    import base64
    import gzip
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive, digest
    class Reader(Drive):
        def _call(self, op, **kwargs):
            if op not in {'file', 'read', 'read_chunk', 'read_verified_chunk', 'source_inventory'}:
                raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op, **kwargs)
    saved = dict(os.environ)
    try:
        env = {e['name']: e for e in deployment.container(doc, market).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = base.deploy.preflight_env_value(doc, env.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
        reader = Reader()
        try:
            path, info = probe_metadata(reader, market)
            length = min(131072, int(info['size']))
            result = reader._call('read_verified_chunk', path=path, revision=info['revision'],
                                  offset=0, length=length, _attempts=2)
            encoded = base64.b64decode(result['data_base64'], validate=True)
            data = gzip.decompress(encoded)
            if (result['revision'] != info['revision'] or result['size'] != int(info['size'])
                    or result['offset'] != 0 or result['length'] != length or len(data) != length
                    or result['encoding'] != 'gzip' or digest(data) != result['sha256']
                    or digest(encoded) != result['compressed_sha256']):
                raise RuntimeError('COMPRESSED_BRIDGE_PREFLIGHT_FAILED')
            print('COMPRESSED_READ_VERIFIED=' + market + ';raw_bytes=' + str(len(data))
                  + ';transfer_bytes=' + str(len(encoded)), flush=True)
        finally:
            reader.http.close()
    finally:
        os.environ.clear(); os.environ.update(saved)


def verify(before, after, market, sha):
    expected = copy.deepcopy(deployment.container(before, market))
    expected['image'] = base.BASE_IMAGE + sha
    env = {v['name']: v for v in expected.get('env', [])}
    for key, value in {'HUNTER_SOURCE_SHA': sha, 'HUNTER_INCREMENTAL_INPUTS': '1',
                       'HUNTER_RESUMABLE_RUN': '1', 'HUNTER_READ_CHECKPOINTS': '1'}.items():
        env[key] = {'name': key, 'value': value}
    expected['env'] = sorted(env.values(), key=lambda v: v['name'])
    actual = copy.deepcopy(deployment.container(after, market))
    actual['env'] = sorted(actual.get('env', []), key=lambda v: v['name'])
    if (actual != expected or base.deploy.daily_runtime(after) != base.deploy.daily_runtime(before)
            or base.deploy.runtime_capacity(after) != base.deploy.runtime_capacity(before)):
        raise RuntimeError('DEPLOY_READBACK_MISMATCH')


def deploy(sha):
    docs = {}
    for market in resume.TARGETS:
        doc = gc('run', 'jobs', 'describe', 'hunter-' + market.lower() + '-daily')
        c = deployment.container(doc, market)
        if (c['image'] not in {base.BASE_IMAGE + PREVIOUS, base.BASE_IMAGE + PRIOR_LARGE_READ, base.BASE_IMAGE + sha}
                or base.deploy.daily_runtime(doc) != (7200, 0)):
            raise RuntimeError('EXPECTED_DEPLOYMENT_REQUIRED')
        docs[market] = doc
        preflight_bridge(doc, market)
    if any(deployment.container(d, m)['image'] != base.BASE_IMAGE + sha for m, d in docs.items()):
        base.build(sha)
    for market, before in docs.items():
        current = gc('run', 'jobs', 'describe', 'hunter-' + market.lower() + '-daily')
        if base.configuration(current) != base.configuration(before):
            raise RuntimeError('CONFIGURATION_CHANGED_DURING_BUILD')
    for market, before in docs.items():
        job = 'hunter-' + market.lower() + '-daily'
        env = {e['name']: e.get('value') for e in deployment.container(before, market).get('env', [])}
        if env.get('HUNTER_SOURCE_SHA') != sha or env.get('HUNTER_RESUMABLE_RUN') != '1' or env.get('HUNTER_INCREMENTAL_INPUTS') != '1' or env.get('HUNTER_READ_CHECKPOINTS') != '1':
            gc('run', 'jobs', 'update', job, '--image=' + base.BASE_IMAGE + sha,
               '--update-env-vars=HUNTER_SOURCE_SHA=' + sha + ',HUNTER_INCREMENTAL_INPUTS=1,HUNTER_RESUMABLE_RUN=1,HUNTER_READ_CHECKPOINTS=1')
        after = gc('run', 'jobs', 'describe', job)
        verify(before, after, market, sha)
        docs[market] = after
        print('RESUMABLE_DEPLOY_VERIFIED=' + job, flush=True)
    return docs


def executions(market):
    rows = gc('run', 'jobs', 'executions', 'list', '--job=hunter-' + market.lower() + '-daily',
              '--sort-by=~metadata.creationTimestamp')
    if not isinstance(rows, list): raise RuntimeError('EXECUTION_LIST_INVALID')
    return rows


def latest_failure(market, rows):
    if any(base.recovery.state(r) == 'ACTIVE' for r in rows): return None
    if not rows or base.recovery.state(rows[0]) != 'FAILED': return None
    return base.recovery.name(rows[0])


def timeout_failure(name):
    doc = gc('run', 'jobs', 'executions', 'describe', name)
    status = doc.get('status', doc)
    messages = ' '.join(c.get('message', '') for c in status.get('conditions', []))
    if 'timeout' in messages.lower(): return True
    query = ('resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="'
             + name + '" AND ("WORK_BUDGET_EXHAUSTED_RESUME_REQUIRED" OR "LARGE_READ_TRANSPORT_RETRY_REQUIRED")')
    return bool(gc('logging', 'read', query, '--freshness=1d', '--limit=1'))


def launch(market, before, sha, expected):
    job = 'hunter-' + market.lower() + '-daily'
    if not resume.preflight(before, market): return None
    current = gc('run', 'jobs', 'describe', job)
    verify(before, current, market, sha)
    if base.configuration(current) != base.configuration(before):
        raise RuntimeError('CONFIGURATION_CHANGED_NO_START')
    if latest_failure(market, executions(market)) != expected:
        print(market + '_EXECUTION_CHANGED_NO_START', flush=True)
        return None
    args = ['--mode', 'auto', '--market', market, '--phase2-only', '--as-of', resume.TARGETS[market][1]]
    own = base.recovery.name(gc('run', 'jobs', 'execute', job, '--args=' + ','.join(args), '--async'))
    if not re.fullmatch(re.escape(job) + r'-[a-z0-9]+', own):
        raise RuntimeError('LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY')
    print('STARTED_' + market + '_RESUMABLE_PHASE2=' + own, flush=True)
    if any(base.recovery.name(r) != own and base.recovery.state(r) == 'ACTIVE' for r in executions(market)):
        gc('run', 'jobs', 'executions', 'cancel', own, '--async')
        print(market + '_CONCURRENT_EXECUTION_PRESERVED_OWN_CANCEL_REQUESTED', flush=True)
    return own


def checkpoint(doc, market):
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive
    saved = dict(os.environ)
    try:
        env = {v['name']: v for v in deployment.container(doc, market).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = base.deploy.preflight_env_value(doc, env.get(key, {}), key)
        reader = Drive()
        try: cp = reader.json(market + '/CONTROL/DAILY_CHECKPOINT.json')
        finally: reader.http.close()
        if cp.get('market') != market: raise RuntimeError('CHECKPOINT_IDENTITY_MISMATCH')
        return cp
    finally:
        os.environ.clear(); os.environ.update(saved)


def follow(docs, sha):
    completed, stopped = set(), set()
    counts = {m: 0 for m in docs}
    last_seen = {}
    deadline = time.monotonic() + 7 * 3600
    while time.monotonic() < deadline and len(completed | stopped) < len(docs):
        for market, doc in docs.items():
            if market in completed | stopped: continue
            rows = executions(market)
            active = [base.recovery.name(r) for r in rows if base.recovery.state(r) == 'ACTIVE']
            if active:
                print(market + '_RUNNING_PRESERVED=' + ','.join(active), flush=True)
                # Keep the operator informed without printing raw URLs or secrets.
                query = ('resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="'
                         + active[0] + '" AND ("PHASE2_" OR "INPUT_CACHE_" OR "INPUT_PACK_" OR "LARGE_READ_")')
                logs = gc('logging', 'read', query, '--freshness=1d', '--limit=1', '--order=desc')
                if logs:
                    item = logs[0]
                    text = item.get('textPayload') or item.get('jsonPayload', {}).get('message', '')
                    match = re.search(r'(?:PHASE2_|INPUT_CACHE_|INPUT_PACK_|LARGE_READ_)[A-Z_]+[^\n]*', str(text))
                    if match and last_seen.get(market) != item.get('timestamp'):
                        print(market + '_PROGRESS=' + re.sub(r'https?://\S+', '[URL]', match.group())[:350], flush=True)
                        last_seen[market] = item.get('timestamp')
                continue
            cp = checkpoint(doc, market)
            date = resume.TARGETS[market][1]
            if cp.get('last_completed_date') == date and cp.get('phase2_completed_date') == date:
                print('PHASE2_COMPLETION_VERIFIED=' + market + ';date=' + date, flush=True)
                completed.add(market); continue
            failed = latest_failure(market, rows)
            if not failed or counts[market] >= 3 or not timeout_failure(failed):
                print(market + '_NOT_COMPLETE_NO_BLIND_RETRY', flush=True)
                stopped.add(market); continue
            own = launch(market, doc, sha, failed)
            if own: counts[market] += 1
        if len(completed | stopped) < len(docs): time.sleep(45)
    if len(completed) != len(docs):
        raise RuntimeError('PHASE2_COMPLETION_NOT_VERIFIED')
    print('US_AND_HK_PHASE2_COMPLETION_VERIFIED', flush=True)


def main():
    sha = sys.argv[1] if len(sys.argv) == 2 else ''
    os.umask(0o077)
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() != sha:
        raise RuntimeError('PINNED_CHECKOUT_REQUIRED')
    follow(deploy(sha), sha)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc)
        safe = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', reason) else type(exc).__name__
        print('RECOVERY_STOPPED=' + safe, flush=True)
        sys.exit(1)

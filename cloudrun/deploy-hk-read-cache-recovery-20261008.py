"""Deploy one pinned HK worker fix without stopping or starting executions.

The caller owns the closeout lock and polls again on WAIT_HK_ACTIVE. All
configuration changes except HK image/source identity are fatal. This module
never invokes the old deployment entrypoints or changes the Apps Script Bridge.
"""
from __future__ import annotations

import copy
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREVIOUS = '2e8dd74363e2f13ea15f7a1ad1d6b24624e88086'
BASE_IMAGE = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
_BUILT_SHAS = set()


def _pinned(r, sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha or '') or sha == PREVIOUS:
        raise RuntimeError('HK_FIX_PINNED_CHECKOUT_REQUIRED')
    try:
        head = subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('HK_FIX_PINNED_CHECKOUT_REQUIRED') from None
    if head != sha:
        raise RuntimeError('HK_FIX_PINNED_CHECKOUT_REQUIRED')
    if pathlib.Path(r.base.ROOT).resolve() != ROOT.resolve() or r.base.BASE_IMAGE != BASE_IMAGE:
        raise RuntimeError('HK_FIX_BUILD_ROOT_MISMATCH')


def _configuration(r, doc):
    result = copy.deepcopy(r.base.configuration(doc))
    for container in r.base.recovery.containers(result):
        if 'env' in container:
            container['env'] = sorted(container['env'], key=lambda entry: entry['name'])
    return result


def _snapshot(r):
    docs, active = {}, {}
    for market in ('US', 'HK'):
        docs[market] = r.gc('run', 'jobs', 'describe', 'hunter-' + market.lower() + '-daily')
        r.deployment.container(docs[market], market)
        rows = r.executions(market)
        active[market] = [r.base.recovery.name(row) for row in rows
                          if r.base.recovery.state(row) == 'ACTIVE']
        if any(not name for name in active[market]):
            raise RuntimeError('HK_FIX_EXECUTION_NAME_MISSING')
    return docs, active


def _unchanged(r, before, after, label):
    for market in ('US', 'HK'):
        if _configuration(r, before[market]) != _configuration(r, after[market]):
            raise RuntimeError('HK_FIX_CONFIGURATION_CHANGED_' + label + ':' + market)


def _identity(r, doc, sha, market='HK'):
    container = r.deployment.container(doc, market)
    entries = container.get('env', [])
    if len({entry['name'] for entry in entries}) != len(entries):
        raise RuntimeError('HK_FIX_DUPLICATE_ENVIRONMENT_NAMES')
    source = next((entry.get('value') for entry in entries
                   if entry['name'] == 'HUNTER_SOURCE_SHA'), None)
    allowed = {PREVIOUS, sha} if market == 'HK' else {PREVIOUS}
    if source not in allowed or container.get('image') != BASE_IMAGE + source:
        raise RuntimeError('HK_FIX_EXPECTED_DEPLOYMENT_REQUIRED:' + market)
    # Existing flags, budget and both image/source identities must be valid
    # before a build or update, rather than failing only in the later monitor.
    r.verify(doc, doc, market, source)
    if r.base.deploy.daily_runtime(doc) != (7200, 0):
        raise RuntimeError('HK_FIX_EXPECTED_RUNTIME_REQUIRED:' + market)
    r.base.deploy.runtime_capacity(doc)
    return source


def _result(status, docs, active, sha, updated=False):
    if status == 'WAIT_HK_ACTIVE':
        print('HK_FIX_WAIT_ACTIVE=' + ','.join(active['HK']), flush=True)
    return {'status': status, 'docs': docs, 'active': active,
            'built': sha in _BUILT_SHAS, 'updated': updated}


def deploy_hk(r, fix_sha):
    """Return VERIFIED or WAIT_HK_ACTIVE with docs/active/built/updated.

    WAIT is a non-error and never updates an active HK job. US may remain active
    throughout. Any pin/configuration/build/update/readback failure propagates;
    the caller must not launch a recovery on that failure or retry an ambiguous
    update blindly. A later call re-reads the actual HK identity before writing.
    """
    _pinned(r, fix_sha)
    before, active = _snapshot(r)
    _identity(r, before['US'], fix_sha, 'US')
    current_sha = _identity(r, before['HK'], fix_sha)
    if active['US']:
        print('HK_FIX_US_EXECUTION_PRESERVED=' + ','.join(active['US']), flush=True)

    if current_sha == fix_sha:
        current, active = _snapshot(r)
        _unchanged(r, before, current, 'DURING_VERIFY')
        _identity(r, current['HK'], fix_sha)
        print('HK_FIX_ALREADY_DEPLOYED_VERIFIED=' + fix_sha, flush=True)
        return _result('VERIFIED', current, active, fix_sha)
    if active['HK']:
        return _result('WAIT_HK_ACTIVE', before, active, fix_sha)

    if fix_sha not in _BUILT_SHAS:
        print('HK_FIX_BUILD_START=' + fix_sha, flush=True)
        r.base.build(fix_sha)
        _BUILT_SHAS.add(fix_sha)
        print('HK_FIX_BUILD_DONE=' + fix_sha, flush=True)

    current, active = _snapshot(r)
    _unchanged(r, before, current, 'DURING_BUILD')
    if active['HK']:
        return _result('WAIT_HK_ACTIVE', current, active, fix_sha)

    # Recheck both templates and HK activity immediately before the sole write.
    latest, active = _snapshot(r)
    _unchanged(r, before, latest, 'BEFORE_UPDATE')
    if active['HK']:
        return _result('WAIT_HK_ACTIVE', latest, active, fix_sha)
    print('HK_FIX_UPDATE_START=' + fix_sha, flush=True)
    r.gc('run', 'jobs', 'update', 'hunter-hk-daily',
         '--image=' + BASE_IMAGE + fix_sha,
         '--update-env-vars=HUNTER_SOURCE_SHA=' + fix_sha)

    after, active = _snapshot(r)
    expected = copy.deepcopy(before)
    container = r.deployment.container(expected['HK'], 'HK')
    container['image'] = BASE_IMAGE + fix_sha
    for entry in container['env']:
        if entry['name'] == 'HUNTER_SOURCE_SHA':
            entry['value'] = fix_sha
    _unchanged(r, expected, after, 'AFTER_UPDATE')
    _identity(r, after['HK'], fix_sha)
    print('HK_FIX_DEPLOY_VERIFIED=' + fix_sha + ';US_CONFIG_UNCHANGED', flush=True)
    return _result('VERIFIED', after, active, fix_sha, updated=True)

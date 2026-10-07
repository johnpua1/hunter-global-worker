"""Resolve the jobs' actual Bridge bindings and verify immutable deployed code."""
import importlib.util
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def bound_deployments():
    spec = importlib.util.spec_from_file_location('large_read_release',
        ROOT / 'cloudrun/deploy-large-read-fix-20261008.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    result = []
    for market in ('US', 'HK'):
        doc = release.gc('run', 'jobs', 'describe', 'hunter-' + market.lower() + '-daily')
        env = {e['name']: e for e in release.deployment.container(doc, market).get('env', [])}
        url = release.base.deploy.preflight_env_value(doc, env.get('APPS_SCRIPT_WEBAPP_URL', {}),
                                                      'APPS_SCRIPT_WEBAPP_URL')
        match = re.fullmatch(r'https://script\.google\.com/macros/s/([A-Za-z0-9_-]+)/exec', url)
        if not match:
            raise RuntimeError(market + '_BRIDGE_BINDING_INVALID')
        if match[1] not in result:
            result.append(match[1])
    return result


def deployment_version(listing, deployment_id):
    matches = re.findall(r'^\s*-\s+' + re.escape(deployment_id) + r'\s+@(\d+)(?:\s|$)',
                         listing, re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError('BOUND_DEPLOYMENT_NOT_IN_SELECTED_SCRIPT')
    return matches[0]


def verify_source(directory):
    candidates = [p for p in pathlib.Path(directory).rglob('*')
                  if p.is_file() and p.stem == 'Gateway' and p.suffix in ('.gs', '.js', '.ts')]
    expected = (ROOT / 'bridge/Gateway.gs').read_text().strip()
    if len(candidates) != 1 or candidates[0].read_text().strip() != expected:
        raise RuntimeError('DEPLOYED_GATEWAY_SOURCE_MISMATCH')


def run_clasp(*args, cwd=None):
    result = subprocess.run(['npx', '-y', '@google/clasp@3.4.1', *args],
                            cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('BRIDGE_RELEASE_READBACK_FAILED')
    return result.stdout


def main():
    if sys.argv[1:] == ['bindings']:
        print('\n'.join(bound_deployments()))
        return
    mode, script_id, deployment_id = sys.argv[1:]
    version = deployment_version(run_clasp('list-deployments'), deployment_id)
    if mode == 'verify':
        with tempfile.TemporaryDirectory(prefix='hunter-bridge-readback-') as tmp:
            run_clasp('clone-script', script_id, version, cwd=tmp)
            verify_source(tmp)
        print('DEPLOYED_BRIDGE_SOURCE_VERIFIED=version_' + version, flush=True)
    elif mode != 'resolve':
        raise RuntimeError('BRIDGE_RELEASE_CHECK_MODE_INVALID')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc)
        print(reason if re.fullmatch('[A-Z][A-Z0-9_]+', reason) else type(exc).__name__, file=sys.stderr)
        sys.exit(1)

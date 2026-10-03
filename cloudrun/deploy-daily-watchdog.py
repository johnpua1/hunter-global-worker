#!/usr/bin/env python3
"""Patch only daily compensation/admin functions and add one hourly trigger."""
import importlib.util
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
BASE = '617c3602340adddc7e94e709ccc2cec422770f81'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'cloudrun' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def stage_watchdog(directory, expected):
    """Reject unknown code or duplicate handlers; never overwrite unrelated files."""
    names = re.findall(r'^function (\w+)\(', expected, re.M)
    target = directory / 'Watchdog.gs'
    changed = True
    for path in directory.rglob('*'):
        if not path.is_file() or path.suffix not in ('.gs', '.js'):
            continue
        content = path.read_text()
        if path.stem == 'Watchdog':
            if path.parent != directory or content != expected:
                raise RuntimeError('LIVE_WATCHDOG_DIFFERS_FROM_REVIEWED_SOURCE')
            target, changed = path, False
            continue
        if any(re.search(r'function\s+' + re.escape(name) + r'\s*\(', content) for name in names):
            raise RuntimeError('WATCHDOG_HANDLER_ALREADY_EXISTS_ELSEWHERE')
    if changed:
        target.write_text(expected)
    return changed


def main():
    deploy = module('watchdog_scoped_deploy', 'deploy-monthly-retry.py')
    deploy.LAUNCH_BASE = BASE
    deploy.LAUNCH_FUNCTIONS = ('doPost', 'runHunterJob_')
    original_patch = deploy.patch_launch
    expected = (ROOT / 'bridge/Watchdog.gs').read_text()
    def patch(directory, old, new):
        changed = original_patch(directory, old, new)
        added = stage_watchdog(directory, expected)
        return changed or added
    deploy.patch_launch = patch
    # Existing deployment readback/rollback checks preserve all unrelated files,
    # daily/monthly trigger IDs, MYT timezone, and monthly state.
    deploy.main(launch_guard=True)
    bridge = module('watchdog_bridge_post', 'apps-script-post.py')
    url = deploy.secret('APPS_SCRIPT_WEBAPP_URL').strip()
    key = deploy.secret('APPS_SCRIPT_SHARED_KEY').strip()
    def post(op):
        result = bridge.post_json(url, {'op':op, 'key':key}, attempts=1)
        if not result.get('ok'):
            raise RuntimeError('WATCHDOG_ADMIN_FAILED:' + op)
        return result['watchdog']
    state = post('watchdog_status')
    if state.get('count') not in (0, 1):
        raise RuntimeError('WATCHDOG_TRIGGER_DRIFT')
    if not state['count']:
        # If this reply is lost, rerun reads status first rather than duplicating.
        post('install_daily_watchdog')
    state = post('watchdog_status')
    if (state.get('count') != 1 or state.get('timezoneBasis') != 'Asia/Kuala_Lumpur'
            or state.get('USAfterHour') != 7 or state.get('HKAfterHour') != 19
            or state.get('maxExtraLaunchesPerDay') != 2):
        raise RuntimeError('WATCHDOG_READBACK_FAILED')
    print('DAILY_WATCHDOG_DEPLOYED_AND_TRIGGER_VERIFIED', flush=True)
    print('MYT US=06:00-07:00 HK=18:00-19:00; hourly catch-up after each window; max 2/day/market', flush=True)
    print('WATCHDOG_BUSINESS_RESULT_PENDING_NEXT_AUTOMATIC_CHECK', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))

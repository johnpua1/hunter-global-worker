"""Verify the failed HK patch through range recovery before resuming Phase 2."""
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'hk_phase2_range_recovery', ROOT / 'cloudrun/resume-hk-phase2-20261007.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
repair.OLD = 'hunter-hk-daily-vg56d'
repair.PREVIOUS = '688a57ce9ef4ff8b8dc80c95f01d4832bea7cf3a'
PATCH = 'HK/REPAIR_PATCH/8d/8ddd17fd5aaacc231fc03034.json'
PATCH_SHA = '586185a74e39431c0cf597c9c68f172bf3fc11a4312c15702605dc698ffa404a'
daily_preflight = repair.preflight


def verify_patch(doc):
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive, digest

    class RangeReader(Drive):
        def _call(self, op, **fields):
            if op == 'read':
                # Exercise the recovery path against the real range endpoint,
                # even if the primary transport happens to have recovered.
                raise ValueError('PREFLIGHT_PRIMARY_READ_FAILURE_INJECTED')
            if op not in {'file', 'read_chunk'}:
                raise RuntimeError('PREFLIGHT_READ_ONLY')
            return super()._call(op, **fields)

    saved = dict(os.environ)
    try:
        values = {v['name']: v for v in repair.container(doc).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = repair.deploy.preflight_env_value(doc, values.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
        reader = RangeReader()
        try:
            raw = reader.read(PATCH)
        finally:
            reader.http.close()
        if digest(raw) != PATCH_SHA:
            raise RuntimeError('HK_FAILED_PATCH_CONTENT_CHANGED_NO_START')
        parsed = json.loads(raw)
        if parsed.get('market') != 'HK' or parsed.get('security_id') != 'HK-000632':
            raise RuntimeError('HK_FAILED_PATCH_IDENTITY_MISMATCH_NO_START')
        print('HK_FAILED_PATCH_RANGE_RECOVERY_VERIFIED;SHA256_MATCH;READ_ONLY', flush=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def preflight(doc):
    if not daily_preflight(doc):
        return False
    verify_patch(doc)
    return True


repair.preflight = preflight

if __name__ == '__main__':
    repair.main()

"""Verify independent recovery of an expired response, then resume HK only."""
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'hk_response_repair_base', ROOT / 'cloudrun/resume-hk-phase2-read-fix-20261007.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
repair = base.repair
repair.OLD = 'hunter-hk-daily-64nqq'
repair.PREVIOUS = '0cac01c9669300ae9af612954a1f994cae32cef3'


def verify_response_recovery(doc):
    sys.path.insert(0, str(ROOT / 'hunter-global'))
    from runner import Drive, digest
    import requests

    class ExpiredResponseSession:
        # Simulate only the unusable response. The independent fallback must
        # obtain and validate real bytes through the existing production Bridge.
        def post(self, url, **kwargs):
            response = requests.Response()
            response.status_code = 302
            response.headers['Location'] = 'https://script.googleusercontent.com/expired-probe'
            response._content = b''
            return response

        def get(self, url, **kwargs):
            response = requests.Response()
            response.status_code = 404
            response.url = url
            response._content = b''
            return response

        def close(self):
            pass

    class Reader(Drive):
        def _call(self, op, **fields):
            if op == 'read':
                raise ValueError('PREFLIGHT_PRIMARY_READ_FAILURE_INJECTED')
            if op == 'file':
                return super()._call(op, **fields)
            if op != 'read_chunk':
                raise RuntimeError('PREFLIGHT_READ_ONLY')
            saved_http = self.http
            self.http = ExpiredResponseSession()
            try:
                fields['_attempts'] = 1
                return super()._call(op, **fields)
            finally:
                self.http.close()
                self.http = saved_http

    saved = dict(os.environ)
    try:
        values = {v['name']: v for v in repair.container(doc).get('env', [])}
        for key in ('APPS_SCRIPT_WEBAPP_URL', 'APPS_SCRIPT_SHARED_KEY'):
            os.environ[key] = repair.deploy.preflight_env_value(doc, values.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS='90', HUNTER_BRIDGE_ATTEMPTS='2')
        reader = Reader()
        try:
            raw = reader.read(base.PATCH)
        finally:
            reader.http.close()
        if digest(raw) != base.PATCH_SHA:
            raise RuntimeError('HK_RESPONSE_PROBE_CONTENT_CHANGED_NO_START')
        parsed = json.loads(raw)
        if parsed.get('market') != 'HK' or parsed.get('security_id') != 'HK-000632':
            raise RuntimeError('HK_RESPONSE_PROBE_IDENTITY_MISMATCH_NO_START')
        print('HK_RESPONSE_404_RECOVERY_VERIFIED;REAL_BRIDGE_BYTES;SHA256_MATCH;READ_ONLY', flush=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)


base.verify_patch = verify_response_recovery

if __name__ == '__main__':
    repair.main()

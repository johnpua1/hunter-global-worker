"""Run the existing production deployment guard cases against this wrapper."""
import importlib.util
import pathlib
import base64
import contextlib
import io
import unittest
import sys
from unittest.mock import Mock, patch
import test_hk_phase2_range_deploy as guards
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'hunter-global'))
import runner

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/resume-hk-phase2-response-fix-20261007.py'
spec = importlib.util.spec_from_file_location('hk_response_deploy_tests', path)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class HKResponseDeploymentTests(guards.HKPhase2RangeDeploymentTests):
    def setUp(self):
        # Existing helpers bind their defaults when imported; explicitly use
        # this recovery's known failed execution and deployed predecessor.
        old_execution, old_job = guards.execution, guards.job
        changes = {
            'mod': wrapper.repair,
            'wrapper': wrapper.base,
            'execution': lambda name=wrapper.repair.OLD, state='False': old_execution(name, state),
            'job': lambda sha=wrapper.repair.PREVIOUS: old_job(sha),
        }
        for key, value in changes.items():
            context = patch.object(guards, key, value)
            context.start()
            self.addCleanup(context.stop)


class ResponsePreflightTests(unittest.TestCase):
    def test_probe_injects_404_and_verifies_independent_response_bytes(self):
        raw = b'{"market":"HK","security_id":"HK-000632"}'
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'ok': True, 'file': {'size': len(raw)}}
        payload = {'ok': True, 'offset': 0, 'size': len(raw), 'length': len(raw),
                   'data_base64': base64.b64encode(raw).decode(),
                   'sha256': runner.digest(raw), 'eof': True}
        def binding(doc, value, key):
            return 'https://script.google.com/macros/s/test/exec' if key.endswith('URL') else 'key'
        output = io.StringIO()
        with patch.object(runner.requests, 'Session', return_value=session), \
             patch.object(runner, 'bridge_read_response', return_value=payload) as fallback, \
             patch.object(wrapper.repair.deploy, 'preflight_env_value', side_effect=binding), \
             patch.object(wrapper.base, 'PATCH_SHA', runner.digest(raw)), \
             contextlib.redirect_stdout(output):
            wrapper.verify_response_recovery(guards.job(wrapper.repair.PREVIOUS))
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[1], {
            'op': 'read_chunk', 'key': 'key', 'path': wrapper.base.PATCH,
            'offset': 0, 'length': len(raw)})
        self.assertIn('HK_RESPONSE_404_RECOVERY_VERIFIED', output.getvalue())
        session.close.assert_called_once()

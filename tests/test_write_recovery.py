import ast
import contextlib
import copy
import io
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import requests
from runner import Drive, digest
from write_recovery import transient
from test_hk_read_cache_recovery_deploy import document
from test_us_phase2_deploy import execution

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREVIOUS = '702bc750ed544efe7eea94ff2a96f343865b30e5'
NEW = 'a' * 40


def response(status, url='https://script.googleusercontent.com/macros/echo'):
    r = requests.Response(); r.status_code = status; r.url = url
    r._content = b'{}'
    return r


def error404(url='https://script.googleusercontent.com/macros/echo'):
    try: response(404, url).raise_for_status()
    except requests.HTTPError as e: return e


class WriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        mode = patch.dict(os.environ, {'HUNTER_CONTINUE_ONLY': '1'})
        mode.start(); self.addCleanup(mode.stop)
        self.d = Drive.__new__(Drive)
        self.d._continuation_state = {'guard': threading.Lock(), 'locks': {}, 'memory': {}}
        self.d.http = Mock()
        self.d.url = 'https://script.google.com/macros/s/test/exec'; self.d.key = 'test'
        for name in ('read', 'file'):
            p = patch.object(self.d, name, side_effect=AssertionError('client readback forbidden'))
            p.start(); self.addCleanup(p.stop)
        for name in ('runner.time.sleep', 'write_recovery.time.sleep'):
            p=patch(name); p.start(); self.addCleanup(p.stop)

    def test_lost_immutable_response_reuses_identical_request_without_readback(self):
        for failure in (requests.ReadTimeout(), error404()):
            with self.subTest(failure=type(failure).__name__):
                self.d._continuation_state.pop('write_uncertain', None)
                with patch.object(self.d, '_call', side_effect=[failure, {'sha256': digest(b'x'), 'file': {'id':'saved'}}]) as call, patch('runner.requests.Session'):
                    self.assertEqual(self.d.put('HK/CONTROL/PHASE2_RESUME/batch.json', b'x', immutable=True), {'id':'saved'})
                    self.assertEqual(call.call_count, 2)
                    self.assertEqual(call.call_args_list[0], call.call_args_list[1])
                    self.assertNotIn('write_uncertain', self.d._continuation_state)

    def test_successful_immutable_write_is_not_repeated(self):
        with patch.object(self.d, '_call', return_value={'sha256':digest(b'x'),'file':{}}) as call:
            self.d.put('HK/test',b'x',immutable=True)
            call.assert_called_once()

    def test_append_mutable_and_cas_writes_never_repost_after_timeout(self):
        for method, kw in [('append', {}), ('put', {}), ('put', {'immutable':True,'expected_sha':'old'})]:
            with self.subTest(method=method,kw=kw):
                self.d._continuation_state.pop('write_uncertain',None)
                with patch.object(self.d,'_call',side_effect=requests.ReadTimeout()) as call:
                    with self.assertRaises(requests.ReadTimeout): getattr(self.d,method)('HK/test',b'x',**kw)
                    call.assert_called_once()
                    self.assertTrue(self.d._continuation_state['write_uncertain'])

    def test_integrity_auth_canonical404_and_unknown_failures_are_not_retried(self):
        for failure in (RuntimeError('BRIDGE_IMMUTABLE_CONFLICT'), RuntimeError('BRIDGE_STALE_WRITE'),
                        ValueError('BRIDGE_UNEXPECTED_REDIRECT:put'), error404('https://script.google.com/macros/s/test/exec'),
                        error404('https://untrusted.example/missing')):
            self.d._continuation_state.pop('write_uncertain',None)
            with patch.object(self.d,'_call',side_effect=failure) as call:
                with self.assertRaises(type(failure)): self.d.put('HK/test',b'x',immutable=True)
                call.assert_called_once()

    def test_three_transport_failures_stop_with_typed_reason_and_block_cleanup(self):
        with patch.object(self.d,'_call',side_effect=requests.ReadTimeout()) as call, patch('runner.requests.Session'):
            with self.assertRaisesRegex(RuntimeError,'IMMUTABLE_WRITE_TRANSPORT_RETRY_REQUIRED'):
                self.d.put('HK/test',b'x',immutable=True)
            self.assertEqual(call.call_count,3)
            with self.assertRaisesRegex(RuntimeError,'CONTINUATION_WRITE_OUTCOME_UNKNOWN'):
                self.d.put('HK/other',b'y',immutable=True)
            self.assertEqual(call.call_count,3)

    def test_bad_ack_sha_is_never_accepted_or_retried(self):
        with patch.object(self.d,'_call',return_value={'sha256':'wrong','file':{}}) as call:
            with self.assertRaisesRegex(RuntimeError,'BRIDGE_WRITE_SHA_MISMATCH'):
                self.d.put('HK/test',b'x',immutable=True)
            call.assert_called_once()

    def test_mutable_write_response404_retries_get_without_reposting(self):
        redirect=Mock(status_code=302,headers={'Location':'https://script.googleusercontent.com/macros/echo'})
        ack=Mock(status_code=200)
        ack.json.return_value={'ok':True,'file':{'id':'done'},'sha256':digest(b'x')}
        self.d.http.post.return_value=redirect
        self.d.http.get.side_effect=[response(404),ack]
        self.assertEqual(self.d.put('US/test',b'x'),{'id':'done'})
        self.d.http.post.assert_called_once()
        self.assertEqual(self.d.http.get.call_count,2)
        for call in self.d.http.get.call_args_list:
            self.assertNotIn('json',call.kwargs)
            self.assertFalse(call.kwargs['allow_redirects'])

    def test_unrecoverable_mutable_response404_still_never_reposts(self):
        self.d.http.post.return_value=Mock(status_code=302,headers={'Location':'https://script.googleusercontent.com/macros/echo'})
        self.d.http.get.side_effect=[response(404),response(404),response(404)]
        with self.assertRaises(requests.HTTPError): self.d.put('US/test',b'x')
        self.d.http.post.assert_called_once()
        self.assertEqual(self.d.http.get.call_count,3)

    def test_no_private_response_url_or_key_in_recovery_log(self):
        with patch.object(self.d,'_call',side_effect=[error404('https://script.googleusercontent.com/SECRET'),{'sha256':digest(b'x'),'file':{}}]), patch('runner.requests.Session'), self.assertLogs('hunter') as log:
            self.d.put('US/test',b'x',immutable=True)
        self.assertNotIn('SECRET','\n'.join(log.output))


class RecoveryControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        source=(ROOT/'cloudrun/phase2-write-recovery-20261008.sh').read_text().split("<<'PHASE2_PY'",1)[1].split('\n',1)[1].rsplit('\nPHASE2_PY',1)[0]
        tree=ast.parse(source); tree.body.pop()
        self.m=types.ModuleType('write_recovery_controller')
        with patch.object(sys,'argv',['controller',str(ROOT),NEW]),patch.object(pathlib.Path,'home',return_value=pathlib.Path(self.temp.name)),patch('fcntl.flock'):
            exec(compile(tree,'write-recovery','exec'),self.m.__dict__)
        self.addCleanup(self.m.LOCK.close)
        self.doc=self.m.pack_deployment.desired(document('HK',PREVIOUS),self.m.r,'HK',PREVIOUS)
        self.trace='''Traceback (most recent call last):
  File "/app/derived_resume.py", line 78, in save
    self.drive.put(self.path(batch, source), payload, immutable=True)
  File "/app/runner.py", line 524, in _continuation_write
    result = self._call(op, _attempts=1, **fields)
requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='script.google.com', port=443): Read timed out. (read timeout=120.0)'''

    def evaluate(self, trace=None, source=PREVIOUS):
        m=self.m
        doc=m.pack_deployment.desired(document('HK',source),m.r,'HK',source)
        def gc(*args):
            if args[:4]==('run','jobs','executions','describe'):return doc
            if args[:2]==('logging','read'):return [{'textPayload':self.trace if trace is None else trace}]
            raise AssertionError(args)
        with patch.object(m.r,'gc',side_effect=gc),patch.object(m,'emit'):
            return m.immutable_batch_failure('hunter-hk-daily-7m95r','HK')

    def test_observed_immutable_batch_timeout_allowed(self): self.assertTrue(self.evaluate())

    def test_observed_immutable_batch_response404_allowed(self):
        trace=self.trace[:self.trace.index('requests.exceptions.ReadTimeout:')]+'requests.exceptions.HTTPError: 404 Client Error: Not Found for url: https://script.googleusercontent.com/macros/echo?SECRET'
        self.assertTrue(self.evaluate(trace))

    def test_unseen_mutable_write_or_bad_source_is_not_whitelisted(self):
        self.assertFalse(self.evaluate(self.trace.replace('derived_resume.py','continuation.py')))
        self.assertFalse(self.evaluate(self.trace.replace('immutable=True','immutable=False')))
        self.assertFalse(self.evaluate(source=NEW))
        with self.assertRaisesRegex(RuntimeError,'SOURCE_MISMATCH'):self.evaluate(source='b'*40)

    def test_conflict_and_canonical404_cannot_be_handed_off(self):
        for ending in ('RuntimeError: BRIDGE_IMMUTABLE_CONFLICT','requests.exceptions.HTTPError: 404 Client Error: Not Found for url: https://script.google.com/macros/s/test/exec'):
            self.assertFalse(self.evaluate(self.trace[:self.trace.index('requests.exceptions.ReadTimeout:')]+ending))

    def test_both_markets_new_image_and_only702_as_upgrade_source(self):
        self.assertEqual(self.m.EXPECTED_SHA,{'US':NEW,'HK':NEW})
        self.assertEqual(self.m.pack_deployment.EXISTING,{'US':PREVIOUS,'HK':PREVIOUS})

    def test_preflight_rejects_unknown_failure_before_build(self):
        m=self.m
        with patch.object(m.r,'executions',return_value=[execution('hunter-us-daily-test')]),patch.object(m.r,'gc',return_value={}),patch.object(m,'recoverable_failure',return_value=None),patch.object(m.pack_deployment,'deploy_both') as deploy:
            with self.assertRaisesRegex(RuntimeError,'PREFLIGHT_REJECTED:US'):m.main()
        deploy.assert_not_called()


if __name__=='__main__':unittest.main()

# Exercise the actual new deployment helper with the existing deployment
# failure/race scenarios, rather than assuming the copied helper is equivalent.
import importlib.util
import test_continuation_deploy as deploy_tests

class WriteRecoveryDeploymentTests(deploy_tests.ContinuationDeployTests):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('write_recovery_deploy_test',ROOT/'cloudrun/deploy-write-recovery-20261008.py')
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        replacement=patch.object(deploy_tests,'mod',mod)
        replacement.start(); self.addCleanup(replacement.stop)
        super().setUp()

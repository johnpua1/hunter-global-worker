"""Observed old US queue failure cannot veto a completed daily-stage handoff."""
import ast
import contextlib
import copy
import io
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock
from test_hk_read_cache_recovery_deploy import document
from test_us_phase2_deploy import execution

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = '702bc750ed544efe7eea94ff2a96f343865b30e5'
OLD_NAME = 'hunter-us-daily-59mqq'
FATAL = 'BRIDGE_READ_CHUNK_POSITION_MISMATCH:REPAIR_QUEUE.json'


class ContinuationHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        source = (ROOT / 'cloudrun/phase2-continuation-resume-20261008.sh').read_text().split("<<'PHASE2_PY'",1)[1].split('\n',1)[1].rsplit('\nPHASE2_PY',1)[0]
        tree = ast.parse(source); tree.body.pop()
        self.m = types.ModuleType('continuation_handoff')
        with patch.object(sys,'argv',['controller',str(ROOT),'c'*40]), patch.object(pathlib.Path,'home',return_value=pathlib.Path(self.temp.name)), patch('fcntl.flock'):
            exec(compile(tree,'handoff','exec'),self.m.__dict__)
        self.addCleanup(self.m.LOCK.close)
        self.doc = self.m.pack_deployment.desired(document('US',WORKER), self.m.r, 'US', WORKER)

    def evaluate(self, *, name=OLD_NAME, market='US', fatal=FATAL, saved=True, old_source=None, doc=None):
        m=self.m
        old = document('US',old_source or m.pack_deployment.EXISTING['US'])
        def gc(*args):
            if args[:2] == ('logging','read'): return [{'textPayload':'ERROR hunter halted: '+fatal}]
            if args[:4] == ('run','jobs','executions','describe'): return old
            raise AssertionError('Unexpected operation '+repr(args))
        with patch.object(m.r,'gc',side_effect=gc), patch.object(m.r,'timeout_failure',return_value=False), patch.object(m,'daily_already_saved',return_value=saved) as checked:
            result = m.recoverable_failure(name,market,self.doc if doc is None else doc)
            return result,checked.call_count

    def test_exact_legacy_failure_and_completed_record_allows_new_stage(self):
        self.assertEqual(self.evaluate()[0],'OLD_COMPLETED_DAILY_STAGE_SKIPPED_BY_NEW_WORKER')
        self.assertEqual(self.m.EXPECTED_SHA,{'US':WORKER,'HK':WORKER})

    def test_without_completed_record_no_integrity_failure_is_ignored(self):
        self.assertIsNone(self.evaluate(saved=False)[0])

    def test_same_error_on_new_execution_or_other_path_is_not_recoverable(self):
        for changes in ({'name':'hunter-us-daily-new'}, {'fatal':'BRIDGE_READ_CHUNK_POSITION_MISMATCH:US/BASE/batch-0001.ndjson.gz'}, {'market':'HK'}, {'old_source':WORKER}):
            with self.subTest(changes=changes): self.assertIsNone(self.evaluate(**changes)[0])

    def test_wrong_worker_or_disabled_mode_cannot_get_handoff_permit(self):
        doc = copy.deepcopy(self.doc)
        c = self.m.r.deployment.container(doc,'US')
        for e in c['env']:
            if e['name']=='HUNTER_CONTINUE_ONLY': e['value']='0'
        self.assertIsNone(self.evaluate(doc=doc)[0])

    def test_consumed_permit_cannot_be_reused(self):
        self.m.OBSERVED_RECOVERY_USED.add(OLD_NAME)
        self.assertIsNone(self.evaluate()[0])

    def test_attach_is_read_only_and_keeps_both_worker_versions(self):
        m=self.m
        docs={market:m.pack_deployment.desired(document(market,WORKER),m.r,market,WORKER) for market in ('US','HK')}
        calls=[]
        def gc(*args):
            calls.append(args)
            self.assertEqual(args[:3],('run','jobs','describe'))
            return copy.deepcopy(docs[args[3].split('-')[1].upper()])
        with patch.object(m.r,'gc',side_effect=gc), patch.object(m.r.base,'build') as build, contextlib.redirect_stdout(io.StringIO()):
            result=m.pack_deployment.verify_current(m.r,WORKER)
        build.assert_not_called(); self.assertEqual(len(calls),4)
        self.assertEqual(result['cancelled'],set())

    def test_active_hk_is_preserved_and_us_waits_without_launch(self):
        m=self.m
        with patch.object(m.r,'executions',side_effect=lambda market: [execution('hunter-hk-daily-7m95r','Unknown')] if market=='HK' else [execution(OLD_NAME)]), patch.object(m.r,'gc') as gc, patch.object(m.r,'checkpoint') as cp, patch.object(m,'emit'):
            self.assertIsNone(m.step('US',self.doc,{}))
        gc.assert_not_called(); cp.assert_not_called()

    def test_attach_rejects_a_mixed_worker_version_without_mutation(self):
        m=self.m
        docs={'US':self.doc,'HK':document('HK',m.pack_deployment.EXISTING['HK'])}
        with patch.object(m.r,'gc',side_effect=lambda *args: copy.deepcopy(docs[args[3].split('-')[1].upper()])), patch.object(m.r.base,'build') as build:
            with self.assertRaisesRegex(RuntimeError,'NEW_TEMPLATE_REQUIRED:HK'):
                m.pack_deployment.verify_current(m.r,WORKER)
        build.assert_not_called()


if __name__ == '__main__': unittest.main()

import ast
import copy
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import test_continuation_deploy as base_tests
from test_hk_read_cache_recovery_deploy import document
from test_us_phase2_deploy import execution

ROOT=pathlib.Path(__file__).resolve().parents[1]
NEW='a'*40
PREVIOUS='b747df7deb0f9257c8e74f5c94c027bc1a63f032'

class AccelerationDeployTests(base_tests.ContinuationDeployTests):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('acceleration_deploy_test',ROOT/'cloudrun/deploy-restore-acceleration-20261008.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        p=patch.object(base_tests,'mod',mod);p.start();self.addCleanup(p.stop)
        self.accel=mod
        super().setUp()

    def test_both_templates_same_version_before_old_executions_cancel_no_launch(self):
        for market in self.rows:self.rows[market]=[execution('hunter-'+market.lower()+'-daily-active','Unknown')]
        result=self.deploy()
        self.assertEqual(result['cancelled'],set())
        self.assertFalse(any('cancel' in c or 'execute' in c for c in self.calls))
        self.assertEqual([c[3] for c in self.calls if c[:3]==('run','jobs','update')],['hunter-us-daily','hunter-hk-daily'])
        self.r.base.build.assert_called_once_with(NEW)

    def test_can_upgrade_both_b747_workers_without_rollback(self):
        self.docs={m:self.accel.desired(document(m,PREVIOUS),self.r,m,PREVIOUS) for m in ('US','HK')}
        self.deploy()
        self.assertTrue(all(c[4].endswith(NEW) for c in self.calls if 'update' in c))
        self.assertFalse(any('cancel' in c or 'execute' in c for c in self.calls))


class AccelerationControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.dict(os.environ);p.start();self.addCleanup(p.stop)
        source=(ROOT/'cloudrun/phase2-restore-acceleration-20261008.sh').read_text().split("<<'PHASE2_PY'",1)[1].split('\n',1)[1].rsplit('\nPHASE2_PY',1)[0]
        tree=ast.parse(source);tree.body.pop()
        self.m=types.ModuleType('acceleration_controller_test')
        with patch.object(sys,'argv',['controller',str(ROOT),NEW]),patch.object(pathlib.Path,'home',return_value=pathlib.Path(self.temp.name)),patch('fcntl.flock'):
            exec(compile(tree,'accelerator','exec'),self.m.__dict__)
        self.addCleanup(self.m.LOCK.close)

    def test_preflight_preserves_active_known_worker_without_mutations(self):
        m=self.m
        with patch.object(m.r,'executions',return_value=[execution('hunter-us-daily-running','Unknown')]),patch.object(m,'execution_source',return_value=PREVIOUS),patch.object(m.r,'gc') as gc,patch.object(m,'emit'):
            m.preflight_write_recovery()
        gc.assert_not_called()

    def test_old_typed_immutable_transport_failure_can_resume_on_new_worker(self):
        m=self.m
        with patch.object(m.r,'gc',return_value=[{'textPayload':'ERROR hunter halted: IMMUTABLE_WRITE_TRANSPORT_RETRY_REQUIRED'}]),patch.object(m,'execution_source',return_value=PREVIOUS):
            self.assertEqual(m.recoverable_failure('hunter-us-daily-known','US'),'IMMUTABLE_WRITE_RESPONSE_RESUME')

    def test_cache_rebuild_forbidden_is_not_reinterpreted_as_success_or_retry(self):
        m=self.m
        with patch.object(m.r,'gc',return_value=[{'textPayload':'ERROR hunter halted: INPUT_CACHE_REBUILD_FORBIDDEN:US/CONTROL/INCREMENTAL_CACHE/pack-00000-0.zip'}]):
            self.assertIsNone(m.recoverable_failure('hunter-us-daily-known','US'))

    def test_explicit_cancelled_known_worker_can_resume_without_fabricating_completion(self):
        m=self.m
        def gc(*args):
            if args[:2]==('logging','read'):return []
            return {'status':{'completionTime':'2026-10-08T15:32:00Z','conditions':[{'reason':'Cancelled'}]}}
        with patch.object(m.r,'gc',side_effect=gc),patch.object(m,'execution_source',return_value=PREVIOUS):
            self.assertEqual(m.recoverable_failure('hunter-us-daily-known','US'),'USER_STOPPED_CONTINUATION_WORKER_RESUME')

if __name__=='__main__':unittest.main()

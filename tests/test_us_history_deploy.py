import contextlib
import copy
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest.mock import patch
from test_us_phase2_deploy import job, execution, SHA

path=pathlib.Path(__file__).resolve().parents[1]/'cloudrun/resume-us-phase2-history-fix-20261007.py'
spec=importlib.util.spec_from_file_location('us_history_recovery',path)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


class HistoryDeploymentTests(unittest.TestCase):
    def run_case(self, *, build_failure=False, inactive=8, completed=False,
                 latest=None, other=False, wrong_image=False, corrupt_readback=False):
        calls=[];deployed=False;launched=False;cancelled=False
        own='hunter-us-daily-repaired'
        def gc(*args):
            nonlocal deployed,launched,cancelled
            calls.append(args)
            if args[:4]==('run','jobs','executions','list'):
                rows=[execution(own if launched else (latest or mod.OLD),
                                'Unknown' if launched or not cancelled else 'False')]
                if other:rows.append(execution('hunter-us-daily-other','Unknown'))
                return rows
            if args[:4]==('run','jobs','executions','describe'):
                self.assertEqual(args[4],mod.OLD)
                result=execution(mod.OLD)
                result['spec']={'template':{'spec':{'containers':[{
                    'image':mod.base.BASE_IMAGE+('b'*40 if wrong_image else mod.PREVIOUS)}]}}}
                return result
            self.assertEqual(args[3],mod.JOB)
            if args[2]=='describe':
                result=job(SHA if deployed else mod.PREVIOUS)
                if deployed and corrupt_readback:
                    result['spec']['template']['spec']['template']['spec']['timeoutSeconds']=9000
                return result
            if args[2]=='update':deployed=True;return job(SHA)
            if args[2]=='execute':launched=True;return execution(own,'Unknown')
            self.fail('unexpected action '+str(args))
        def cancel():
            nonlocal cancelled
            self.assertTrue(deployed)
            self.assertEqual(mod.recovery.OLD,mod.OLD)
            cancelled=True;calls.append(('cancel_confirmed',mod.OLD))
        def preflight(doc):return not (completed and deployed)
        error=None
        with patch.object(mod.recovery,'gc',side_effect=gc), \
             patch.object(mod.base,'preflight',side_effect=preflight), \
             patch.object(mod,'verify_history',return_value=inactive), \
             patch.object(mod.base,'build',side_effect=RuntimeError('BUILD_FAILED') if build_failure else None), \
             patch.object(mod.recovery,'cancel_old',side_effect=cancel), \
             patch.object(mod.subprocess,'check_output',return_value=SHA), \
             patch.object(sys,'argv',['script',SHA]),contextlib.redirect_stdout(io.StringIO()):
            try:mod.main()
            except RuntimeError as exc:error=str(exc)
        return calls,error,cancelled,launched

    def test_verified_deploy_then_only_known_us_cancel_and_phase2_launch(self):
        calls,error,cancelled,launched=self.run_case()
        self.assertIsNone(error);self.assertTrue(cancelled);self.assertTrue(launched)
        launch=[c for c in calls if c[:3]==('run','jobs','execute')]
        self.assertEqual(len(launch),1)
        self.assertIn('--args=--mode,auto,--market,US,--phase2-only,--as-of,2026-10-06',launch[0])
        update=next(c for c in calls if c[:3]==('run','jobs','update'))
        self.assertFalse(any(c.startswith(('--cpu','--memory','--task-timeout','--max-retries','--args')) for c in update))

    def test_build_failure_preserves_active_execution(self):
        calls,error,cancelled,launched=self.run_case(build_failure=True)
        self.assertEqual(error,'BUILD_FAILED');self.assertFalse(cancelled or launched)
        self.assertFalse(any(c[:3]==('run','jobs','update') for c in calls))

    def test_no_blocker_preserves_active_execution(self):
        calls,error,cancelled,launched=self.run_case(inactive=0)
        self.assertIsNone(error);self.assertFalse(cancelled or launched)
        self.assertFalse(any(c[:3]==('run','jobs','update') for c in calls))

    def test_receipt_committed_during_build_prevents_replacement(self):
        _,error,cancelled,launched=self.run_case(completed=True)
        self.assertIsNone(error);self.assertFalse(cancelled or launched)

    def test_repeat_or_concurrent_execution_prevents_all_mutations(self):
        for kwargs in ({'latest':'hunter-us-daily-repaired'},{'other':True}):
            calls,error,cancelled,launched=self.run_case(**kwargs)
            self.assertIsNone(error);self.assertFalse(cancelled or launched)
            self.assertEqual(len(calls),1)

    def test_wrong_old_image_cannot_be_cancelled(self):
        _,error,cancelled,launched=self.run_case(wrong_image=True)
        self.assertIn('IMAGE_MISMATCH',error);self.assertFalse(cancelled or launched)

    def test_failed_config_readback_cannot_cancel_or_launch(self):
        _,error,cancelled,launched=self.run_case(corrupt_readback=True)
        self.assertIn('READBACK_MISMATCH',error);self.assertFalse(cancelled or launched)

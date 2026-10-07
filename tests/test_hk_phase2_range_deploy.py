import contextlib
import copy
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/resume-hk-phase2-read-fix-20261007.py'
spec = importlib.util.spec_from_file_location('hk_phase2_recovery', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
wrapper = mod
mod = wrapper.repair
SHA = 'a' * 40


def execution(name=mod.OLD, state='False'):
    return {'metadata':{'name':name},'status':{'conditions':[{'type':'Completed','status':state}]}}


def job(sha=mod.PREVIOUS):
    return {'spec':{'template':{'spec':{'taskCount':1,'parallelism':1,
        'template':{'spec':{'timeoutSeconds':7200,'maxRetries':0,
            'serviceAccountName':'existing-hk', 'containers':[{
                'image':mod.BASE_IMAGE+sha,'args':mod.ARGS[:],
                'resources':{'limits':{'cpu':'1','memory':'2Gi'}},
                'env':[{'name':'DERIVED_READ_WORKERS','value':'2'},
                       {'name':'HUNTER_SOURCE_SHA','value':sha}]}]}}}}}}


class HKPhase2RangeDeploymentTests(unittest.TestCase):
    def test_repeat_or_any_active_hk_does_not_build_or_write(self):
        for rows in ([execution('hunter-hk-daily-new')],
                     [execution(),execution('hunter-hk-daily-other','Unknown')]):
            with patch.object(mod.recovery,'gc',return_value=rows) as gc, \
                 patch.object(mod,'build') as build, patch.object(mod,'preflight') as preflight, \
                 patch.object(mod.subprocess,'check_output',return_value=SHA), \
                 patch.object(sys,'argv',['script',SHA]), contextlib.redirect_stdout(io.StringIO()):
                mod.main()
                build.assert_not_called()
                preflight.assert_not_called()
                self.assertEqual(gc.call_count,1)

    def test_failed_real_range_probe_stops_before_deployment(self):
        def gc(*args):
            if args[:4] == ('run','jobs','executions','list'): return [execution()]
            if args[:3] == ('run','jobs','describe'): return job()
            self.fail('mutation before read probe passed')
        with patch.object(mod.recovery,'gc',side_effect=gc), \
             patch.object(wrapper,'daily_preflight',return_value=True), \
             patch.object(wrapper,'verify_patch',side_effect=RuntimeError('range failed')), \
             patch.object(mod,'build') as build, \
             patch.object(mod.subprocess,'check_output',return_value=SHA), \
             patch.object(sys,'argv',['script',SHA]):
            with self.assertRaisesRegex(RuntimeError,'range failed'): mod.main()
            build.assert_not_called()

    def test_failed_build_leaves_all_jobs_untouched(self):
        def gc(*args):
            if args[:4] == ('run','jobs','executions','list'): return [execution()]
            if args[:3] == ('run','jobs','describe'): return job()
            self.fail('unexpected mutation '+str(args[:3]))
        with patch.object(mod.recovery,'gc',side_effect=gc), \
             patch.object(mod,'preflight',return_value=True), \
             patch.object(mod,'build',side_effect=RuntimeError('build failed')), \
             patch.object(mod.subprocess,'check_output',return_value=SHA), \
             patch.object(sys,'argv',['script',SHA]):
            with self.assertRaisesRegex(RuntimeError,'build failed'): mod.main()

    def test_only_hk_image_update_and_single_phase2_override_launch(self):
        calls=[]; deployed=False; launched=False
        own='hunter-hk-daily-phase2'
        def gc(*args):
            nonlocal deployed,launched
            calls.append(args)
            if args[:4] == ('run','jobs','executions','list'):
                self.assertIn('--job='+mod.JOB,args)
                return [execution(own,'Unknown')] if launched else [execution()]
            self.assertEqual(args[3],mod.JOB)
            if args[2]=='describe': return job(SHA if deployed else mod.PREVIOUS)
            if args[2]=='update': deployed=True; return job(SHA)
            if args[2]=='execute': launched=True; return execution(own,'Unknown')
            self.fail('unexpected mutation '+str(args))
        with patch.object(mod.recovery,'gc',side_effect=gc), \
             patch.object(mod,'preflight',return_value=True), patch.object(mod,'build'), \
             patch.object(mod.subprocess,'check_output',return_value=SHA), \
             patch.object(sys,'argv',['script',SHA]), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual([c[2] for c in calls if c[2] in ('update','execute')],['update','execute'])
        launch=next(c for c in calls if c[2]=='execute')
        self.assertIn('--args=--mode,auto,--market,HK,--phase2-only,--as-of,2026-10-06',launch)
        update=next(c for c in calls if c[2]=='update')
        self.assertFalse(any(x.startswith(('--args','--cpu','--memory','--task-timeout','--max-retries')) for x in update))

    def test_readback_rejects_resource_budget_or_entry_drift(self):
        before=job(); after=job(SHA)
        mod.verify_update(before,after,SHA)
        for change in ('timeout','args','env'):
            bad=copy.deepcopy(after)
            task=bad['spec']['template']['spec']['template']['spec']
            if change=='timeout': task['timeoutSeconds']=9000
            elif change=='args': task['containers'][0]['args'] += ['--phase2-only']
            else: task['containers'][0]['env'].append({'name':'UNEXPECTED','value':'1'})
            with self.assertRaises(RuntimeError): mod.verify_update(before,bad,SHA)


if __name__=='__main__':
    unittest.main()

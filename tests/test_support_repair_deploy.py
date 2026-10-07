import contextlib
import copy
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from test_daily_runtime_budget import deploy, job, BASE, SHA

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('monthly_patch', ROOT/'cloudrun/repair-monthly-trigger-20261007.py')
monthly_patch=importlib.util.module_from_spec(spec);spec.loader.exec_module(monthly_patch)

class SupportRepairDeployTests(unittest.TestCase):
    def test_only_support_jobs_updated_without_capacity_change_or_launch(self):
        docs={}
        for name,args in [('hunter-maintenance',['/app/maintenance.py']),('hunter-monthly-v2',['--mode','monthly'])]:
            doc=job();task=doc['spec']['template']['spec']['template']['spec']
            task['containers'][0]['args']=args
            docs[name]=doc
        calls=[]
        def gc(*args):
            calls.append(args)
            name=args[3];self.assertIn(name,docs)
            if args[:3]==('run','jobs','describe'):return copy.deepcopy(docs[name])
            self.assertEqual(args[:3],('run','jobs','update'))
            flags=dict(x[2:].split('=',1) for x in args[4:])
            self.assertEqual(set(flags),{'image','update-env-vars'})
            self.assertEqual(flags['update-env-vars'],'HUNTER_SOURCE_SHA='+SHA)
            container=docs[name]['spec']['template']['spec']['template']['spec']['containers'][0]
            container['image']=flags['image'];container['env'].append({'name':'HUNTER_SOURCE_SHA','value':SHA})
            return {}
        with tempfile.TemporaryDirectory() as temp, patch.object(deploy.sys,'argv',['deploy',SHA,'--support-only','--no-start']), patch.object(deploy.subprocess,'check_output',return_value=SHA), patch.object(deploy.subprocess,'run') as build, patch.object(deploy.tempfile,'mkdtemp',return_value=temp), patch.object(deploy.os,'umask'), patch.object(deploy.recovery,'gc',side_effect=gc), patch.object(deploy.recovery,'executions') as executions, patch('runner.Drive') as drive, contextlib.redirect_stdout(io.StringIO()):
            drive.return_value.json.side_effect=lambda path:{'market':path.split('/')[0],'securities':[{}]}
            deploy.main()
            executions.assert_not_called();build.assert_called_once()
        self.assertEqual(len([c for c in calls if c[:3]==('run','jobs','update')]),2)

    def test_support_cannot_launch_or_mix_with_daily(self):
        for flags in (['--support-only'],['--support-only','--no-start','--daily-only']):
            with patch.object(deploy.sys,'argv',['deploy',SHA]+flags),patch.object(deploy.subprocess,'check_output',return_value=SHA),patch.object(deploy.recovery,'gc') as gc,patch.object(deploy.os,'umask'):
                with self.assertRaisesRegex(RuntimeError,'SUPPORT_REPAIR_REQUIRES'):
                    deploy.main()
                gc.assert_not_called()

    def test_month_patch_preserves_remote_content_and_is_idempotent(self):
        candidate=(ROOT/'bridge/Gateway.gs').read_text()
        remote=candidate
        for name,following in monthly_patch.FUNCTIONS.items():
            a,b,_=monthly_patch.section(remote,name,following)
            baseline=(ROOT/'tests/fixtures'/(name+'-before-20261007.js')).read_text()
            remote=remote[:a]+baseline+remote[b:]
        remote += '\nfunction unrelatedRemoteOnly() {return 42;}\n'
        expected=candidate+'\nfunction unrelatedRemoteOnly() {return 42;}\n'
        self.assertEqual(monthly_patch.patch_source(remote,candidate),expected)
        self.assertEqual(monthly_patch.patch_source(expected,candidate),expected)
        with self.assertRaisesRegex(RuntimeError,'SOURCE_CHANGED'):
            monthly_patch.patch_source(remote.replace('var result = runMonthly();','var result = changedMonthly();'),candidate)

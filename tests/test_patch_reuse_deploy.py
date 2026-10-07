import contextlib
import copy
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest.mock import patch
from test_us_phase2_deploy import job, SHA

path=pathlib.Path(__file__).resolve().parents[1]/'cloudrun/deploy-patch-reuse-20261007.py'
spec=importlib.util.spec_from_file_location('patch_reuse_deploy',path)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


def config(market,sha):
    doc=job(sha)
    doc['spec']['template']['spec']['template']['spec']['containers'][0]['args'][-1]=market
    return doc


class PatchReuseDeployTests(unittest.TestCase):
    def run_case(self,fail_build=False,already=False,drift=False):
        calls=[];built=False;updated=set();error=None
        def build(sha):
            nonlocal built
            if fail_build:raise RuntimeError('BUILD_FAILED')
            built=True
        def gc(*args):
            calls.append(args)
            self.assertEqual(args[:2],('run','jobs'))
            self.assertIn(args[2],('describe','update'))
            self.assertIn(args[3],('hunter-us-daily','hunter-hk-daily'))
            market=args[3].split('-')[1].upper()
            if args[2]=='update':updated.add(market)
            doc=config(market,SHA if already or market in updated else mod.PREVIOUS[market])
            if drift and built:doc['spec']['template']['spec']['template']['spec']['timeoutSeconds']=9000
            return doc
        with patch.object(mod,'gc',side_effect=gc),patch.object(mod,'checkpoint'), \
             patch.object(mod.base,'build',side_effect=build) as b, \
             patch.object(mod.subprocess,'check_output',return_value=SHA), \
             patch.object(sys,'argv',['script',SHA]),contextlib.redirect_stdout(io.StringIO()):
            try:mod.main()
            except RuntimeError as exc:error=str(exc)
            if already:b.assert_not_called()
        return calls,error

    def test_only_existing_daily_images_change_without_launch_or_cancel(self):
        calls,error=self.run_case();self.assertIsNone(error)
        writes=[c for c in calls if c[2]=='update'];self.assertEqual(len(writes),2)
        for c in writes:
            self.assertEqual(c[4:],('--image='+mod.base.BASE_IMAGE+SHA,'--update-env-vars=HUNTER_SOURCE_SHA='+SHA))

    def test_failed_build_and_concurrent_configuration_change_prevent_updates(self):
        for kwargs in ({'fail_build':True},{'drift':True}):
            calls,error=self.run_case(**kwargs);self.assertIsNotNone(error)
            self.assertFalse(any(c[2]=='update' for c in calls))

    def test_repeat_only_verifies_without_build_or_update(self):
        calls,error=self.run_case(already=True);self.assertIsNone(error)
        self.assertFalse(any(c[2]=='update' for c in calls))

    def test_readback_rejects_budget_or_resource_change(self):
        before=config('HK',mod.PREVIOUS['HK']);after=config('HK',SHA)
        mod.verify(before,after,'HK',SHA)
        after['spec']['template']['spec']['template']['spec']['maxRetries']=1
        with self.assertRaisesRegex(RuntimeError,'READBACK_MISMATCH'):
            mod.verify(before,after,'HK',SHA)

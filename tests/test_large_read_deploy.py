import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest.mock import patch
import test_resumable_release as release

path=pathlib.Path(__file__).resolve().parents[1]/'cloudrun/deploy-large-read-fix-20261008.py'
spec=importlib.util.spec_from_file_location('large_read_deploy',path)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
SHA='c'*40

def config(market,new=False):
    doc=release.target(market)
    c=mod.deployment.container(doc,market)
    sha=SHA if new else mod.PREVIOUS
    c['image']=mod.base.BASE_IMAGE+sha
    for e in c['env']:
        if e['name']=='HUNTER_SOURCE_SHA':e['value']=sha
    if new:c['env'].append({'name':'HUNTER_READ_CHECKPOINTS','value':'1'})
    return doc

class LargeReadDeploymentTests(unittest.TestCase):
    def test_bridge_must_pass_before_build_or_job_update(self):
        with patch.object(mod,'gc',return_value=config('US')) as gc, \
             patch.object(mod,'preflight_bridge',side_effect=RuntimeError('CAPABILITY_MISSING')), \
             patch.object(mod.base,'build') as build:
            with self.assertRaisesRegex(RuntimeError,'CAPABILITY_MISSING'):mod.deploy(SHA)
            build.assert_not_called()
            self.assertFalse(any(c.args[2]=='update' for c in gc.call_args_list))

    def test_only_daily_images_and_flags_change_active_runs_untouched(self):
        calls=[];updated=set()
        def gc(*args):
            calls.append(args)
            self.assertIn(args[2],('describe','update'))
            market=args[3].split('-')[1].upper()
            if args[2]=='update':updated.add(market)
            return config(market,market in updated)
        with patch.object(mod,'gc',side_effect=gc),patch.object(mod,'preflight_bridge'), \
             patch.object(mod.base,'build'),contextlib.redirect_stdout(io.StringIO()):mod.deploy(SHA)
        writes=[c for c in calls if c[2]=='update']
        self.assertEqual(len(writes),2)
        self.assertTrue(all('HUNTER_READ_CHECKPOINTS=1' in c[-1] for c in writes))

    def test_repeat_deploy_verifies_without_build_or_update(self):
        with patch.object(mod,'gc',side_effect=lambda *a:config(a[3].split('-')[1].upper(),True)) as gc, \
             patch.object(mod,'preflight_bridge'),patch.object(mod.base,'build') as build, \
             contextlib.redirect_stdout(io.StringIO()):mod.deploy(SHA)
        build.assert_not_called()
        self.assertFalse(any(c.args[2]=='update' for c in gc.call_args_list))

    def test_readback_rejects_disabled_checkpoint_flag(self):
        before=config('US');after=config('US',True)
        mod.verify(before,after,'US',SHA)
        mod.deployment.container(after,'US')['env'][-1]['value']='0'
        with self.assertRaisesRegex(RuntimeError,'READBACK_MISMATCH'):mod.verify(before,after,'US',SHA)

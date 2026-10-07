import contextlib
import base64
import gzip
import hashlib
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest.mock import patch
from unittest.mock import Mock
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
    def test_old_endpoint_can_settle_without_redeploying_or_launching(self):
        info={'revision':'file:1','size':10}
        reader=Mock()
        reader._call.side_effect=[{'ok':True,'file':{'size':10}},
            {'ok':True,'read_protocol':'verified-chunks-v2','file':info}]
        with patch.object(mod.time,'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            path,got=mod.probe_metadata(reader,'US')
        self.assertEqual(got,info)
        self.assertEqual(path,'US/PHASE2/EARNINGS_HISTORY.json')
        sleep.assert_called_once_with(5)
        self.assertTrue(all(c.args==('file',) for c in reader._call.call_args_list))

    def test_preflight_reasons_are_distinct_and_bounded(self):
        cases=[({'ok':True,'file':{'size':10}},'US_BRIDGE_VERSION_NOT_SERVING',6),
               ({'ok':True,'read_protocol':'verified-chunks-v2','file':None},'US_PHASE2_HISTORY_FILE_MISSING',6),
               ({'ok':True,'read_protocol':'verified-chunks-v2','file':{'size':10}},'US_BRIDGE_REVISION_FIELD_MISSING',1)]
        for response,reason,count in cases:
            reader=Mock();reader._call.return_value=response
            with patch.object(mod.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,reason):mod.probe_metadata(reader,'US')
            self.assertEqual(reader._call.call_count,count)

    def test_actual_preflight_checks_compressed_bytes_for_both_markets(self):
        sys.path.insert(0,str(mod.ROOT/'hunter-global'))
        import runner
        data=b'financial history'*10000
        part=data[:131072];packed=gzip.compress(part)
        sha=lambda raw:hashlib.sha256(raw).hexdigest()
        for market in ('US','HK'):
            calls=[]
            def call(_reader,op,**kw):
                calls.append(op)
                self.assertEqual(kw['path'],market+'/PHASE2/EARNINGS_HISTORY.json')
                if op=='file':return {'ok':True,'read_protocol':'verified-chunks-v2',
                    'file':{'revision':'history:1','size':len(data)}}
                self.assertEqual(op,'read_verified_chunk')
                return {'revision':'history:1','size':len(data),'offset':0,'length':len(part),
                    'encoding':'gzip','sha256':sha(part),'compressed_sha256':sha(packed),
                    'data_base64':base64.b64encode(packed).decode()}
            def env(_doc,_entry,key):
                return 'https://script.google.com/macros/s/test/exec' if key.endswith('URL') else 'test-key'
            with patch.object(runner.Drive,'_call',call),patch.object(mod.base.deploy,'preflight_env_value',side_effect=env), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                mod.preflight_bridge(config(market),market)
            self.assertEqual(calls,['file','read_verified_chunk'])
            self.assertIn('COMPRESSED_READ_VERIFIED='+market,output.getvalue())

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

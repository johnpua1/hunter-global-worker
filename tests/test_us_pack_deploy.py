import contextlib
import copy
import importlib.util
import io
import pathlib
import types
import unittest
from unittest.mock import Mock, patch

import test_large_read_deploy as existing
from test_hk_read_cache_recovery_deploy import document
from test_us_phase2_deploy import execution

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('us_pack_deploy', ROOT / 'cloudrun/deploy-us-pack-readback-20261008.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SHA = 'e' * 40


class UsPackDeployTests(unittest.TestCase):
    def setUp(self):
        self.docs = {'US': document('US', mod.US_PREVIOUS), 'HK': document('HK', mod.HK_FIXED)}
        self.calls = []
        self.on_update = lambda: None
        base = existing.mod.base
        self.r = types.SimpleNamespace(gc=self.gc, executions=Mock(return_value=[]),
            deployment=existing.mod.deployment, verify=existing.mod.verify,
            base=types.SimpleNamespace(ROOT=ROOT, BASE_IMAGE=mod.BASE_IMAGE,
                configuration=base.configuration, recovery=base.recovery,
                deploy=base.deploy, build=Mock()))
        self.pin = patch.object(mod.subprocess, 'check_output', return_value=SHA+'\n')
        self.pin.start()
        self.addCleanup(self.pin.stop)

    def gc(self, *args):
        self.calls.append(args)
        market = args[3].split('-')[1].upper()
        if args[:3] == ('run', 'jobs', 'update'):
            self.assertEqual(market, 'US')
            self.assertEqual(args[4:], ('--image='+mod.BASE_IMAGE+SHA,
                                       '--update-env-vars=HUNTER_SOURCE_SHA='+SHA))
            c = self.r.deployment.container(self.docs['US'], 'US')
            c['image'] = mod.BASE_IMAGE+SHA
            for e in c['env']:
                if e['name'] == 'HUNTER_SOURCE_SHA':
                    e['value'] = SHA
            self.on_update()
        else:
            self.assertEqual(args[:3], ('run', 'jobs', 'describe'))
        return copy.deepcopy(self.docs[market])

    def run_deploy(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return mod.deploy_us(self.r, SHA)

    def test_only_us_template_changes_no_execution_is_started_or_cancelled(self):
        hk = copy.deepcopy(self.docs['HK'])
        self.r.executions.return_value = [execution('hunter-us-daily-existing', 'Unknown')]
        self.assertEqual(self.run_deploy()['status'], 'VERIFIED')
        self.assertEqual(self.docs['HK'], hk)
        self.r.base.build.assert_called_once_with(SHA)
        self.assertEqual(len([c for c in self.calls if c[2]=='update']), 1)

    def test_already_deployed_is_verified_without_rebuild_or_update(self):
        self.docs['US'] = document('US', SHA)
        self.assertEqual(self.run_deploy()['status'], 'VERIFIED')
        self.r.base.build.assert_not_called()
        self.assertFalse(any(c[2]=='update' for c in self.calls))

    def test_build_failure_cannot_mutate_jobs(self):
        self.r.base.build.side_effect = RuntimeError('BUILD_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'BUILD_FAILED'):
            self.run_deploy()
        self.assertFalse(any(c[2]=='update' for c in self.calls))

    def test_unknown_image_or_source_is_rejected_before_build(self):
        self.docs['US'] = document('US', 'a'*40)
        with self.assertRaisesRegex(RuntimeError, 'EXPECTED_DEPLOYMENT_REQUIRED'):
            self.run_deploy()
        self.r.base.build.assert_not_called()

    def test_config_change_during_build_prevents_update(self):
        def change(sha):
            self.r.deployment.container(self.docs['HK'], 'HK')['env'].append({'name':'CHANGED','value':'1'})
        self.r.base.build.side_effect = change
        with self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_CHANGED_DURING_BUILD:HK'):
            self.run_deploy()
        self.assertFalse(any(c[2]=='update' for c in self.calls))

    def test_client_annotation_order_changes_are_not_workload_changes(self):
        def change():
            t = self.docs['US']['spec']['template']
            t['metadata'] = {'annotations': {'run.googleapis.com/client-name': 'gcloud',
                                            'run.googleapis.com/client-version': 'new'}}
            self.r.deployment.container(self.docs['US'], 'US')['env'].reverse()
        self.on_update = change
        self.assertEqual(self.run_deploy()['status'], 'VERIFIED')

    def test_security_annotation_and_secret_changes_are_never_ignored(self):
        for kind in ('annotation', 'secret', 'runtime', 'hk'):
            with self.subTest(kind=kind):
                self.docs = {'US': document('US', mod.US_PREVIOUS), 'HK': document('HK', mod.HK_FIXED)}
                def change():
                    t = self.docs['US']['spec']['template']
                    if kind == 'annotation':
                        t['metadata'] = {'annotations': {'run.googleapis.com/vpc-access-connector': 'different'}}
                    elif kind == 'runtime':
                        t['spec']['template']['spec']['timeoutSeconds'] = 9999
                    elif kind == 'secret':
                        self.r.deployment.container(self.docs['US'], 'US')['env'][-1]['valueFrom']['secretKeyRef']['key'] = '8'
                    else:
                        self.r.deployment.container(self.docs['HK'], 'HK')['image'] = 'changed'
                self.on_update = change
                with self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_CHANGED_AFTER_UPDATE'):
                    self.run_deploy()

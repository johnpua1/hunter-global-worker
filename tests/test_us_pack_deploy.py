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

    def test_random_deployment_nonce_changes_are_ignored_but_other_labels_are_not(self):
        meta = self.docs['US']['spec']['template'].setdefault('metadata', {})
        meta['labels'] = {'client.knative.dev/nonce': 'old', 'owner': 'hunter'}
        def change():
            meta['labels']['client.knative.dev/nonce'] = 'new'
        self.on_update = change
        self.assertEqual(self.run_deploy()['status'], 'VERIFIED')
        before = copy.deepcopy(self.docs)
        meta['labels']['owner'] = 'different'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_CHANGED'):
            mod.unchanged(self.r, before, self.docs, 'TEST')


class ReadbackRecoveryTests(unittest.TestCase):
    def setUp(self):
        base = existing.mod.base
        self.docs = {'US': document('US', SHA), 'HK': document('HK', mod.HK_FIXED)}
        old = document('US', mod.US_PREVIOUS)
        self.reference = {'metadata': {'name': 'hunter-us-daily-55g2b'},
                          'spec': copy.deepcopy(old['spec']['template']['spec'])}
        self.calls = []
        def gc(*args):
            self.calls.append(args)
            if args == ('run', 'jobs', 'executions', 'describe', 'hunter-us-daily-55g2b'):
                return copy.deepcopy(self.reference)
            if args[:3] == ('run', 'jobs', 'describe'):
                return copy.deepcopy(self.docs[args[3].split('-')[1].upper()])
            raise AssertionError('Unexpected mutation: ' + repr(args))
        self.r = types.SimpleNamespace(gc=gc, deployment=existing.mod.deployment,
            verify=existing.mod.verify, base=types.SimpleNamespace(
                configuration=base.configuration, recovery=base.recovery, deploy=base.deploy))

    def test_already_updated_workload_matches_old_execution_without_writes(self):
        self.docs['US']['spec']['template']['metadata'] = {
            'labels': {'client.knative.dev/nonce': 'new'},
            'annotations': {'run.googleapis.com/execution-environment': 'gen2'}}
        self.reference['metadata']['annotations'] = {
            'run.googleapis.com/execution-environment': 'gen2',
            'run.googleapis.com/operation-id': 'generated'}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.verify_deployed_us(self.r, SHA)['status'], 'VERIFIED')
        self.assertEqual(len(self.calls), 5)

    def test_actual_secret_drift_is_blocked_and_values_are_not_printed(self):
        c = self.r.deployment.container(self.docs['US'], 'US')
        c['env'][-1]['valueFrom']['secretKeyRef']['key'] = 'sensitive-new-version'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(RuntimeError, 'REFERENCE_WORKLOAD_MISMATCH'):
                mod.verify_deployed_us(self.r, SHA)
        self.assertIn('PACK_FIX_DIFFERENT_PATHS=', out.getvalue())
        self.assertNotIn('sensitive-new-version', out.getvalue())

    def test_observed_execution_creator_metadata_does_not_change_workload(self):
        self.docs['US']['spec']['template']['metadata'] = {
            'annotations': {'run.googleapis.com/execution-environment': 'gen2'}}
        self.reference['metadata']['annotations'] = {
            'run.googleapis.com/execution-environment': 'gen2',
            'run.googleapis.com/creator': 'execution-creator@example.invalid',
            'run.googleapis.com/lastModifier': 'execution-modifier@example.invalid'}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mod.verify_deployed_us(self.r, SHA)['status'], 'VERIFIED')
        self.assertNotIn('example.invalid', out.getvalue())
        # Ignoring those exact audit fields must not bypass execution settings.
        self.reference['metadata']['annotations']['run.googleapis.com/execution-environment'] = 'gen1'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'REFERENCE_ANNOTATIONS_MISMATCH'):
            mod.verify_deployed_us(self.r, SHA)

    def test_unexpected_runtime_annotation_is_blocked(self):
        self.docs['US']['spec']['template']['metadata'] = {
            'annotations': {'run.googleapis.com/vpc-access-connector': 'other'}}
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'REFERENCE_ANNOTATIONS_MISMATCH'):
            mod.verify_deployed_us(self.r, SHA)

    def test_old_image_requires_diagnosis_and_is_not_silently_redeployed(self):
        self.docs['US'] = document('US', mod.US_PREVIOUS)
        with self.assertRaisesRegex(RuntimeError, 'NEW_IMAGE_NOT_DEPLOYED'):
            mod.verify_deployed_us(self.r, SHA)

import contextlib
import copy
import importlib.util
import io
import pathlib
import types
import unittest
from unittest.mock import Mock, patch

import test_large_read_deploy as existing
import test_resumable_release as release
from test_us_phase2_deploy import execution


PATH = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-hk-read-cache-recovery-20261008.py'
spec = importlib.util.spec_from_file_location('hk_read_cache_recovery_deploy', PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SHA = 'd' * 40


def document(market, sha=mod.PREVIOUS):
    doc = release.target(market)
    container = existing.mod.deployment.container(doc, market)
    container['image'] = mod.BASE_IMAGE + sha
    for entry in container['env']:
        if entry['name'] == 'HUNTER_SOURCE_SHA':
            entry['value'] = sha
    container['env'].append({'name': 'HUNTER_READ_CHECKPOINTS', 'value': '1'})
    container['env'].append({'name': 'PRESERVED_SECRET',
                             'valueFrom': {'secretKeyRef': {'name': 'secret-alias', 'key': '7'}}})
    return doc


class FakeCloud:
    def __init__(self):
        self.docs = {market: document(market) for market in ('US', 'HK')}
        self.rows = {'US': [], 'HK': []}
        self.calls = []
        self.reads = {'US': 0, 'HK': 0}
        self.on_read = lambda market, count: None
        self.on_build = lambda: None
        self.on_update = lambda: None
        base = existing.mod.base
        self.r = types.SimpleNamespace(
            gc=self.gc, executions=self.executions, deployment=existing.mod.deployment,
            verify=existing.mod.verify,
            base=types.SimpleNamespace(ROOT=mod.ROOT, BASE_IMAGE=mod.BASE_IMAGE,
                                      configuration=base.configuration, recovery=base.recovery,
                                      deploy=base.deploy, build=Mock(side_effect=lambda sha: self.on_build())))

    def gc(self, *args):
        self.calls.append(args)
        if args[:3] not in (('run', 'jobs', 'describe'), ('run', 'jobs', 'update')):
            raise AssertionError('Unexpected cloud mutation: ' + repr(args))
        market = args[3].split('-')[1].upper()
        if args[2] == 'update':
            if market != 'HK':
                raise AssertionError('US must never be updated')
            if args[4:] != ('--image=' + mod.BASE_IMAGE + SHA,
                            '--update-env-vars=HUNTER_SOURCE_SHA=' + SHA):
                raise AssertionError('Only image/source identity may be changed')
            container = self.r.deployment.container(self.docs['HK'], 'HK')
            container['image'] = mod.BASE_IMAGE + SHA
            for entry in container['env']:
                if entry['name'] == 'HUNTER_SOURCE_SHA':
                    entry['value'] = SHA
            self.on_update()
        return copy.deepcopy(self.docs[market])

    def executions(self, market):
        self.reads[market] += 1
        self.on_read(market, self.reads[market])
        return copy.deepcopy(self.rows[market])

    def writes(self):
        return [args for args in self.calls if args[2] == 'update']


class HkReadCacheDeploymentTests(unittest.TestCase):
    def setUp(self):
        mod._BUILT_SHAS.clear()
        self.cloud = FakeCloud()
        self.output = io.StringIO()
        self.pin = patch.object(mod.subprocess, 'check_output', return_value=SHA + '\n')
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.quiet = contextlib.redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def deploy(self):
        return mod.deploy_hk(self.cloud.r, SHA)

    def test_active_us_does_not_block_hk_only_build_and_update(self):
        self.cloud.rows['US'] = [execution('hunter-us-daily-xc6qn', 'Unknown')]
        us_before = copy.deepcopy(self.cloud.docs['US'])
        result = self.deploy()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertTrue(result['updated'])
        self.assertEqual(result['active']['US'], ['hunter-us-daily-xc6qn'])
        self.assertEqual(result['docs']['US'], us_before)
        self.assertEqual(self.cloud.docs['US'], us_before)
        self.cloud.r.base.build.assert_called_once_with(SHA)
        self.assertEqual(len(self.cloud.writes()), 1)
        self.assertNotIn('secret-alias', self.output.getvalue())

    def test_queued_hk_waits_without_build_update_start_or_cancel(self):
        self.cloud.rows['HK'] = [execution('hunter-hk-daily-queued', 'Unknown')]
        result = self.deploy()
        self.assertEqual(result['status'], 'WAIT_HK_ACTIVE')
        self.assertEqual(set(result['docs']), {'US', 'HK'})
        self.assertEqual(result['active']['HK'], ['hunter-hk-daily-queued'])
        self.cloud.r.base.build.assert_not_called()
        self.assertEqual(self.cloud.writes(), [])

    def test_hk_starts_during_build_wait_then_reuse_successful_build(self):
        self.cloud.on_build = lambda: self.cloud.rows['HK'].append(
            execution('hunter-hk-daily-scheduled', 'Unknown'))
        self.assertEqual(self.deploy()['status'], 'WAIT_HK_ACTIVE')
        self.assertEqual(self.cloud.writes(), [])
        self.cloud.rows['HK'].clear()
        result = self.deploy()
        self.assertEqual(result['status'], 'VERIFIED')
        self.cloud.r.base.build.assert_called_once_with(SHA)
        self.assertEqual(len(self.cloud.writes()), 1)

    def test_hk_starts_at_final_preupdate_check_is_preserved(self):
        def read(market, count):
            if market == 'HK' and count == 3:
                self.cloud.rows['HK'].append(execution('hunter-hk-daily-new', 'Unknown'))
        self.cloud.on_read = read
        self.assertEqual(self.deploy()['status'], 'WAIT_HK_ACTIVE')
        self.assertEqual(self.cloud.writes(), [])

    def test_failed_build_cannot_mutate_jobs_or_be_cached(self):
        self.cloud.r.base.build.side_effect = RuntimeError('BUILD_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'BUILD_FAILED'):
            self.deploy()
        self.assertNotIn(SHA, mod._BUILT_SHAS)
        self.assertEqual(self.cloud.writes(), [])

    def test_either_market_config_change_during_build_prevents_update(self):
        for market in ('US', 'HK'):
            with self.subTest(market=market):
                mod._BUILT_SHAS.clear()
                self.cloud = FakeCloud()
                def build():
                    container = self.cloud.r.deployment.container(self.cloud.docs[market], market)
                    container['env'].append({'name': 'EXTERNAL_CHANGE', 'value': '1'})
                self.cloud.on_build = build
                with self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_CHANGED_DURING_BUILD:' + market):
                    self.deploy()
                self.assertEqual(self.cloud.writes(), [])

    def test_already_fixed_repeat_skips_build_and_update_even_if_active(self):
        self.cloud.docs['HK'] = document('HK', SHA)
        self.cloud.rows['HK'] = [execution('hunter-hk-daily-fixed', 'Unknown')]
        result = self.deploy()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertFalse(result['updated'])
        self.cloud.r.base.build.assert_not_called()
        self.assertEqual(self.cloud.writes(), [])
        self.assertEqual(result['active']['HK'], ['hunter-hk-daily-fixed'])

    def test_unknown_or_mismatched_image_source_fails_before_build(self):
        for image_sha, source_sha in [('a' * 40, 'a' * 40), (SHA, mod.PREVIOUS), (mod.PREVIOUS, SHA)]:
            with self.subTest(image=image_sha, source=source_sha):
                self.cloud = FakeCloud()
                container = self.cloud.r.deployment.container(self.cloud.docs['HK'], 'HK')
                container['image'] = mod.BASE_IMAGE + image_sha
                for entry in container['env']:
                    if entry['name'] == 'HUNTER_SOURCE_SHA':
                        entry['value'] = source_sha
                with self.assertRaisesRegex(RuntimeError, 'EXPECTED_DEPLOYMENT_REQUIRED'):
                    self.deploy()
                self.cloud.r.base.build.assert_not_called()
                self.assertEqual(self.cloud.writes(), [])

    def test_each_market_requires_existing_flags_and_budget_before_build(self):
        for market in ('US', 'HK'):
            for key in ('HUNTER_INCREMENTAL_INPUTS', 'HUNTER_RESUMABLE_RUN',
                        'HUNTER_READ_CHECKPOINTS', 'timeout', 'source'):
                with self.subTest(market=market, key=key):
                    self.cloud = FakeCloud()
                    task = self.cloud.docs[market]['spec']['template']['spec']['template']['spec']
                    if key == 'timeout':
                        task['timeoutSeconds'] = 3600
                    else:
                        name = 'HUNTER_SOURCE_SHA' if key == 'source' else key
                        for entry in task['containers'][0]['env']:
                            if entry['name'] == name:
                                entry['value'] = '0'
                    with self.assertRaises(RuntimeError):
                        self.deploy()
                    self.cloud.r.base.build.assert_not_called()
                    self.assertEqual(self.cloud.writes(), [])

    def test_readback_detects_budget_secret_resource_and_us_changes(self):
        for change in ('budget', 'secret', 'resource', 'us'):
            with self.subTest(change=change):
                mod._BUILT_SHAS.clear()
                self.cloud = FakeCloud()
                def mutate():
                    market = 'US' if change == 'us' else 'HK'
                    task = self.cloud.docs[market]['spec']['template']['spec']['template']['spec']
                    if change == 'budget':
                        task['timeoutSeconds'] += 1
                    elif change == 'resource':
                        task['containers'][0]['resources']['limits']['memory'] = '99Gi'
                    elif change == 'secret':
                        task['containers'][0]['env'][-1]['valueFrom']['secretKeyRef']['key'] = '8'
                    else:
                        task['containers'][0]['env'].append({'name': 'UNEXPECTED', 'value': '1'})
                self.cloud.on_update = mutate
                with self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_CHANGED_AFTER_UPDATE'):
                    self.deploy()

    def test_environment_order_change_is_not_a_configuration_change(self):
        def reorder():
            for market in ('US', 'HK'):
                self.cloud.r.deployment.container(self.cloud.docs[market], market)['env'].reverse()
        self.cloud.on_update = reorder
        self.assertEqual(self.deploy()['status'], 'VERIFIED')

    def test_wrong_head_or_build_root_prevents_even_cloud_reads(self):
        with patch.object(mod.subprocess, 'check_output', return_value=mod.PREVIOUS):
            with self.assertRaisesRegex(RuntimeError, 'PINNED_CHECKOUT_REQUIRED'):
                self.deploy()
        self.cloud.r.base.ROOT = mod.ROOT / 'different-checkout'
        with self.assertRaisesRegex(RuntimeError, 'BUILD_ROOT_MISMATCH'):
            self.deploy()
        self.assertEqual(self.cloud.calls, [])


if __name__ == '__main__':
    unittest.main()

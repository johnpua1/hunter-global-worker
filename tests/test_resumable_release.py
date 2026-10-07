import contextlib
import copy
import importlib.util
import io
import pathlib
import unittest
from unittest.mock import patch
from test_us_phase2_deploy import execution
from test_both_phase2_recovery import config

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-resumable-phase2-20261007.py'
spec = importlib.util.spec_from_file_location('resumable_release', path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
SHA = 'b' * 40


def target(market):
    doc = config(market)
    c = mod.deployment.container(doc, market)
    c['image'] = mod.base.BASE_IMAGE + SHA
    for e in c['env']:
        if e['name'] == 'HUNTER_SOURCE_SHA': e['value'] = SHA
    c['env'].append({'name': 'HUNTER_RESUMABLE_RUN', 'value': '1'})
    return doc


class ResumableReleaseTests(unittest.TestCase):
    def test_deploy_changes_only_image_and_intended_flags_never_starts_or_cancels(self):
        calls = []; updated = set()
        def gc(*args):
            calls.append(args)
            self.assertIn(args[2], ('describe', 'update'))
            market = args[3].split('-')[1].upper()
            if args[2] == 'update': updated.add(market)
            return target(market) if market in updated else config(market)
        with patch.object(mod, 'gc', side_effect=gc), patch.object(mod.deployment, 'checkpoint'), \
             patch.object(mod.base, 'build') as build, contextlib.redirect_stdout(io.StringIO()):
            mod.deploy(SHA)
            build.assert_called_once_with(SHA)
        writes = [c for c in calls if c[2] == 'update']
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(len(c) == 6 and c[4] == '--image=' + mod.base.BASE_IMAGE + SHA for c in writes))

    def test_failed_build_does_not_change_jobs(self):
        def gc(*args):
            self.assertEqual(args[2], 'describe')
            return config(args[3].split('-')[1].upper())
        with patch.object(mod, 'gc', side_effect=gc), patch.object(mod.deployment, 'checkpoint'), \
             patch.object(mod.base, 'build', side_effect=RuntimeError('BUILD_FAILED')):
            with self.assertRaisesRegex(RuntimeError, 'BUILD_FAILED'): mod.deploy(SHA)

    def test_readback_rejects_changed_resources_budget_or_args(self):
        for change in ('memory', 'timeout', 'args'):
            before, after = config('HK'), target('HK')
            task = after['spec']['template']['spec']['template']['spec']
            if change == 'memory': task['containers'][0]['resources']['limits']['memory'] = '8Gi'
            elif change == 'timeout': task['timeoutSeconds'] = 14400
            else: task['containers'][0]['args'] += ['--phase2-only']
            with self.assertRaises(RuntimeError): mod.verify(before, after, 'HK', SHA)

    def test_race_before_launch_or_existing_queued_execution_is_preserved(self):
        queued = [execution('hunter-hk-daily-queued', 'Unknown')]
        self.assertIsNone(mod.latest_failure('HK', queued))
        with patch.object(mod, 'gc', return_value=target('HK')) as gc, \
             patch.object(mod.resume, 'preflight', return_value=True), \
             patch.object(mod, 'executions', return_value=queued), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(mod.launch('HK', target('HK'), SHA, 'old'))
            self.assertFalse(any(c.args[2] == 'execute' for c in gc.call_args_list))

    def follow_case(self, budget_failure=True, completed=False):
        launches = []
        cp = {'market': 'HK', 'last_completed_date': '2026-10-07'}
        if completed: cp['phase2_completed_date'] = '2026-10-07'
        def launch(*args): launches.append(args); return 'own'
        with patch.object(mod, 'executions', return_value=[execution('failed')]), \
             patch.object(mod, 'checkpoint', return_value=cp), \
             patch.object(mod, 'timeout_failure', return_value=budget_failure), \
             patch.object(mod, 'launch', side_effect=launch), patch.object(mod.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()):
            if completed: mod.follow({'HK': target('HK')}, SHA)
            else:
                with self.assertRaisesRegex(RuntimeError, 'COMPLETION_NOT_VERIFIED'):
                    mod.follow({'HK': target('HK')}, SHA)
        return launches

    def test_follow_checks_receipt_and_never_repeats_a_complete_market(self):
        self.assertEqual(self.follow_case(completed=True), [])

    def test_follow_retries_only_budget_failures_with_finite_bound(self):
        self.assertEqual(self.follow_case(budget_failure=False), [])
        self.assertEqual(len(self.follow_case()), 3)

    def test_successful_execution_without_phase2_receipt_is_not_success(self):
        with patch.object(mod, 'executions', return_value=[execution('done', 'True')]), \
             patch.object(mod, 'checkpoint', return_value={'market':'US','last_completed_date':'2026-10-06'}), \
             patch.object(mod, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'COMPLETION_NOT_VERIFIED'):
                mod.follow({'US': target('US')}, SHA)
            launch.assert_not_called()

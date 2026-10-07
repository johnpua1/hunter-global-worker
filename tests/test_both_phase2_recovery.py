import contextlib
import copy
import importlib.util
import io
import pathlib
import unittest
from unittest.mock import patch
from test_us_phase2_deploy import job, execution

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/resume-both-phase2-incremental-20261007.py'
spec = importlib.util.spec_from_file_location('both_phase2_recovery', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def config(market):
    doc = job(mod.IMAGE_SHA)
    c = doc['spec']['template']['spec']['template']['spec']['containers'][0]
    c['args'][-1] = market
    c['env'].append({'name': 'HUNTER_INCREMENTAL_INPUTS', 'value': '1'})
    return doc


class BothPhase2RecoveryTests(unittest.TestCase):
    def run_case(self, market='HK', active=False, newer=False, committed=False,
                 drift=False, race=False, preflight_error=False, bad_response=False):
        calls = []; launched = False; descriptions = 0
        own = 'hunter-' + market.lower() + '-daily-resume'
        old = mod.TARGETS[market][0]
        def gc(*args):
            nonlocal launched, descriptions
            calls.append(args)
            if args[:4] == ('run', 'jobs', 'executions', 'list'):
                if launched:
                    rows = [execution(own, 'Unknown')]
                    if race: rows.append(execution('other-execution', 'Unknown'))
                    return rows
                if active: return [execution(old), execution('queued', 'Unknown')]
                return [execution('newer' if newer else old)]
            if args[:4] == ('run', 'jobs', 'executions', 'cancel'):
                self.assertEqual(args[4], own)
                return {}
            self.assertEqual(args[3], 'hunter-' + market.lower() + '-daily')
            if args[2] == 'describe':
                descriptions += 1
                doc = config(market)
                if drift and descriptions > 1:
                    doc['spec']['template']['spec']['parallelism'] = 2
                return doc
            if args[2] == 'execute':
                launched = True
                return {} if bad_response else execution(own, 'Unknown')
            self.fail('Unexpected mutation: ' + str(args))
        error = None
        with patch.object(mod, 'gc', side_effect=gc), \
             patch.object(mod, 'preflight', return_value=not committed,
                          side_effect=RuntimeError('VALIDATION_FAILED') if preflight_error else None), \
             contextlib.redirect_stdout(io.StringIO()):
            try: mod.recover(market)
            except RuntimeError as exc: error = str(exc)
        return calls, error

    def test_both_markets_only_launch_phase2_with_expected_dates(self):
        for market in ('US', 'HK'):
            calls, error = self.run_case(market=market)
            self.assertIsNone(error)
            launches = [c for c in calls if c[2] == 'execute']
            self.assertEqual(len(launches), 1)
            self.assertIn('--args=--mode,auto,--market,' + market + ',--phase2-only,--as-of,' + mod.TARGETS[market][1], launches[0])
            self.assertFalse(any(c[2] in ('update', 'delete') for c in calls))

    def test_queued_newer_or_committed_execution_does_not_launch(self):
        for kwargs in ({'active': True}, {'newer': True}, {'committed': True}):
            calls, error = self.run_case(**kwargs)
            self.assertIsNone(error)
            self.assertFalse(any(c[2] == 'execute' for c in calls))

    def test_validation_or_configuration_drift_prevents_launch(self):
        for kwargs in ({'drift': True}, {'preflight_error': True}):
            calls, error = self.run_case(**kwargs)
            self.assertIsNotNone(error)
            self.assertFalse(any(c[2] == 'execute' for c in calls))

    def test_racing_external_launch_cancels_only_own_new_execution(self):
        calls, error = self.run_case(race=True)
        self.assertIn('OWN_EXECUTION_CANCEL_REQUESTED', error)
        cancellations = [c for c in calls if c[:4] == ('run', 'jobs', 'executions', 'cancel')]
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(cancellations[0][4], 'hunter-hk-daily-resume')

    def test_ambiguous_launch_response_is_not_retried(self):
        calls, error = self.run_case(bad_response=True)
        self.assertIn('DO_NOT_REPEAT_BLINDLY', error)
        self.assertEqual(sum(c[2] == 'execute' for c in calls), 1)

    def test_wrong_image_disabled_cache_or_retry_budget_is_rejected(self):
        for change in ('image', 'cache', 'retries'):
            doc = copy.deepcopy(config('US'))
            task = doc['spec']['template']['spec']['template']['spec']
            if change == 'image': task['containers'][0]['image'] = 'old'
            elif change == 'cache': task['containers'][0]['env'][-1]['value'] = '0'
            else: task['maxRetries'] = 1
            with self.assertRaisesRegex(RuntimeError, 'EXPECTED_INCREMENTAL_DEPLOYMENT'):
                mod.verify_job(doc, 'US')

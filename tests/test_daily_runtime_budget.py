"""Guard against increasing capacity/budget or restarting active DAILY jobs."""
import contextlib
import copy
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hunter-global'))
spec = importlib.util.spec_from_file_location('runtime_deploy', ROOT / 'cloudrun/deploy-sync-fix-20261007.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
SHA = 'a' * 40
BASE = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'


def job(market='US', seconds=3600, retries=1):
    return {'spec': {'template': {'spec': {'taskCount': 1, 'parallelism': 1,
        'template': {'spec': {'timeoutSeconds': str(seconds), 'maxRetries': retries,
            'containers': [{'image': BASE + 'a6de79fb94a1c4fbd9f99312f4744d4aa0d8d67f',
                'args': ['--mode', 'auto', '--market', market],
                'resources': {'limits': {'cpu': '1000m', 'memory': '512Mi'}},
                'env': [{'name': 'APPS_SCRIPT_WEBAPP_URL', 'value': 'test-url'},
                        {'name': 'APPS_SCRIPT_SHARED_KEY', 'value': 'test-key'}]}]}}}}}}


class DailyRuntimeBudgetTests(unittest.TestCase):
    def test_v1_v2_and_repeat_application_preserve_total_budget(self):
        for seconds, retries in ((3600, 1), (7200, 0)):
            v1 = job(seconds=seconds, retries=retries)
            task = copy.deepcopy(v1['spec']['template']['spec']['template']['spec'])
            task.pop('timeoutSeconds')
            task['timeout'] = str(seconds) + 's'
            v2 = {'template': {'taskCount': 1, 'parallelism': 1, 'template': task}}
            for doc in (v1, v2):
                with self.subTest(seconds=seconds, v2=doc is v2):
                    self.assertEqual(deploy.daily_runtime(doc), (seconds, retries))
                    self.assertEqual(deploy.runtime_repair_flags(doc), ['--task-timeout=120m', '--max-retries=0'])
                    self.assertEqual(seconds * (retries + 1), 7200)
            self.assertEqual(deploy.runtime_capacity(v1), deploy.runtime_capacity(v2))

    def test_unknown_or_smaller_budget_is_not_silently_extended(self):
        for seconds, retries in ((3600, 0), (1800, 1), (3600, 3), (7200, 1), (10800, 0)):
            with self.subTest(seconds=seconds, retries=retries):
                with self.assertRaisesRegex(RuntimeError, 'BUDGET_NOT_APPROVED'):
                    deploy.runtime_repair_flags(job(seconds=seconds, retries=retries))
        with self.assertRaisesRegex(RuntimeError, 'CONFIGURATION_MISSING'):
            deploy.runtime_repair_flags({})

    def run_deploy(self, *, smaller_budget=False, changed_capacity=False, race=False, no_start=False):
        docs = {'hunter-us-daily': job(), 'hunter-hk-daily': job('HK')}
        if smaller_budget:
            docs['hunter-us-daily']['spec']['template']['spec']['template']['spec']['maxRetries'] = 0
        calls, descriptions = [], {}
        def gc(*args):
            calls.append(args)
            name = args[3]
            if args[:3] == ('run', 'jobs', 'describe'):
                descriptions[name] = descriptions.get(name, 0) + 1
                result = copy.deepcopy(docs[name])
                if race and descriptions[name] == 2:
                    result['spec']['template']['spec']['template']['spec']['containers'][0]['image'] = BASE + 'b' * 40
                return result
            self.assertEqual(args[:3], ('run', 'jobs', 'update'))
            flags = dict(a[2:].split('=', 1) for a in args[4:])
            self.assertEqual(flags['task-timeout'], '120m')
            self.assertEqual(flags['max-retries'], '0')
            self.assertFalse({'cpu', 'memory', 'tasks', 'parallelism', 'set-env-vars', 'set-secrets'} & flags.keys())
            task = docs[name]['spec']['template']['spec']['template']['spec']
            task.update(timeoutSeconds='7200', maxRetries=0)
            task['containers'][0]['image'] = flags['image']
            task['containers'][0]['env'].extend({'name': k, 'value': v}
                for k, v in (pair.split('=', 1) for pair in flags['update-env-vars'].split(',')))
            if changed_capacity:
                task['containers'][0]['resources']['limits']['cpu'] = '2000m'
            return {}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(deploy.sys, 'argv', ['deploy', SHA, '--daily-only', '--repair-timeout'] + (['--no-start'] if no_start else [])), \
                patch.object(deploy.subprocess, 'check_output', return_value=SHA), \
                patch.object(deploy.subprocess, 'run') as build, \
                patch.object(deploy.tempfile, 'mkdtemp', return_value=tmp), \
                patch.object(deploy.os, 'umask'), \
                patch.object(deploy.recovery, 'gc', side_effect=gc), \
                patch.object(deploy.recovery, 'executions', return_value=[{'name': 'existing-active'}]) as executions, \
                patch('runner.Drive') as drive, contextlib.redirect_stdout(output):
            drive.return_value.json.side_effect = lambda path: {'market': path.split('/')[0], 'securities': [{}]}
            if smaller_budget:
                with self.assertRaisesRegex(RuntimeError, 'BUDGET_NOT_APPROVED'):
                    deploy.main()
                build.assert_not_called()
            elif changed_capacity:
                with self.assertRaisesRegex(RuntimeError, 'RUNTIME_READBACK_FAILED'):
                    deploy.main()
            elif race:
                with self.assertRaisesRegex(RuntimeError, 'JOB_CHANGED_DURING_BUILD'):
                    deploy.main()
                self.assertFalse(any(c[:3] == ('run', 'jobs', 'update') for c in calls))
            else:
                deploy.main()
                self.assertEqual(output.getvalue().count('RUNTIME_VERIFIED='), 2)
                if no_start:
                    executions.assert_not_called()
                    self.assertIn('JOBS_UPDATED_ONLY=', output.getvalue())
                else:
                    self.assertEqual(output.getvalue().count('EXISTING_EXECUTION='), 2)
            self.assertFalse(any(c[:3] in [('run', 'jobs', 'execute'), ('run', 'jobs', 'executions')] for c in calls))
        return calls

    def test_update_retains_active_tasks_capacity_secrets_and_budget(self):
        self.run_deploy()

    def test_update_only_never_dispatches_or_cancels_any_execution(self):
        self.run_deploy(no_start=True)

    def test_smaller_production_budget_blocks_before_build(self):
        self.run_deploy(smaller_budget=True)

    def test_capacity_readback_mismatch_blocks_launch(self):
        self.run_deploy(changed_capacity=True)

    def test_configuration_race_does_not_overwrite_new_image(self):
        self.run_deploy(race=True)


if __name__ == '__main__':
    unittest.main()

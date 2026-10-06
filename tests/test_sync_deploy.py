import importlib.util
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'hunter-global'))


class SyncDeploymentTests(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-sync-fix-20261007.py'
        spec = importlib.util.spec_from_file_location('sync_secret_test', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_literal_value_does_not_access_secrets(self):
        with patch.object(self.module.subprocess, 'run') as access:
            self.assertEqual(self.module.preflight_env_value({}, {'value': 'literal'}, 'TEST'), 'literal')
            access.assert_not_called()

    def test_existing_v1_v2_and_cross_project_secret_references(self):
        cases = [
            ({}, {'valueFrom': {'secretKeyRef': {'name': 'bridge', 'key': '2'}}},
             'rgs-hunter-global', 'bridge', '2'),
            ({}, {'valueSource': {'secretKeyRef': {'secret': 'bridge', 'version': 'latest'}}},
             'rgs-hunter-global', 'bridge', 'latest'),
            ({}, {'valueSource': {'secretKeyRef': {'secret': 'projects/123/secrets/bridge', 'version': '4'}}},
             '123', 'bridge', '4'),
            ({'spec': {'template': {'metadata': {'annotations': {'run.googleapis.com/secrets':
                'other:projects/456/secrets/unrelated,lookup:projects/123/secrets/bridge'}}}}},
             {'valueFrom': {'secretKeyRef': {'name': 'lookup', 'key': 'latest'}}}, '123', 'bridge', 'latest'),
        ]
        for doc, entry, project, secret, version in cases:
            with self.subTest(entry=entry), patch.object(self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, 'private-payload', '')) as access:
                self.assertEqual(self.module.preflight_env_value(doc, entry, 'TEST'), 'private-payload')
                access.assert_called_once_with(['gcloud', 'secrets', 'versions', 'access', version,
                    '--secret=' + secret, '--project=' + project, '--quiet'],
                    capture_output=True, text=True, timeout=60)

    def test_secret_failure_never_discloses_payload_or_stderr(self):
        entry = {'valueFrom': {'secretKeyRef': {'name': 'bridge', 'key': 'latest'}}}
        for outcome in [subprocess.CompletedProcess([], 1, 'private-payload', 'private-error'),
                        subprocess.TimeoutExpired('gcloud', 60, output='private-payload', stderr='private-error')]:
            kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with self.subTest(outcome=type(outcome)), patch.object(self.module.subprocess, 'run', **kwargs):
                with self.assertRaisesRegex(RuntimeError, '^PREFLIGHT_SECRET_ACCESS_FAILED:TEST$'):
                    self.module.preflight_env_value({}, entry, 'TEST')

    def test_missing_binding_and_invalid_reference_fail_before_secret_access(self):
        for entry in [{}, {'valueFrom': {'secretKeyRef': {'name': '../secret', 'key': 'latest'}}},
                      {'valueFrom': {'secretKeyRef': {'name': 'bridge', 'key': '--help'}}}]:
            with self.subTest(entry=entry), patch.object(self.module.subprocess, 'run') as access:
                with self.assertRaises(RuntimeError):
                    self.module.preflight_env_value({}, entry, 'TEST')
                access.assert_not_called()

    def test_daily_secret_preflight_restores_environment_and_preserves_bindings(self):
        module, sha = self.module, 'a' * 40
        base = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
        updates = {}
        bindings = [
            {'name': 'APPS_SCRIPT_WEBAPP_URL', 'valueFrom': {'secretKeyRef': {'name': 'url', 'key': 'latest'}}},
            {'name': 'APPS_SCRIPT_SHARED_KEY', 'valueSource': {'secretKeyRef': {'secret': 'key', 'version': '3'}}}]
        def gc(*args):
            job = args[3]
            if args[:3] == ('run', 'jobs', 'describe'):
                return {'containers': [{'image': base + (sha if job in updates else '6cbba32e4754868e93da9f00ca19371e92a5e8e5'),
                    'args': ['--mode', 'auto', '--market', job.split('-')[1].upper()],
                    'env': bindings + [{'name': k, 'value': v} for k, v in updates.get(job, {}).items()]}]}
            self.assertEqual(args[:3], ('run', 'jobs', 'update'))
            updates[job] = dict(v.split('=', 1) for v in args[5].split('=', 1)[1].split(','))
            self.assertNotIn('APPS_SCRIPT_WEBAPP_URL', updates[job])
            self.assertNotIn('APPS_SCRIPT_SHARED_KEY', updates[job])
            return {}
        def run(args, **kwargs):
            if args[1] == 'secrets':
                return subprocess.CompletedProcess(args, 0, 'private-payload', '')
            self.assertEqual(args[:3], ['gcloud', 'builds', 'submit'])
            self.assertEqual(dict(os.environ), original)
            return subprocess.CompletedProcess(args, 0)
        def read(path):
            self.assertEqual(os.environ['APPS_SCRIPT_SHARED_KEY'], 'private-payload')
            return {'market': path.split('/')[0], 'securities': [{}]}
        original, output = dict(os.environ), io.StringIO()
        with tempfile.TemporaryDirectory() as work, \
                patch.object(module.sys, 'argv', ['deploy', sha, '--daily-only']), \
                patch.object(module.subprocess, 'check_output', return_value=sha), \
                patch.object(module.subprocess, 'run', side_effect=run), \
                patch.object(module.tempfile, 'mkdtemp', return_value=work), \
                patch.object(module.os, 'umask'), \
                patch.object(module.recovery, 'gc', side_effect=gc), \
                patch.object(module.recovery, 'executions', return_value=[{'name': 'existing'}]), \
                patch('runner.Drive') as drive, contextlib.redirect_stdout(output):
            drive.return_value.json.side_effect = read
            module.main()
        self.assertEqual(set(updates), {'hunter-us-daily', 'hunter-hk-daily'})
        self.assertEqual(dict(os.environ), original)
        self.assertNotIn('private-payload', output.getvalue())
        self.assertIn('PRODUCTION_CONFIG_FULL_READ_PASS=US:1', output.getvalue())
        self.assertIn('PRODUCTION_CONFIG_FULL_READ_PASS=HK:1', output.getvalue())

    def test_daily_preflight_blocks_deploy_when_real_configuration_reads_wrong_market(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-sync-fix-20261007.py'
        spec = importlib.util.spec_from_file_location('sync_preflight_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sha = 'a' * 40
        base = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
        def gc(*args):
            self.assertEqual(args[:3], ('run', 'jobs', 'describe'))
            market = args[3].split('-')[1].upper()
            return {'containers': [{'image': base + '6cbba32e4754868e93da9f00ca19371e92a5e8e5',
                    'args': ['--mode', 'auto', '--market', market], 'env': [
                        {'name': 'APPS_SCRIPT_WEBAPP_URL', 'value': 'test-url'},
                        {'name': 'APPS_SCRIPT_SHARED_KEY', 'value': 'test-key'}]}]}
        with tempfile.TemporaryDirectory() as work, \
                patch.object(module.sys, 'argv', ['deploy', sha, '--daily-only']), \
                patch.object(module.subprocess, 'check_output', return_value=sha), \
                patch.object(module.subprocess, 'run') as build, \
                patch.object(module.tempfile, 'mkdtemp', return_value=work), \
                patch.object(module.os, 'umask'), \
                patch.object(module.recovery, 'gc', side_effect=gc), \
                patch('runner.Drive') as drive:
            drive.return_value.json.return_value = {'market': 'HK', 'securities': [{}]}
            with self.assertRaisesRegex(RuntimeError, 'PREFLIGHT_CONTENT_INVALID:US'):
                module.main()
            build.assert_not_called()

    def test_secret_access_failure_blocks_build_and_restores_environment(self):
        module, sha = self.module, 'a' * 40
        base = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
        def gc(*args):
            self.assertEqual(args[:3], ('run', 'jobs', 'describe'))
            return {'containers': [{'image': base + '6cbba32e4754868e93da9f00ca19371e92a5e8e5',
                'args': ['--mode', 'auto', '--market', args[3].split('-')[1].upper()], 'env': [
                    {'name': 'APPS_SCRIPT_WEBAPP_URL', 'value': 'temporary-url'},
                    {'name': 'APPS_SCRIPT_SHARED_KEY', 'valueFrom': {'secretKeyRef': {'name': 'key', 'key': 'latest'}}}]}]}
        original = dict(os.environ)
        with tempfile.TemporaryDirectory() as work, \
                patch.object(module.sys, 'argv', ['deploy', sha, '--daily-only']), \
                patch.object(module.subprocess, 'check_output', return_value=sha), \
                patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')) as access, \
                patch.object(module.tempfile, 'mkdtemp', return_value=work), \
                patch.object(module.os, 'umask'), \
                patch.object(module.recovery, 'gc', side_effect=gc):
            with self.assertRaisesRegex(RuntimeError, 'PREFLIGHT_SECRET_ACCESS_FAILED:APPS_SCRIPT_SHARED_KEY'):
                module.main()
            access.assert_called_once()
            self.assertEqual(access.call_args.args[0][:4], ['gcloud', 'secrets', 'versions', 'access'])
        self.assertEqual(dict(os.environ), original)

    def test_updates_writer_and_consumers_but_does_not_duplicate_active_execution(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-sync-fix-20261007.py'
        spec = importlib.util.spec_from_file_location('sync_deploy_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sha = 'a' * 40
        old = '760c2808f786df376575839ac0bb4334989955fe'
        base = 'us-central1-docker.pkg.dev/rgs-hunter-global/hunter-worker/runner:'
        updates, calls = {}, []
        def gc(*args):
            calls.append(args)
            if args[:3] == ('run', 'jobs', 'describe'):
                job = args[3]
                argv = ['/app/maintenance.py'] if job == 'hunter-maintenance' else ['--mode', 'auto', '--market', job.split('-')[1].upper()]
                env = updates.get(job, {})
                return {'containers': [{'image': base + (sha if env else old), 'args': argv,
                                        'env': [{'name': k, 'value': v} for k, v in env.items()]}]}
            if args[:3] == ('run', 'jobs', 'update'):
                updates[args[3]] = dict(v.split('=', 1) for v in args[5].split('=', 1)[1].split(','))
                return {}
            if args[:3] == ('run', 'jobs', 'execute'):
                return {'name': 'hunter-hk-daily-own'}
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as work, \
                patch.object(module.sys, 'argv', ['deploy', sha]), \
                patch.object(module.subprocess, 'check_output', return_value=sha), \
                patch.object(module.subprocess, 'run') as build, \
                patch.object(module.tempfile, 'mkdtemp', return_value=work), \
                patch.object(module.os, 'umask'), \
                patch.object(module.recovery, 'gc', side_effect=gc), \
                patch.object(module.recovery, 'executions', side_effect=[
                    [{'name': 'hunter-us-daily-active'}], [], [], [{'name': 'hunter-hk-daily-own'}]]):
            module.main()
        self.assertEqual(set(updates), {'hunter-us-daily', 'hunter-hk-daily', 'hunter-maintenance'})
        self.assertEqual([x[3] for x in calls if x[:3] == ('run', 'jobs', 'execute')], ['hunter-hk-daily'])
        self.assertFalse(any('--memory' in a or '--cpu' in a for c in calls for a in c))
        build.assert_called_once()


if __name__ == '__main__':
    unittest.main()

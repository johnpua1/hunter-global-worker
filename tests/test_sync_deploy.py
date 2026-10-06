import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch


class SyncDeploymentTests(unittest.TestCase):
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

import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/resume-us-after-timeout-20261007.py'
spec = importlib.util.spec_from_file_location('us_resume', path)
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)


def execution(name=resume.OLD, status='False'):
    return {'metadata': {'name': name}, 'status': {
        'conditions': [{'type': 'Completed', 'status': status}],
        **({'completionTime': '2026-10-07T02:21:02Z'} if status != 'Unknown' else {})}}


def job():
    return {'spec': {'template': {'spec': {'template': {'spec': {
        'timeoutSeconds': 7200, 'maxRetries': 0, 'containers': [{
            'image': resume.IMAGE, 'args': ['--mode', 'auto', '--market', 'US'],
            'env': [{'name': k, 'value': v} for k, v in resume.recovery.SETTINGS.items()]}]}}}}}}


class ResumeTests(unittest.TestCase):
    def run_case(self, rows=None, log_time='2026-10-07T02:20:50Z', config=None):
        self.calls = []
        own = resume.JOB + '-new01'
        def gc(*args):
            self.calls.append(args)
            if args[:4] == ('run', 'jobs', 'executions', 'describe'):
                return execution()
            if args[:2] == ('logging', 'read'):
                return [{'timestamp': log_time, 'textPayload':
                    'Terminating task because it has reached the maximum timeout of 3600 seconds.'}]
            if args[:3] == ('run', 'jobs', 'describe'):
                return config or job()
            if args[:3] == ('run', 'jobs', 'execute'):
                return execution(own, 'Unknown')
            if args[:4] == ('run', 'jobs', 'executions', 'cancel'):
                return {}
            raise AssertionError(args)
        rows = rows or [[execution()], [execution()], [execution(own, 'Unknown')]]
        with patch.object(resume.recovery, 'gc', side_effect=gc), \
             patch.object(resume.recovery, 'executions', side_effect=rows), \
             contextlib.redirect_stdout(io.StringIO()):
            resume.main()

    def assert_no_start(self):
        self.assertFalse(any(c[:3] == ('run', 'jobs', 'execute') for c in self.calls))

    def test_timeout_resumes_once_without_config_changes(self):
        self.run_case()
        launches = [c for c in self.calls if c[:3] == ('run', 'jobs', 'execute')]
        self.assertEqual(launches, [('run', 'jobs', 'execute', resume.JOB, '--async')])
        self.assertFalse(any('update' in c or 'builds' in c for c in self.calls))

    def test_old_first_attempt_timeout_is_insufficient(self):
        with self.assertRaisesRegex(RuntimeError, 'FINAL_TIMEOUT_NOT_CONFIRMED'):
            self.run_case(log_time='2026-10-07T01:18:05Z')
        self.assert_no_start()

    def test_active_or_newer_execution_prevents_repeat(self):
        for rows in ([execution(status='Unknown')], [execution(resume.JOB + '-other')], []):
            self.run_case(rows=[rows])
            self.assert_no_start()

    def test_scheduler_start_before_dispatch_prevents_launch(self):
        self.run_case(rows=[[execution()], [execution(resume.JOB + '-scheduled', 'Unknown')]])
        self.assert_no_start()

    def test_wrong_runtime_or_image_stops(self):
        for field, value in [('timeoutSeconds', 3600), ('maxRetries', 1)]:
            config = job()
            config['spec']['template']['spec']['template']['spec'][field] = value
            with self.assertRaisesRegex(RuntimeError, 'EXPECTED_US_DEPLOYMENT_REQUIRED'):
                self.run_case(config=config)
            self.assert_no_start()
        config = job()
        config['spec']['template']['spec']['template']['spec']['containers'][0]['image'] = 'wrong'
        with self.assertRaisesRegex(RuntimeError, 'EXPECTED_US_DEPLOYMENT_REQUIRED'):
            self.run_case(config=config)
        self.assert_no_start()

    def test_launch_race_cancels_only_own_execution(self):
        own = resume.JOB + '-new01'
        self.run_case(rows=[[execution()], [execution()],
                           [execution(own, 'Unknown'), execution(resume.JOB + '-scheduled', 'Unknown')]])
        cancels = [c for c in self.calls if c[:4] == ('run', 'jobs', 'executions', 'cancel')]
        self.assertEqual(cancels, [('run', 'jobs', 'executions', 'cancel', own, '--async')])


if __name__ == '__main__':
    unittest.main()

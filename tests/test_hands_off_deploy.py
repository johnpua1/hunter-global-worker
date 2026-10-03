import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]

def module(name, filename):
    spec=importlib.util.spec_from_file_location(name, ROOT/'cloudrun'/filename)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

release=module('hands_off_test','deploy-hands-off.py')
watchdog=module('watchdog_test','deploy-daily-watchdog.py')

class HandsOffDeployTests(unittest.TestCase):
    def test_release_rejects_unreviewed_runtime_changes(self):
        helper=SimpleNamespace(command=Mock(side_effect=['a'*40,'\n'.join(release.RUNTIME_FILES),'']))
        self.assertEqual(release.reviewed_release(helper),'a'*40)
        helper.command=Mock(side_effect=['a'*40,'\n'.join(release.RUNTIME_FILES|{'hunter-global/monthly.py'})])
        with self.assertRaisesRegex(RuntimeError,'UNREVIEWED'):
            release.reviewed_release(helper)

    def test_watchdog_file_is_idempotent_and_rejects_unknown_code(self):
        expected=(ROOT/'bridge/Watchdog.gs').read_text()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.assertTrue(watchdog.stage_watchdog(root,expected))
            self.assertFalse(watchdog.stage_watchdog(root,expected))
            (root/'Other.gs').write_text('function hunterDailyWatchdog() {}')
            with self.assertRaisesRegex(RuntimeError,'ELSEWHERE'):
                watchdog.stage_watchdog(root,expected)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'Watchdog.js').write_text('unknown code')
            with self.assertRaisesRegex(RuntimeError,'DIFFERS'):
                watchdog.stage_watchdog(root,expected)

    def test_scoped_gateway_patch_preserves_unrelated_content(self):
        helper=module('patch_helpers','deploy-monthly-retry.py')
        helper.LAUNCH_FUNCTIONS=('doPost','runHunterJob_')
        new_source=(ROOT/'bridge/Gateway.gs').read_text()
        new=helper.launch_functions(new_source)
        old={name:'function '+name+'(arg) {\n  return "old";\n}\n' for name in new}
        unchanged='\nfunction monthlyV2() { return "preserved"; }\n'
        old_source='\n'.join(old.values())+unchanged
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);gateway=root/'Gateway.js';gateway.write_text(old_source)
            self.assertTrue(helper.patch_launch(root,old,new))
            self.assertTrue(gateway.read_text().endswith(unchanged))
            self.assertFalse(helper.patch_launch(root,old,new))

    def test_scheduler_must_be_maintenance_only_and_myt(self):
        row={'state':'ENABLED','schedule':'0 20 * * *','timeZone':'Asia/Kuala_Lumpur',
          'httpTarget':{'uri':'https://run.googleapis.com/v1/projects/p/jobs/hunter-maintenance:run',
          'oauthToken':{'serviceAccountEmail':'hunter-scheduler@rgs-hunter-global.iam.gserviceaccount.com'}}}
        helper=SimpleNamespace(command=Mock(return_value=json.dumps([row])))
        release.verify_scheduler(helper)
        helper.command.return_value=json.dumps([row,{'state':'ENABLED','httpTarget':{'uri':'https://example/jobs/hunter-us-daily:run'}}])
        with self.assertRaisesRegex(RuntimeError,'FORBIDDEN'):
            release.verify_scheduler(helper)
        row['timeZone']='UTC';helper.command.return_value=json.dumps([row])
        with self.assertRaisesRegex(RuntimeError,'DRIFT'):
            release.verify_scheduler(helper)

    def test_deploy_does_not_launch_business_and_busy_blocks_changes(self):
        for busy in (False, True):
            events=[]
            before={}
            helper=SimpleNamespace(command=Mock())
            def idle(*a):
                events.append('idle')
                if busy: raise RuntimeError('JOB_STILL_RUNNING')
            repair=SimpleNamespace(ensure_image=lambda _:events.append('build'),wait_idle=idle,stamp=lambda _:None)
            guard=SimpleNamespace(PREVIOUS={},REGION='r',PROJECT='p',gc=Mock(return_value=before),
              validate_previous=Mock(),parts=lambda _:(None,None,{'image':'old'},{}),
              main=lambda **_:events.append('deploy'),probe=Mock())
            mutex=SimpleNamespace(verify_lock_probe=lambda _:events.append('lock_probe'),rollback=Mock())
            wd=SimpleNamespace(main=lambda:events.append('watchdog'))
            with patch.object(release,'module',side_effect=[helper,repair,guard,mutex,wd]), \
                 patch.object(release,'reviewed_release',return_value='a'*40), \
                 patch.object(release,'verify_scheduler'),patch('sys.argv',['deploy']):
                if busy:
                    with self.assertRaisesRegex(RuntimeError,'JOB_STILL_RUNNING'):release.main()
                    self.assertEqual(events,['build','idle'])
                else:
                    release.main()
                    self.assertEqual(events,['build','idle','deploy','idle','lock_probe','idle','watchdog'])
            # Only describe reaches this gc mock; no business execute request.
            guard.gc.assert_called_once_with('run','jobs','describe','hunter-maintenance')

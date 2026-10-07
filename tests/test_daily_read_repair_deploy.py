import contextlib
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).resolve().parents[1] / 'cloudrun/deploy-daily-read-repair-20261007.py'
spec = importlib.util.spec_from_file_location('daily_read_deploy',path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def execution(name=mod.OLD,state='Unknown'):
    return {'metadata':{'name':name},'status':{'conditions':[{'type':'Completed','status':state}]}}


class DeployReadRepairTests(unittest.TestCase):
    def test_build_failure_does_not_cancel_or_launch(self):
        with patch.object(mod.recovery,'executions',return_value=[execution()]), \
             patch.object(mod.deploy,'main',side_effect=RuntimeError('build failed')), \
             patch.object(mod.recovery,'cancel_old') as cancel, patch.object(mod.recovery,'gc') as gc, \
             patch.object(sys,'argv',['script','a'*40]):
            with self.assertRaisesRegex(RuntimeError,'build failed'):
                mod.main()
            cancel.assert_not_called()
            gc.assert_not_called()

    def test_repeat_or_other_active_execution_is_noop(self):
        for rows in ([execution('hunter-us-daily-new')],
                     [execution(),execution('hunter-us-daily-other')]):
            with patch.object(mod.recovery,'executions',return_value=rows), \
                 patch.object(mod.deploy,'main') as deploy, patch.object(mod.recovery,'gc') as gc, \
                 contextlib.redirect_stdout(io.StringIO()):
                mod.main()
                deploy.assert_not_called()
                gc.assert_not_called()

    def test_cancel_confirmation_precedes_single_us_launch(self):
        events=[]
        own='hunter-us-daily-new'
        rows=[[execution()],[execution()],[execution(state='False')],
              [execution(state='False')],[execution(own)]]
        def gc(*args):
            events.append('launch')
            self.assertEqual(args,('run','jobs','execute',mod.JOB,'--async'))
            return execution(own)
        with patch.object(mod.recovery,'executions',side_effect=rows), \
             patch.object(mod.deploy,'main',side_effect=lambda:events.append('deploy')), \
             patch.object(mod.recovery,'cancel_old',side_effect=lambda:events.append('cancel-confirmed')), \
             patch.object(mod.recovery,'gc',side_effect=gc), \
             patch.object(sys,'argv',['script','a'*40]), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(events,['deploy','cancel-confirmed','launch'])

    def test_completion_during_build_is_not_restarted(self):
        with patch.object(mod.recovery,'executions',side_effect=[[execution()],[execution(state='True')]]), \
             patch.object(mod.deploy,'main'), patch.object(mod.recovery,'cancel_old') as cancel, \
             patch.object(mod.recovery,'gc') as gc, patch.object(sys,'argv',['script','a'*40]), \
             contextlib.redirect_stdout(io.StringIO()):
            mod.main()
            cancel.assert_not_called()
            gc.assert_not_called()


if __name__=='__main__':
    unittest.main()

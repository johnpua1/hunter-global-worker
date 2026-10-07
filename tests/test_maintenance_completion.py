import contextlib
import datetime as dt
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'hunter-global'))
import maintenance

class MaintenanceCompletionTests(unittest.TestCase):
    def invoke(self, clock, results):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', {'HUNTER_ACTIONS_CUTOVER':'CONFIRMED', 'CLOUD_RUN_JOB':'hunter-maintenance'}))
            stack.enter_context(patch('production_guard.check_at_start'))
            for name in ('current_universe', 'seed_corporate_actions', 'refresh', 'materialize'):
                stack.enter_context(patch.object(maintenance, name))
            stack.enter_context(patch.object(maintenance, 'monthly', return_value=0))
            stack.enter_context(patch.object(maintenance, 'initialize_control', return_value={'last_completed_date':'2026-10-06'}))
            drive = stack.enter_context(patch.object(maintenance, 'Drive')).return_value
            drive.json.return_value = {'updated_at_myt':dt.datetime.now(maintenance.TZ).isoformat()}
            # Patch only the clock interface, not logging's shared time module.
            stack.enter_context(patch.object(maintenance, 'time', Mock(monotonic=Mock(side_effect=clock))))
            repair = stack.enter_context(patch.object(maintenance, 'run_repair', side_effect=results))
            try:
                maintenance.main()
            finally:
                self.calls = repair.call_args_list

    def test_prework_timeout_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'US:NOT_RUN,HK:NOT_RUN'):
            self.invoke([0,3200,3200], [])
        self.assertEqual(self.calls, [])

    def test_us_timeout_still_reports_unrun_hk(self):
        with self.assertRaisesRegex(RuntimeError, 'US:INCOMPLETE,HK:NOT_RUN'):
            self.invoke([0,100,3200], [{'open':2,'timed_out':True,'processed':25}])
        self.assertEqual(len(self.calls), 1)

    def test_open_queue_is_not_complete_even_without_timeout_flag(self):
        with self.assertRaisesRegex(RuntimeError, 'US:INCOMPLETE'):
            self.invoke([0,100,200], [{'open':1,'timed_out':False}, {'open':0,'timed_out':False}])
        self.assertEqual(len(self.calls), 2)

    def test_both_queues_drained_succeeds(self):
        self.invoke([0,100,200], [{'open':0,'timed_out':False}] * 2)
        self.assertEqual([c.args[1] for c in self.calls], ['US','HK'])

    def test_transport_failure_propagates(self):
        with self.assertRaisesRegex(RuntimeError, 'BRIDGE_FAILED'):
            self.invoke([0,100], [RuntimeError('BRIDGE_FAILED')])

    def test_us_receives_bounded_share_and_hk_keeps_remaining_time(self):
        with self.assertRaisesRegex(RuntimeError, 'US:INCOMPLETE'):
            self.invoke([0,100,1700], [{'open':10,'timed_out':True}, {'open':0,'timed_out':False}])
        self.assertEqual(self.calls[0].kwargs['deadline'], 1700)
        self.assertEqual(self.calls[1].kwargs['deadline'], 3300)

    def test_quick_us_returns_unused_time_to_hk(self):
        self.invoke([0,100,200], [{'open':0,'timed_out':False}] * 2)
        self.assertEqual(self.calls[1].kwargs['deadline'], 3300)

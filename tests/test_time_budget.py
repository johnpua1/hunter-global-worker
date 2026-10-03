import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hunter-global'))
from time_budget import BudgetExceeded, budget, request_timeout, retry_sleep
from runner import fetch_security, retry_http, compact, now_myt
from repair import run_repair
import test_maintenance_resume


class BudgetTests(unittest.TestCase):
    def test_nested_budget_cannot_extend_parent_and_resets(self):
        with patch('time_budget.time.monotonic', return_value=100):
            with budget(130):
                self.assertEqual(request_timeout(120), 10)
                with budget(160):
                    self.assertEqual(request_timeout(120), 10)
                with self.assertRaises(BudgetExceeded):
                    retry_sleep(30)
            self.assertEqual(request_timeout(120), 120)

    def test_expired_budget_never_sends_http(self):
        session = Mock()
        with patch('time_budget.time.monotonic', return_value=0):
            with budget(10):
                with patch('time_budget.time.monotonic', return_value=10):
                    with self.assertRaises(BudgetExceeded):
                        retry_http(session, 'GET', 'https://example.invalid')
        session.request.assert_not_called()

    def test_budget_is_not_fetch_failed(self):
        with patch('runner.yahoo_chart', side_effect=BudgetExceeded('MAINTENANCE_TIME_BUDGET')):
            with self.assertRaises(BudgetExceeded):
                fetch_security({'security_id':'US-1','market':'US','ticker':'X'}, ['2026-10-02'], '2026-10-02')

    def test_mid_chunk_budget_flushes_completed_rows_without_consuming_others(self):
        drive = test_maintenance_resume.MaintenanceResumeTests().drive()
        doc = drive.json('REPAIR_QUEUE.json')
        doc['items'].append(dict(doc['items'][0], security_id='US-000002'))
        drive.data['REPAIR_QUEUE.json'] = compact(doc)
        clock = [0]
        answer = {'result':'RESOLVED','accepted':True,'verified_at_myt':now_myt()}
        from repair import append_repair_patch
        def append(*args):
            result = append_repair_patch(*args)
            clock[0] = 701  # Work deadline=700, queue save deadline=1000.
            return result
        with patch('repair.current_universe', return_value=[{'security_id':x['security_id']} for x in doc['items']]), \
             patch('repair.decide', return_value=answer), \
             patch('repair.time.monotonic', side_effect=lambda: clock[0]), \
             patch('repair.append_repair_patch', side_effect=append):
            result = run_repair(drive, 'US', deadline=1000)
        items = drive.json('REPAIR_QUEUE.json')['items']
        self.assertEqual(items[0]['status'], 'RESOLVED')
        self.assertEqual(items[0]['repair_attempts'], 1)
        self.assertEqual(items[1], doc['items'][1])
        self.assertEqual(result['processed'], 1)
        self.assertTrue(result['timed_out'])

    def test_worker_budget_exception_leaves_item_unchanged(self):
        drive = test_maintenance_resume.MaintenanceResumeTests().drive()
        original = drive.data['REPAIR_QUEUE.json']
        with patch('repair.current_universe', return_value=[{'security_id':'US-000001'}]), \
             patch('repair.decide', side_effect=BudgetExceeded('MAINTENANCE_TIME_BUDGET')):
            result = run_repair(drive, 'US')
        self.assertEqual(result['processed'], 0)
        self.assertTrue(result['timed_out'])
        self.assertEqual(drive.data['REPAIR_QUEUE.json'], original)

class OptionBudgetTests(unittest.TestCase):
    def test_thread_budget_propagates_and_partial_options_are_not_published(self):
        import options
        drive = Mock()
        drive.file.return_value = None
        with patch('options.current_universe',return_value=[{'ticker':'X','security_id':'US-1'}]), \
             patch('options.source_preflight',return_value={'available':True}), \
             patch('options.label',side_effect=lambda *a: request_timeout(120)) as label:
            with patch('time_budget.time.monotonic',return_value=0):
                with budget(10):
                    with patch('time_budget.time.monotonic',return_value=11):
                        with self.assertRaises(BudgetExceeded):
                            options.monthly(drive,'US')
        drive.put.assert_not_called()

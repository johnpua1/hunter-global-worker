import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hk_rank_closeout', ROOT / 'cloudrun/hk_rank_closeout.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class HKRankCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ), patch.object(sys, 'argv', ['test', str(ROOT), 'a' * 40]), \
             patch.object(pathlib.Path, 'home', return_value=pathlib.Path(self.temp.name)), patch('fcntl.flock'):
            self.m = mod.controller(ROOT)
        self.addCleanup(self.m.LOCK.close)

    def prepared(self, cp=None):
        m = self.m
        m.r.gc = Mock(return_value={'job': 'HK'})
        m.r.verify = Mock()
        m.r.checkpoint = Mock(return_value=cp or {'market': 'HK', 'last_completed_date': '2026-10-07'})
        m.pack_deployment.configuration = Mock(return_value='same')
        m.pack_deployment.desired = Mock(return_value={'job': 'HK'})
        m.preflight_write_recovery = Mock()
        m.emit = Mock()
        return m

    def test_scope_only_hk_but_us_conflict_still_checked(self):
        m = self.m
        self.assertEqual(m.GOAL, {'HK': '2026-10-08'})
        self.assertEqual(m.WORKER_SHA, mod.WORKER)
        m.r.executions = Mock(return_value=[])
        self.assertEqual(m.peer_active('HK'), [])
        m.r.executions.assert_called_once_with('US')

    def test_prepare_reuses_october7_receipt_without_build_or_data_scan(self):
        m = self.prepared()
        mod.prepare(m)
        m.r.gc.assert_called_once_with('run', 'jobs', 'describe', 'hunter-hk-daily')
        m.r.verify.assert_called_once_with({'job': 'HK'}, {'job': 'HK'}, 'HK', mod.WORKER)
        m.preflight_write_recovery.assert_called_once_with()

    def test_refuses_regressed_rank_checkpoint(self):
        m = self.prepared({'market': 'HK', 'last_completed_date': '2026-10-06'})
        with self.assertRaisesRegex(RuntimeError, 'OCTOBER7_RANK_COMMIT_REQUIRED'):
            mod.prepare(m)
        m.preflight_write_recovery.assert_not_called()

    def test_rejects_non_acceleration_template_before_dispatch(self):
        m = self.prepared()
        m.r.verify.side_effect = RuntimeError('WRONG_IMAGE')
        with self.assertRaisesRegex(RuntimeError, 'WRONG_IMAGE'):
            mod.prepare(m)
        m.preflight_write_recovery.assert_not_called()

    def test_accepts_only_after_phase2_final_proof_and_no_active_hk(self):
        m = self.prepared()
        m.step = Mock(return_value=mod.TARGET)
        m.full_proof = Mock()
        m.r.executions = Mock(return_value=[])
        final = {'market': 'HK', 'last_completed_date': mod.TARGET,
                 'phase2_completed_date': mod.TARGET, 'phase2_completed_at_myt': '2026-10-09T01:00:00+08:00'}
        m.r.checkpoint.side_effect = [{'market': 'HK', 'last_completed_date': '2026-10-07'}, final]
        with patch.object(m.time, 'monotonic', side_effect=[0, 1]):
            mod.run(m)
        m.step.assert_called_once_with('HK', {'job': 'HK'}, {})
        m.full_proof.assert_called_once_with({'job': 'HK'}, 'HK', mod.TARGET)
        self.assertIn('HK_PHASE2_ACCEPTED', m.emit.call_args.args[0])

    def test_failed_final_proof_cannot_publish_acceptance(self):
        m = self.prepared()
        m.step = Mock(return_value=mod.TARGET)
        m.full_proof = Mock(side_effect=RuntimeError('INCOMPLETE'))
        with patch.object(m.time, 'monotonic', side_effect=[0, 1]):
            with self.assertRaisesRegex(RuntimeError, 'INCOMPLETE'):
                mod.run(m)
        self.assertFalse(any('HK_PHASE2_ACCEPTED' in c.args[0] for c in m.emit.call_args_list))


if __name__ == '__main__':
    unittest.main()

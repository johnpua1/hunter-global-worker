"""Exercise the shipped controller without cloud access or starting its main loop."""
import ast
import contextlib
import io
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_resumable_release import target
from test_us_phase2_deploy import execution


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX_SHA = 'c' * 40


def load_controller(home):
    shell = (ROOT / 'cloudrun/phase2-closeout-20261008.sh').read_text()
    source = shell.split("<<'PHASE2_PY'", 1)[1].split('\n', 1)[1].rsplit('\nPHASE2_PY', 1)[0]
    tree = ast.parse(source)
    final = tree.body.pop()
    assert isinstance(final, ast.Try) and final.body[0].value.func.id == 'main'
    mod = types.ModuleType('closeout_under_test')
    with patch.object(sys, 'argv', ['controller', str(ROOT), FIX_SHA]), \
         patch.object(pathlib.Path, 'home', return_value=pathlib.Path(home)), \
         patch('fcntl.flock'):
        exec(compile(tree, str(ROOT / 'cloudrun/phase2-closeout-20261008.sh'), 'exec'), mod.__dict__)
    return mod


class CloseoutHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mod = load_controller(self.temp.name)

    def tearDown(self):
        self.mod.LOCK.close()
        self.temp.cleanup()

    def config(self, market):
        doc = target(market)
        c = self.mod.r.deployment.container(doc, market)
        sha = self.mod.EXPECTED_SHA[market]
        c['image'] = self.mod.r.base.BASE_IMAGE + sha
        env = {e['name']: e for e in c['env']}
        for key, value in {'HUNTER_SOURCE_SHA': sha, 'HUNTER_READ_CHECKPOINTS': '1'}.items():
            env[key] = {'name': key, 'value': value}
        c['env'] = list(env.values())
        return doc

    def completed(self, market):
        return {'market': market, 'last_completed_date': '2026-10-07',
                'phase2_completed_date': '2026-10-07',
                'phase2_completed_at_myt': '2026-10-08T16:00:00+08:00'}

    def test_exact_observed_failure_gets_one_permit_other_errors_stay_fatal(self):
        mod = self.mod
        name = 'hunter-hk-daily-cc7hv'
        def fatal(message):
            return [{'textPayload': 'INFO previous line\nERROR hunter halted: ' + message}]
        with patch.object(mod.r, 'gc', return_value=fatal('BRIDGE_Service error: Drive')), \
             patch.object(mod.r, 'timeout_failure', return_value=False):
            self.assertEqual(mod.recoverable_failure(name, 'HK'), 'HK_PATCHED_READ_CACHE_FAILURE_ONE_RESUME')
            self.assertIsNone(mod.recoverable_failure(name, 'US'))
            self.assertIsNone(mod.recoverable_failure('hunter-hk-daily-unseen', 'HK'))
            mod.OBSERVED_RECOVERY_USED.add(name)
            self.assertIsNone(mod.recoverable_failure(name, 'HK'))
        mod.OBSERVED_RECOVERY_USED.clear()
        with patch.object(mod.r, 'gc', return_value=fatal('BRIDGE_STALE_WRITE')):
            self.assertIsNone(mod.recoverable_failure(name, 'HK'))

    def test_running_us_and_waiting_hk_do_not_launch_or_read_checkpoints(self):
        mod = self.mod
        live = execution('hunter-us-daily-xc6qn', 'Unknown')
        with patch.object(mod.r, 'executions', side_effect=lambda m: [live] if m == 'US' else []), \
             patch.object(mod.r, 'checkpoint') as checkpoint, \
             patch.object(mod.r, 'gc') as gc, patch.object(mod, 'progress'), \
             contextlib.redirect_stdout(io.StringIO()):
            mod.step('US', self.config('US'), {})
            mod.step('HK', self.config('HK'), {})
        checkpoint.assert_not_called()
        gc.assert_not_called()

    def test_hk_resumes_only_phase2_after_mixed_version_verification(self):
        mod = self.mod
        doc = self.config('HK')
        failed = execution('hunter-hk-daily-cc7hv')
        own = execution('hunter-hk-daily-new', 'Unknown')
        cp = {'market': 'HK', 'last_completed_date': '2026-10-07', 'phase2_completed_date': '2026-10-06'}
        calls = []
        def gc(*args):
            calls.append(args)
            if args[:3] == ('run', 'jobs', 'describe'):
                return doc
            if args[:3] == ('run', 'jobs', 'execute'):
                return own
            raise AssertionError(args)
        reader = contextlib.nullcontext(Mock())
        with patch.object(mod.r, 'executions', side_effect=[[failed], [failed], [own]]), \
             patch.object(mod, 'peer_active', return_value=[]), \
             patch.object(mod.r, 'checkpoint', return_value=cp), \
             patch.object(mod, 'recoverable_failure', return_value='HK_PATCHED_READ_CACHE_FAILURE_ONE_RESUME'), \
             patch.object(mod, 'reader', return_value=reader), \
             patch.object(mod, 'validate_phase2_resume', return_value=cp), \
             patch.object(mod.r, 'gc', side_effect=gc), contextlib.redirect_stdout(io.StringIO()):
            mod.step('HK', doc, {})
        self.assertEqual(calls[-1], ('run', 'jobs', 'execute', 'hunter-hk-daily',
                         '--args=--mode,auto,--market,HK,--phase2-only,--as-of,2026-10-07', '--async'))
        self.assertIn('hunter-hk-daily-cc7hv', mod.OBSERVED_RECOVERY_USED)

    def run_main(self, proof_error=None):
        mod = self.mod
        docs = {m: self.config(m) for m in ('US', 'HK')}
        with patch.object(mod.hk_deployment, 'deploy_hk', return_value={'status': 'VERIFIED', 'docs': docs}), \
             patch.object(mod.r, 'executions', return_value=[]), \
             patch.object(mod.r, 'checkpoint', side_effect=lambda doc, m: self.completed(m)), \
             patch.object(mod, 'full_proof', side_effect=proof_error) as proof, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            if proof_error:
                with self.assertRaisesRegex(RuntimeError, 'FINAL_DAILY_RANK_PHASE2_MISMATCH'):
                    mod.main()
            else:
                mod.main()
        return proof, output.getvalue()

    def test_main_accepts_mixed_images_only_after_both_full_proofs(self):
        proof, output = self.run_main()
        self.assertEqual([c.args[1] for c in proof.call_args_list], ['US', 'HK'])
        self.assertIn('US_AND_HK_PHASE2_ACCEPTED_ASOF_2026-10-07', output)

    def test_failed_full_proof_never_prints_acceptance(self):
        _, output = self.run_main(RuntimeError('FINAL_DAILY_RANK_PHASE2_MISMATCH'))
        self.assertNotIn('US_AND_HK_PHASE2_ACCEPTED', output)


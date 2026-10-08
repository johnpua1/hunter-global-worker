import contextlib
import copy
import importlib.util
import io
import pathlib
import types
import unittest
from unittest.mock import Mock, patch
from test_hk_read_cache_recovery_deploy import document
from test_us_phase2_deploy import execution
import test_large_read_deploy as existing

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('continuation_deploy', ROOT / 'cloudrun/deploy-continuation-20261008.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
SHA = 'a' * 40


class ContinuationDeployTests(unittest.TestCase):
    def setUp(self):
        self.docs = {m: document(m, mod.EXISTING[m]) for m in ('US', 'HK')}
        self.calls = []
        self.rows = {m: [] for m in self.docs}
        self.on_update = lambda market: None
        base = existing.mod.base
        self.r = types.SimpleNamespace(gc=self.gc, executions=lambda m:self.rows[m],
            deployment=existing.mod.deployment, verify=existing.mod.verify,
            base=types.SimpleNamespace(ROOT=ROOT, BASE_IMAGE=mod.BASE_IMAGE,
                configuration=base.configuration, recovery=base.recovery,
                deploy=base.deploy, build=Mock()))
        pin = patch.object(mod.subprocess, 'check_output', return_value=SHA+'\n')
        pin.start(); self.addCleanup(pin.stop)

    def gc(self, *args):
        self.calls.append(args)
        if args[:3] == ('run','jobs','executions'):
            market = args[4].split('-')[1].upper()
            if args[3] == 'cancel':
                self.assertTrue(all(mod.configuration(self.r, d) == mod.configuration(self.r, mod.desired(d,self.r,m,SHA)) for m,d in self.docs.items()))
                return {}
            if args[3] == 'describe':
                return document(market, mod.EXISTING[market])
            raise AssertionError(args)
        market = args[3].split('-')[1].upper()
        if args[:3] == ('run','jobs','update'):
            self.assertEqual(args[4], '--image='+mod.BASE_IMAGE+SHA)
            self.assertIn('HUNTER_CONTINUE_ONLY=1', args[5])
            self.docs[market] = mod.desired(self.docs[market],self.r,market,SHA)
            self.on_update(market)
        elif args[:3] != ('run','jobs','describe'):
            raise AssertionError(args)
        return copy.deepcopy(self.docs[market])

    def deploy(self):
        with contextlib.redirect_stdout(io.StringIO()): return mod.deploy_both(self.r,SHA)

    def test_both_templates_same_version_before_old_executions_cancel_no_launch(self):
        for m in self.rows: self.rows[m] = [execution('hunter-'+m.lower()+'-daily-old','Unknown')]
        result = self.deploy()
        self.assertEqual(len(result['cancelled']),2)
        self.assertEqual([c[3] for c in self.calls if c[:3] == ('run','jobs','update')], ['hunter-us-daily','hunter-hk-daily'])
        self.assertFalse(any('execute' in c for c in self.calls))
        self.r.base.build.assert_called_once_with(SHA)

    def test_build_failure_never_changes_or_cancels_any_job(self):
        self.r.base.build.side_effect = RuntimeError('BUILD_FAILED')
        with self.assertRaisesRegex(RuntimeError,'BUILD_FAILED'): self.deploy()
        self.assertFalse(any('update' in c or 'cancel' in c for c in self.calls))

    def test_partial_update_failure_does_not_cancel_or_launch_or_roll_back(self):
        def fail(market):
            if market == 'HK': raise RuntimeError('UPDATE_UNCERTAIN')
        self.on_update = fail
        with self.assertRaisesRegex(RuntimeError,'UPDATE_UNCERTAIN'): self.deploy()
        self.assertFalse(any('cancel' in c or 'execute' in c for c in self.calls))
        updates = [c for c in self.calls if 'update' in c]
        self.assertTrue(all(c[4].endswith(SHA) for c in updates))

    def test_resume_already_deployed_does_not_rebuild(self):
        self.docs = {m:mod.desired(d,self.r,m,SHA) for m,d in self.docs.items()}
        self.deploy(); self.r.base.build.assert_not_called()
        self.assertFalse(any('update' in c for c in self.calls))


if __name__ == '__main__': unittest.main()

import importlib.util
import pathlib
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bridge_release_check',ROOT/'cloudrun/bridge-release-check-20261008.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class BridgeReleaseCheckTests(unittest.TestCase):
    def test_exact_bound_deployment_required_not_newest_other_url(self):
        listing='- actual @24 description\n- other @99 newer\n'
        self.assertEqual(mod.deployment_version(listing,'actual'),'24')
        with self.assertRaisesRegex(RuntimeError,'BOUND_DEPLOYMENT_NOT_IN_SELECTED_SCRIPT'):
            mod.deployment_version(listing,'missing')

    def test_readback_rejects_old_source_and_accepts_clasp_js_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/'Gateway.js'
            path.write_text('old gateway')
            with self.assertRaisesRegex(RuntimeError,'SOURCE_MISMATCH'):mod.verify_source(tmp)
            path.write_text((ROOT/'bridge/Gateway.gs').read_text())
            mod.verify_source(tmp)
            (pathlib.Path(tmp)/'Gateway.gs').write_text(path.read_text())
            with self.assertRaisesRegex(RuntimeError,'SOURCE_MISMATCH'):mod.verify_source(tmp)

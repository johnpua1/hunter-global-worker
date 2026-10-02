import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "cloudrun" / "normalize-clasp-dir.py"
spec = importlib.util.spec_from_file_location("clasp_normalizer", HELPER)
normalizer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(normalizer)


class ClaspNormalizerTests(unittest.TestCase):
    def test_normalize_keeps_one_extension_per_basename(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "Gateway.js").write_text("old", encoding="utf-8")
            (root / "Gateway.gs").write_text("new", encoding="utf-8")
            (root / "Code.js").write_text("code", encoding="utf-8")
            (root / "Other.ts").write_text("ts", encoding="utf-8")
            (root / "Other.js").write_text("js", encoding="utf-8")

            removed = normalizer.normalize(root)
            normalizer.assert_unique(root)

            self.assertTrue((root / "Gateway.gs").exists())
            self.assertFalse((root / "Gateway.js").exists())
            self.assertTrue((root / "Code.js").exists())
            self.assertTrue((root / "Other.js").exists())
            self.assertFalse((root / "Other.ts").exists())
            self.assertIn(("Gateway.js", "Gateway.gs"), removed)

    def test_deploy_resolves_and_updates_versioned_deployment(self):
        script = (ROOT / "cloudrun" / "deploy-monthly-full.sh").read_text(encoding="utf-8")
        self.assertNotIn('list-deployments | grep', script)
        self.assertNotIn('create-deployment --deploymentId', script)
        self.assertIn('resolve-clasp-deployment.py', script)
        self.assertIn('update-deployment "$DEPLOYMENT_ID"', script)

    def test_deploy_removes_clone_gateway_before_copy_and_checks_before_push(self):
        script = (ROOT / "cloudrun" / "deploy-monthly-full.sh").read_text(encoding="utf-8")
        rm_pos = script.index('rm -f "$SCRIPT_DIR/Gateway.js" "$SCRIPT_DIR/Gateway.ts" "$SCRIPT_DIR/Gateway.gs"')
        copy_pos = script.index('cp "$ROOT/bridge/Gateway.gs" "$SCRIPT_DIR/Gateway.gs"')
        check_pos = script.index('python "$ROOT/cloudrun/normalize-clasp-dir.py" "$SCRIPT_DIR"\n')
        push_pos = script.index('npx -y "$CLASP" push --force')
        self.assertLess(rm_pos, copy_pos)
        self.assertLess(copy_pos, check_pos)
        self.assertLess(check_pos, push_pos)


if __name__ == "__main__":
    unittest.main()

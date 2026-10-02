import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "cloudrun" / "resolve-clasp-deployment.py"
spec = importlib.util.spec_from_file_location("clasp_deployment_resolver", HELPER)
resolver = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(resolver)


class ClaspDeploymentResolverTests(unittest.TestCase):
    SAMPLE = """Found 3 deployments.
- DEV123 @HEAD
- PROD5 @5
- PROD7 @7
"""

    def test_prefers_existing_secret_id_when_versioned(self):
        self.assertEqual(resolver.resolve(self.SAMPLE, "PROD5"), "PROD5")

    def test_ignores_head_and_uses_highest_version_when_secret_is_stale(self):
        self.assertEqual(resolver.resolve(self.SAMPLE, "STALE"), "PROD7")

    def test_rejects_head_only(self):
        with self.assertRaisesRegex(ValueError, "NO_VERSIONED_CLASP_DEPLOYMENT"):
            resolver.resolve("Found 1 deployments.\n- DEV123 @HEAD\n", "DEV123")


if __name__ == "__main__":
    unittest.main()

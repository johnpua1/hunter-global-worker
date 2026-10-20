"""Phase 2 publication uses Bridge SHA acknowledgment, not duplicate large reads."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))

import execution_budget as budget
import phase2_resume as journal
import runner
from test_incremental_inputs import Store, open_cache


class Phase2PublishBudgetTests(unittest.TestCase):
    def tearDown(self):
        budget._started = None

    def fixture(self, market="US"):
        store = Store()
        date = "2026-10-19"
        cp = market + "/CONTROL/DAILY_CHECKPOINT.json"
        store.data[cp] = runner.compact({"market": market, "last_completed_date": date})
        store.data[market + "/CURRENT_UNIVERSE.json"] = b'{"securities":[]}'
        store.data[market + "/PHASE2/SECTOR_MAP.json"] = b'{"rows":[]}'
        outputs = []
        for name in ("EARNINGS_HISTORY", "EARNINGS_CALENDAR"):
            path = market + "/PHASE2/" + name + ".json"
            store.data[path] = runner.compact({"market": market, "events": ["previous"]})
            new = runner.compact({"market": market, "events": ["latest"]})
            outputs.append((path, new, runner.digest(store.data[path])))
        return store, cp, outputs

    def test_normal_publish_never_downloads_current_business_outputs(self):
        for market in ("US", "HK"):
            with self.subTest(market=market):
                store, cp, outputs = self.fixture(market)
                store.put_fast = Mock(wraps=store.put)
                _, drive = open_cache(store, market)
                original_read = store.read
                forbidden = {row[0] for row in outputs}

                def guarded_read(path):
                    if path in forbidden:
                        raise AssertionError("redundant multi-MB business output readback")
                    return original_read(path)

                with patch.object(store, "read", side_effect=guarded_read):
                    result = journal.stage_and_publish(drive, market, outputs,
                                                       {"market": market})
                self.assertEqual(result["market"], market)
                for path, payload, _ in outputs:
                    self.assertEqual(store.data[path], payload)
                self.assertTrue(forbidden.issubset({
                    call.args[0] for call in store.put_fast.call_args_list
                }))
                # Only the owner of finish_phase2_daily may commit the receipt.
                self.assertNotIn("phase2_completed_date", json.loads(store.data[cp]))

    def test_optimistic_cas_refuses_concurrently_changed_output(self):
        store, cp, outputs = self.fixture()
        store.put_fast = Mock(wraps=store.put)
        _, drive = open_cache(store)
        with patch.object(journal, "publish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                journal.stage_and_publish(drive, "US", outputs, {"market": "US"})
        store.data[outputs[0][0]] = b'{"different":"writer"}'
        store.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "PHASE2_RESUME_OUTPUT_CHANGED"):
            journal.resume(drive, "US")
        self.assertEqual(store.writes, [])
        self.assertNotIn("phase2_completed_date", json.loads(store.data[cp]))

    def test_stale_after_previous_commit_requires_exact_byte_confirmation(self):
        store, cp, outputs = self.fixture()
        store.put_fast = Mock(wraps=store.put)
        _, drive = open_cache(store)
        with patch.object(journal, "publish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                journal.stage_and_publish(drive, "US", outputs, {"market": "US"})
        store.data[outputs[0][0]] = outputs[0][1]  # previous write succeeded
        store.writes.clear()
        result = journal.resume(drive, "US")
        self.assertEqual(result["market"], "US")
        self.assertEqual(store.writes, [outputs[1][0]])
        self.assertEqual(store.data[outputs[1][0]], outputs[1][1])

    def test_publication_uses_reserved_budget_after_normal_work_cutoff(self):
        store, cp, outputs = self.fixture()
        existing = store.put
        def checked_fast(*args, **kwargs):
            self.assertEqual(budget.timeout(15), 15)
            return existing(*args, **kwargs)
        store.put_fast = checked_fast
        _, drive = open_cache(store)
        with patch.object(budget, "_started", 100), \
             patch.object(budget.time, "monotonic", return_value=6101):
            self.assertEqual(journal.stage_and_publish(
                drive, "US", outputs, {"market": "US"})["market"], "US")

    def test_content_addressed_staging_avoids_recursive_durable_read_cache(self):
        original = b"A" * 310000
        p = "US/CONTROL/PHASE2_RESUME/blob-" + runner.digest(original) + ".gz"
        drive = runner.Drive.__new__(runner.Drive)
        drive.file = Mock(return_value={"size": len(original), "revision": "1:2"})
        def chunk_call(op, **kw):
            self.assertEqual(op, "read_chunk")
            offset, length = kw["offset"], kw["length"]
            payload = original[offset:offset + length]
            return {"offset": offset, "size": len(original), "length": len(payload),
                    "sha256": runner.digest(payload),
                    "data_base64": base64.b64encode(payload).decode(),
                    "eof": offset + len(payload) == len(original)}
        drive._call = Mock(side_effect=chunk_call)
        with patch.dict(os.environ, {"HUNTER_READ_CHECKPOINTS": "1"}), \
             patch("durable_reads.read_large", side_effect=AssertionError("recursive cache")):
            self.assertEqual(drive.read(p), original)
        self.assertGreaterEqual(drive._call.call_count, 1)


if __name__ == "__main__":
    unittest.main()

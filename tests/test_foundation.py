import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hunter-global"))
from analytics import compose, excursions, indicators, split_adjust
from foundation import append_daily_date, read_existing, run_daily
from options import current_status, label
from repair import decide
from repair import run_repair
from market_calendar import materialize
from derived import read_files
from runner import compact, lines_gz, parse_lines_gz


class MemoryDrive:
    def __init__(self):
        self.data = {"REPAIR_QUEUE.json": compact({"items": []})}
        self.writes = []

    def read(self, path):
        return self.data[path]

    def json(self, path):
        return json.loads(self.read(path))

    def file(self, path):
        return path in self.data

    def list(self, folder):
        prefix = folder + "/"
        return [{"name": path[len(prefix):]} for path in self.data if path.startswith(prefix)]

    def put(self, path, data, **kwargs):
        self.data[path] = data
        self.writes.append(path)

    def put_fast(self, path, data, **kwargs):
        self.put(path, data, **kwargs)

    def append(self, path, data, mime=None):
        if path in self.data:
            raise RuntimeError("APPEND_CONFLICT")
        self.data[path] = data
        self.writes.append(path)


class LegacyBridgeDrive(MemoryDrive):
    def append(self, path, data, mime=None):
        if "/DAILY/" in path and path.endswith(".ndjson.gz"):
            raise RuntimeError("BRIDGE_DAILY_PAYLOAD_INVALID")
        return super().append(path, data, mime)


def row(date, close, sid="US-000001", anchor="2026-01-01"):
    return {"security_id": sid, "date": date, "trade_date": date,
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": 100, "adjustment_as_of": anchor}


class FoundationTests(unittest.TestCase):
    def test_hk_identity_fix_requires_official_isin_proof(self):
        item = {"market": "HK", "security_id": "HK-000001", "category": "IDENTITY_REVIEW",
                "problem": "OFFICIAL_ISIN_SAME_NEW_TICKER"}
        security = {"ticker": "0002.HK", "exchange": "HKEX", "isin": "HK123",
                    "identity_proof": {"kind": "HK_ISIN_MATCH", "isin": "HK123",
                                       "old_ticker": "0001.HK", "new_ticker": "0002.HK",
                                       "source_hash": "abc"}}
        self.assertEqual(decide(MemoryDrive(), item, security)["result"], "IDENTITY_FIXED")
        security["identity_proof"]["isin"] = "DIFFERENT"
        # Without matching proof the worker cannot assert identity.
        answer = decide(MemoryDrive(), item, security)
        self.assertEqual(answer["result"], "UNRESOLVED")
        self.assertFalse(answer["accepted"])

    def test_options_status_stales_after_missed_monthly_refresh(self):
        self.assertEqual(current_status({"status": "TRUE", "checked_at_myt": "2026-08-01T09:00:00+08:00"},
                                        "2026-09"), "STALE")

    @patch("options.retry_http")
    def test_options_requires_two_quoted_distinct_strikes_same_expiry(self, get):
        chain = {"expirationDate": 1790294400, "calls": [
            {"strike": 100, "bid": 2.0, "ask": 2.2},
            {"strike": 105, "bid": 1.0, "ask": 1.2}]}
        get.return_value.json.return_value = {"optionChain": {"result": [{
            "expirationDates": [1790294400], "options": [chain]}]}}
        full = label("AAPL", "US")
        self.assertEqual(full["vertical_usable"], "TRUE")
        self.assertEqual(len(full["vertical_evidence"]["legs"]), 2)
        chain["calls"][1].pop("ask")
        partial = label("AAPL", "US")
        self.assertEqual(partial["has_options"], "TRUE")
        self.assertEqual(partial["vertical_usable"], "UNKNOWN")
    def test_patch_composition_and_split_applied_once(self):
        base = [row("2026-01-01", 100)]
        patch = [{"accepted": True, "result": "RESOLVED", "rows": [row("2026-01-01", 110)]}]
        result = compose(base, patch, [row("2026-01-02", 55, anchor="2026-01-02")])
        adjusted = split_adjust(result, [{"security_id": "US-000001",
                                           "effective_date": "2026-01-02", "factor": 2}])
        self.assertEqual([r["close"] for r in adjusted], [55, 55])
        self.assertEqual(adjusted[0]["volume"], 200)

    def test_underlying_excursions_use_future_days(self):
        rows = [row((dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(), 100 + i)
                for i in range(61)]
        result = excursions(rows, signal_date="2026-01-01")
        self.assertEqual(result["5D"]["days_to_MFE"], 5)
        self.assertAlmostEqual(result["5D"]["final_return"], .05)
        self.assertEqual(result["instrument"], "UNDERLYING")

    def test_accepted_daily_repair_wins_without_modifying_daily(self):
        bad = row("2026-01-02", 40)
        good = row("2026-01-02", 50)
        patch = [{"accepted": True, "result": "RESOLVED", "rows": [good]}]
        self.assertEqual(compose([row("2026-01-01", 100)], patch, [bad])[-1]["close"], 50)
        self.assertEqual(bad["close"], 40)

    def test_bottom_confirmation_requires_two_closes_and_ma20(self):
        rows = [row((dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(),
                    100 if i < 18 else (90 if i == 18 else 110)) for i in range(20)]
        self.assertFalse(indicators(rows)["bottom_confirmation"])
        rows.append(row("2026-01-21", 112))
        self.assertTrue(indicators(rows)["bottom_confirmation"])

    @patch("foundation.closed_dates_since", return_value=["2026-01-02"])
    @patch("foundation.fetch_security")
    def test_daily_second_run_writes_zero(self, fetch, dates):
        drive = MemoryDrive()
        security = {"market": "US", "security_id": "US-000001", "ticker": "AAPL"}
        fetch.return_value = ([row("2026-01-02", 101)], ["PASS_DAILY"], [], None)
        last, keys = {security["security_id"]: "2026-01-01"}, set()
        first = append_daily_date(drive, "US", "2026-01-02", [security], last, keys,
                                  1, ["2026-01-01"])
        before = len(drive.writes)
        second = append_daily_date(drive, "US", "2026-01-02", [security], last, keys,
                                   1, ["2026-01-01"])
        self.assertEqual(first["written"], 1)
        self.assertEqual(second["written"], 0)
        self.assertEqual(len([x for x in drive.writes[before:] if "/DAILY/" in x]), 0)
        self.assertEqual(len(parse_lines_gz(drive.data["US/DAILY/2026-01-02/part-0001.ndjson.gz"])), 1)

    @patch("foundation.fetch_security")
    def test_daily_falls_back_for_legacy_bridge_gzip_validator(self, fetch):
        drive = LegacyBridgeDrive()
        security = {"market": "US", "security_id": "US-000001", "ticker": "AAPL"}
        fetch.return_value = ([row("2026-01-02", 101)], ["PASS_DAILY"], [], None)
        result = append_daily_date(
            drive, "US", "2026-01-02", [security],
            {security["security_id"]: "2026-01-01"}, set(), 1, ["2026-01-01"])
        self.assertEqual(result["status"], "COMPLETE")
        fallback = "US/DAILY/2026-01-02/part-0001.ndjson.gzip"
        self.assertIn(fallback, drive.data)
        self.assertEqual(len(parse_lines_gz(drive.data[fallback])), 1)

    def test_read_existing_accepts_legacy_bridge_fallback_suffix(self):
        drive = MemoryDrive()
        security = {"market": "US", "security_id": "US-000001", "ticker": "AAPL"}
        path = "US/DAILY/2026-01-02/part-0001.ndjson.gzip"
        drive.data[path] = lines_gz([row("2026-01-02", 101)])
        with patch("foundation.daily_segments", return_value=["2026-01-02"]):
            last, keys = read_existing(drive, "US", [security], "2026-01-01")
        self.assertEqual(last["US-000001"], "2026-01-02")
        self.assertIn(("US-000001", "2026-01-02"), keys)

    @patch("foundation.fetch_security")
    def test_daily_no_targets_is_noop_not_market_outage(self, fetch):
        drive = MemoryDrive()
        security = {"market": "US", "security_id": "US-000001", "ticker": "AAPL",
                    "listing_status": "ACTIVE"}
        result = append_daily_date(
            drive, "US", "2026-09-25", [security],
            {security["security_id"]: "2026-09-25"}, set(), 1, ["2026-09-25"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["no_op_reason"], "NO_TARGETS")
        self.assertEqual(result["available"], 1)
        self.assertNotIn("US/CONTROL/DAILY_RUN_2026-09-25.json", drive.data)
        fetch.assert_not_called()

    @patch("foundation.fetch_security")
    def test_base_date_new_listing_bootstrap_counts_base_as_available(self, fetch):
        drive = MemoryDrive()
        existing = {"market": "US", "security_id": "US-000001", "ticker": "AAPL",
                    "listing_status": "ACTIVE"}
        new_listing = {"market": "US", "security_id": "US-000002", "ticker": "NEWX",
                       "listing_status": "ACTIVE", "security_id_origin": "NEW_LISTING"}
        fetch.return_value = ([row("2026-09-25", 50, sid="US-000002")],
                              ["PASS_DAILY"], [], None)
        last = {"US-000001": "2026-09-25", "US-000002": None}
        keys = set()

        result = append_daily_date(
            drive, "US", "2026-09-25", [existing, new_listing],
            last, keys, 1, ["2026-09-25"])

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["available"], 2)
        self.assertEqual(result["written"], 1)
        self.assertNotEqual(result["status"], "MARKET_WIDE_DATA_UNAVAILABLE")

    @patch("foundation.append_daily_date")
    @patch("foundation.closed_dates_since", return_value=[])
    @patch("foundation.read_existing")
    @patch("foundation.current_universe")
    @patch("foundation.load_market")
    def test_inactive_new_listing_does_not_force_weekend_replay(
            self, base, universe, existing, dates, append):
        drive = MemoryDrive()
        drive.data["US/CONTROL/DAILY_CHECKPOINT.json"] = compact({
            "market": "US", "last_completed_date": "2026-09-25",
            "updated_at_myt": "2026-09-27T09:12:28+08:00"})
        base.return_value = type("Base", (), {
            "checkpoint": {"as_of": "2026-09-25", "total_batches": 1,
                           "verified_batches": {"1": {}}},
            "calendar": ["2026-09-25"],
        })()
        securities = [
            {"market": "US", "security_id": "US-000001", "ticker": "AAPL",
             "listing_status": "ACTIVE"},
            {"market": "US", "security_id": "US-009999", "ticker": "NEWX",
             "listing_status": "QUARANTINED_DATA_GAP",
             "security_id_origin": "NEW_LISTING"},
        ]
        universe.return_value = securities
        existing.return_value = (
            {"US-000001": "2026-09-25", "US-009999": None}, set())
        self.assertEqual(run_daily(drive, "US", 1), [])
        append.assert_not_called()

    @patch("foundation.closed_dates_since", return_value=["2026-01-02"])
    @patch("foundation.fetch_security")
    def test_market_wide_outage_has_no_individual_repairs(self, fetch, dates):
        drive = MemoryDrive()
        securities = [{"market": "US", "security_id": f"US-{i:06d}", "ticker": f"X{i}"}
                      for i in range(10)]
        fetch.return_value = ([], ["FETCH_FAILED"], [], "YAHOO_404")
        result = append_daily_date(drive, "US", "2026-01-02", securities,
                                   {s["security_id"]: "2026-01-01" for s in securities},
                                   set(), 2, ["2026-01-01"])
        self.assertEqual(result["status"], "MARKET_WIDE_DATA_UNAVAILABLE")
        self.assertEqual(drive.json("REPAIR_QUEUE.json")["items"], [])

    @patch("repair.load_market", return_value=object())
    @patch("repair.decide")
    @patch("repair.current_universe")
    def test_repairs_drain_in_persisted_chunks_without_base_writes(self, universe, decide_repair, base):
        drive = MemoryDrive()
        universe.return_value = [{"security_id": "US-000001"}]
        drive.data["US/CALENDAR_BASE.ndjson.gz"] = lines_gz([{"date": "2026-01-02"}])
        drive.data["REPAIR_QUEUE.json"] = compact({"items": [
            {"market": "US", "security_id": "US-000001", "category": "DATA_SUSPECT",
             "batch": i, "status": "OPEN"} for i in range(52)]})
        decide_repair.return_value = {"result": "UNRESOLVED", "accepted": False,
                                      "reason": "NO_INDEPENDENT_EVIDENCE",
                                      "verified_at_myt": "2026-09-27T08:00:00+08:00"}
        result = run_repair(drive, "US", chunk_size=20)
        self.assertEqual(result["open"], 0)
        self.assertEqual(result["processed"], 52)
        self.assertEqual(drive.writes, ["REPAIR_QUEUE.json"] * 3)
        self.assertEqual(run_repair(drive, "US")["processed"], 0)

    def test_derived_reads_flat_and_sharded_repair_patches(self):
        class ShardDrive:
            def list(self, folder):
                if folder == "US/REPAIR_PATCH":
                    return [
                        {"name": "aa", "mimeType": "application/vnd.google-apps.folder"},
                        {"name": "legacy.json", "mimeType": "application/json"},
                    ]
                if folder == "US/REPAIR_PATCH/aa":
                    return [{"name": "aabb.json", "mimeType": "application/json"}]
                raise AssertionError(folder)

        self.assertEqual(
            read_files(ShardDrive(), "US", "REPAIR_PATCH", ".json"),
            ["US/REPAIR_PATCH/aa/aabb.json", "US/REPAIR_PATCH/legacy.json"],
        )

    @patch("market_calendar.closed_dates_since", return_value=[])
    @patch("market_calendar.load_market")
    def test_calendar_does_not_invent_halt_or_weekday_closure(self, base, sessions):
        drive = MemoryDrive()
        base.return_value.calendar = ["2026-09-25"]
        materialize(drive, "US", "2026-09-28")
        doc = drive.json("US/MARKET_CALENDAR/as_of_2026-09-28.json")
        self.assertEqual([x["session_status"] for x in doc["sessions"]],
                         ["OPEN", "CLOSED", "CLOSED"])
        self.assertEqual(doc["unverified_weekdays"], ["2026-09-28"])


if __name__ == "__main__":
    unittest.main()

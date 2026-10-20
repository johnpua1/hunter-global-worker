"""Offline synthetic contract tests; NOT market backtesting results."""
from __future__ import annotations

import math
from datetime import datetime,timezone,timedelta
import unittest

from v3_ablation_adapter import InvalidAblationInput,evaluate_ablation
from evaluation_adapter import evaluate


def fixture(n=130):
    start=datetime(2025,1,2,16,tzinfo=timezone.utc)
    rows=[]
    for i in range(n):
        target=start+timedelta(days=i)
        issue=target-timedelta(hours=3)
        h=math.sin(i/3)
        q=math.cos(i/5)
        direction="UP" if h+q>.3 else "DOWN" if h+q<-.3 else "FLAT"
        def evidence(source,value):
            return {"source":source,"status":"VERIFIED_ASOF",
                    "source_ref":"SYNTHETIC_TEST_ONLY",
                    "observed_at":(issue-timedelta(hours=2)).isoformat(),
                    "published_at":(issue-timedelta(hours=1)).isoformat(),
                    "price_replaces_moomoo":False,"value":value}
        rows.append({
            "symbol":"HK.01196","horizon":"NEXT_CLOSE","ticket_id":f"synthetic-{i}",
            "model_version":"TK-V3-SYNTHETIC-NOT_REAL",
            "price_basis":"FORWARD_EXCLUDE_CASH_DIVIDEND",
            "hour_feature_certified":True,"official_outcome_status":"SETTLED_MOOMOO",
            "trade_permission":"WAIT","frozen_v3_probs":{"UP":.38,"DOWN":.38,"FLAT":.24},
            "actual_direction":direction,"issued_at":issue.isoformat(),
            "target_start_at":target.isoformat(),
            "target_end_at":(target+timedelta(hours=6)).isoformat(),
            "official_settled_at":(target+timedelta(hours=7)).isoformat(),
            "hunter":evidence("HUNTER_MARKET_BREADTH",h),
            "phase2":evidence("PHASE2_EVENT_RISK",q),
        })
    return {"mode":"V3_TOOL_ABLATION","data_provenance":"CONFIRMED_MOOMOO",
            "prospective_outcomes_only":True,
            "case":{"evidence_role":"REUSED_DEVELOPMENT","rows":rows}}


class V3AblationSyntheticTests(unittest.TestCase):
    def test_four_arms_research_only(self):
        result=evaluate(fixture())
        self.assertEqual(result["evaluation_status"],"RESEARCH_ONLY")
        self.assertFalse(result["training_weights_changed"])
        self.assertFalse(result["orders_executed"])
        panel=result["result"]["evaluations"]["HK.01196:NEXT_CLOSE"]
        self.assertEqual(panel["status"],"RETROSPECTIVE_DEVELOPMENT_ONLY")
        self.assertEqual(set(panel["arms"]),{"A","B","C","D"})
        self.assertTrue(all(not arm["promoted_to_v3"] for arm in panel["arms"].values()))

    def test_no_input_no_fake_results(self):
        result=evaluate_ablation(fixture(0))
        self.assertEqual(result["status"],"NO_MATURED_VERIFIED_INPUT")
        self.assertEqual(result["evaluations"],{})

    def test_hunter_future_publication_fails(self):
        p=fixture()
        p["case"]["rows"][2]["hunter"]["published_at"]=p["case"]["rows"][2]["target_start_at"]
        with self.assertRaisesRegex(InvalidAblationInput,"LOOKAHEAD_BLOCKED"):
            evaluate_ablation(p)

    def test_phase2_future_publication_fails(self):
        p=fixture()
        p["case"]["rows"][2]["phase2"]["published_at"]=p["case"]["rows"][2]["target_start_at"]
        with self.assertRaisesRegex(InvalidAblationInput,"LOOKAHEAD_BLOCKED"):
            evaluate_ablation(p)

    def test_missing_phase2_safe_skip(self):
        p=fixture()
        for row in p["case"]["rows"]:
            row.pop("phase2")
        r=evaluate_ablation(p)["evaluations"]["HK.01196:NEXT_CLOSE"]["arms"]
        self.assertEqual(r["B"]["status"],"RESEARCH_ONLY_EVALUATED")
        self.assertEqual(r["C"]["status"],"INSUFFICIENT_MATCHED_CAUSAL_SAMPLES")
        self.assertEqual(r["D"]["status"],"INSUFFICIENT_MATCHED_CAUSAL_SAMPLES")

    def test_unverified_hour_blocks_scoring(self):
        p=fixture()
        p["case"]["rows"][0]["hour_feature_certified"]=False
        with self.assertRaises(InvalidAblationInput):
            evaluate_ablation(p)

    def test_hunter_cannot_replace_broker_price(self):
        p=fixture()
        p["case"]["rows"][0]["hunter"]["price_replaces_moomoo"]=True
        with self.assertRaises(InvalidAblationInput):
            evaluate_ablation(p)

    def test_postissue_predictions_blocked(self):
        p=fixture()
        p["case"]["rows"][0]["issued_at"]=p["case"]["rows"][0]["target_start_at"]
        with self.assertRaises(InvalidAblationInput):
            evaluate_ablation(p)

    def test_mixed_adjustment_and_duplicate_blocked(self):
        p=fixture()
        p["case"]["rows"][0]["price_basis"]="BACKWARD_INCLUDES_CASH_DIVIDEND"
        with self.assertRaises(InvalidAblationInput):
            evaluate_ablation(p)
        p=fixture()
        p["case"]["rows"][1]["ticket_id"]=p["case"]["rows"][0]["ticket_id"]
        with self.assertRaises(InvalidAblationInput):
            evaluate_ablation(p)

    def test_multiple_horizons_not_pooled(self):
        p=fixture()
        for r in p["case"]["rows"][100:]:
            r["horizon"]="NEXT_OPEN"
        result=evaluate_ablation(p)["evaluations"]
        self.assertEqual(result["HK.01196:NEXT_CLOSE"]["status"],"RETROSPECTIVE_DEVELOPMENT_ONLY")
        self.assertEqual(result["HK.01196:NEXT_OPEN"]["status"],"INSUFFICIENT_PROVEN_MATURED_TICKETS")

if __name__=="__main__":
    unittest.main(verbosity=2)

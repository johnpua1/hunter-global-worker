"""V3 A/B/C/D read-only source ablation; no network, broker or database access.

A=frozen V3, B=V3+Hunter, C=V3+Phase2, D=V3+both. Candidate
calibration is a research-only overlay; never changes original V3 models.
Only independently matured, time-causal evidence may be compared.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any
import numpy as np

SYMBOLS={"US.QQQM","HK.01196","HK.02225"}
HORIZONS={"NEXT_HOUR","NEXT_OPEN","NEXT_CLOSE"}
ARMS={"A":(),"B":("hunter",),"C":("phase2",),"D":("hunter","phase2")}
LABELS=("UP","DOWN","FLAT")
BINARY_LABELS=("UP","DOWN")
SOURCE_NAMES={"hunter":"HUNTER_MARKET_BREADTH","phase2":"PHASE2_EVENT_RISK"}
PRICE_BASIS="FORWARD_EXCLUDE_CASH_DIVIDEND"

class InvalidAblationInput(ValueError):
    """Source, provenance, label or timestamp invalid."""

def _utc(value,label):
    if not isinstance(value,str):
        raise InvalidAblationInput(f"{label} requires timezone-aware ISO timestamp")
    try:
        d=datetime.fromisoformat(value.replace("Z","+00:00"))
        if d.utcoffset() is None:
            raise ValueError("missing timezone")
        return d.astimezone(timezone.utc)
    except ValueError as ex:
        raise InvalidAblationInput(f"{label} invalid timezone") from ex

def _feature(raw,source,issued):
    if raw is None:
        return None
    if not isinstance(raw,dict) or raw.get("source")!=SOURCE_NAMES[source] or raw.get("status")!="VERIFIED_ASOF":
        raise InvalidAblationInput(f"{source} lacks source verification")
    if not isinstance(raw.get("source_ref"),str) or not raw["source_ref"].strip():
        raise InvalidAblationInput(f"{source} original evidence missing")
    observed=_utc(raw.get("observed_at"),f"{source}.observed_at")
    published=_utc(raw.get("published_at"),f"{source}.published_at")
    if observed>published or published>issued:
        raise InvalidAblationInput(f"LOOKAHEAD_BLOCKED: {source} not known at V3 issue time")
    if raw.get("price_replaces_moomoo") is not False:
        raise InvalidAblationInput(f"{source} cannot replace official moomoo prices")
    try:
        value=float(raw["value"])
    except (ValueError,TypeError,KeyError) as ex:
        raise InvalidAblationInput(f"{source} score missing") from ex
    if not math.isfinite(value) or not -3<=value<=3:
        raise InvalidAblationInput(f"{source} score outside [-3,3]")
    return value

def _validate(payload):
    if not isinstance(payload,dict) or payload.get("mode")!="V3_TOOL_ABLATION":
        raise InvalidAblationInput("V3_TOOL_ABLATION mode required")
    if payload.get("data_provenance")!="CONFIRMED_MOOMOO" or payload.get("prospective_outcomes_only") is not True:
        raise InvalidAblationInput("Causal finalized official moomoo outcomes required")
    case=payload.get("case")
    if not isinstance(case,dict) or case.get("evidence_role") not in ("REUSED_DEVELOPMENT","NEW_PROSPECTIVE"):
        raise InvalidAblationInput("Declare independent forward vs reused development evidence")
    inputs=case.get("rows",[])
    if not isinstance(inputs,list):
        raise InvalidAblationInput("rows must be list")
    ids=set()
    prepared=[]
    for row in inputs:
        if not isinstance(row,dict) or row.get("symbol") not in SYMBOLS or row.get("horizon") not in HORIZONS:
            raise InvalidAblationInput("Unsupported symbol/horizon")
        key=(row["symbol"],row["horizon"],row.get("ticket_id"))
        if not isinstance(key[-1],str) or not key[-1] or key in ids:
            raise InvalidAblationInput("Missing or duplicate immutable V3 ticket")
        ids.add(key)
        if row.get("price_basis")!=PRICE_BASIS or row.get("official_outcome_status")!="SETTLED_MOOMOO":
            raise InvalidAblationInput("Uncertified actual outcome or mixed price adjustment")
        version=row.get("model_version")
        if not isinstance(version,str) or not version.startswith("TK-V3-"):
            raise InvalidAblationInput("Frozen V3 model required")
        if row.get("hour_feature_certified") is not True:
            raise InvalidAblationInput("Incomplete hourly data cannot be scored as V3")
        class_labels=BINARY_LABELS if row["horizon"]=="NEXT_CLOSE" else LABELS
        if row.get("trade_permission")!="WAIT" or row.get("actual_direction") not in class_labels:
            raise InvalidAblationInput("Invalid research-only outcome")
        issued=_utc(row.get("issued_at"),"issued_at")
        start=_utc(row.get("target_start_at"),"target_start_at")
        end=_utc(row.get("target_end_at"),"target_end_at")
        finalized=_utc(row.get("official_settled_at"),"official_settled_at")
        if not issued<start<end<=finalized:
            raise InvalidAblationInput("Forecast not issued before event or outcome not finalized")
        probs=row.get("frozen_v3_probs")
        if not isinstance(probs,dict) or set(probs)!=set(class_labels):
            raise InvalidAblationInput("Frozen probabilities must exactly match original horizon classes; no synthetic FLAT")
        p=np.asarray([probs[z] for z in class_labels],dtype=float)
        if not np.isfinite(p).all() or (p<=0).any() or (p>=1).any() or abs(float(p.sum())-1)>1e-5:
            raise InvalidAblationInput("Invalid frozen probabilities")
        feat={s:_feature(row.get(s),s,issued) for s in SOURCE_NAMES}
        prepared.append({"key":key,"at":start,"date":start.date().isoformat(),
                         "p":p,"y":class_labels.index(row["actual_direction"]),"feat":feat})
    return prepared,case["evidence_role"]

def _softmax(z):
    z=z-z.max(axis=1,keepdims=True)
    e=np.exp(z)
    return e/e.sum(axis=1,keepdims=True)

def _fit(p,y,x):
    """Softmax additive residual correction; scaling learned from TRAIN only."""
    mu=x.mean(axis=0) if x.shape[1] else np.zeros(0)
    sd=np.maximum(x.std(axis=0),.1) if x.shape[1] else np.zeros(0)
    x=(x-mu)/sd if x.shape[1] else x
    design=np.column_stack((np.ones(len(y)),x))
    beta=np.zeros((design.shape[1],p.shape[1]),float)
    target=np.eye(p.shape[1])[y]
    for _ in range(500):
        pred=_softmax(np.log(p)+design@beta)
        grad=design.T@(pred-target)/len(y)
        grad[0]+=.025*beta[0]
        grad[1:]+=.16*beta[1:]
        beta-=.35*grad
    return beta,mu,sd

def _apply(p,x,fit):
    beta,mu,sd=fit
    if x.shape[1]:
        x=(x-mu)/sd
    return _softmax(np.log(p)+np.column_stack((np.ones(len(p)),x))@beta)

def _score(p,y):
    hit=(p.argmax(axis=1)==y)
    brier=float(np.square(p-np.eye(p.shape[1])[y]).sum(axis=1).mean())
    return {"n":int(len(y)),"hits":int(hit.sum()),
            "accuracy_pct":round(100*float(hit.mean()),4),
            "brier":round(brier,6)}

def _cluster_ci(candidate,control,y,dates):
    d=(candidate.argmax(axis=1)==y).astype(float)-(control.argmax(axis=1)==y).astype(float)
    u=sorted(set(dates))
    if len(u)<10:
        return [-100.0,100.0]
    dx=np.asarray(dates)
    groups=[np.where(dx==day)[0] for day in u]
    rng=np.random.default_rng(20261020)
    gains=np.empty(1000)
    for i in range(1000):
        sampled=np.concatenate([groups[j] for j in rng.integers(len(groups),size=len(groups))])
        gains[i]=100*float(d[sampled].mean())
    return [round(float(np.percentile(gains,2.5)),4),round(float(np.percentile(gains,97.5)),4)]

def _evaluate_group(rows,role):
    rows=sorted(rows,key=lambda r:(r["at"],r["key"][-1]))
    out={"matured_tickets":len(rows),"status":"INSUFFICIENT_PROVEN_MATURED_TICKETS",
         "promoted_to_v3":False,"arms":{}}
    if len(rows)<100:
        return out
    cut=max(50,int(.7*len(rows)))
    if len(rows)-cut<30:
        return out
    train,test=rows[:cut],rows[cut:]
    train_y=np.array([r["y"] for r in train],int)
    simple=int(np.bincount(train_y,minlength=train[0]["p"].size).argmax()) # selected before test
    test_y=np.array([r["y"] for r in test],int)
    test_p=np.stack([r["p"] for r in test])
    out.update({"status":"RETROSPECTIVE_DEVELOPMENT_ONLY" if role=="REUSED_DEVELOPMENT"
                else "PROSPECTIVE_RESEARCH_NOT_PROMOTED",
                "train_count":len(train),"test_count":len(test),
                "frozen_v3":_score(test_p,test_y),
                "simple_baseline_preselected":"ALWAYS_"+LABELS[simple],
                "simple_baseline_test_accuracy_pct":round(100*float((test_y==simple).mean()),4),
                "test_not_used_to_select_model":True})
    for arm,names in ARMS.items():
        tr=[r for r in train if all(r["feat"][k] is not None for k in names)]
        te=[r for r in test if all(r["feat"][k] is not None for k in names)]
        m={"sources":list(names),"matched_train":len(tr),"matched_test":len(te),
           "coverage_pct":round(100*len(te)/len(test),2),"promoted_to_v3":False}
        if len(tr)<50 or len(te)<30 or len(set(r["date"] for r in te))<10:
            m["status"]="INSUFFICIENT_MATCHED_CAUSAL_SAMPLES"
            out["arms"][arm]=m
            continue
        trp=np.stack([r["p"] for r in tr]);try_=np.array([r["y"] for r in tr],int)
        tep=np.stack([r["p"] for r in te]);tey=np.array([r["y"] for r in te],int)
        tx=np.asarray([[r["feat"][k] for k in names] for r in tr],float).reshape(len(tr),len(names))
        ex=np.asarray([[r["feat"][k] for k in names] for r in te],float).reshape(len(te),len(names))
        cand=_apply(tep,ex,_fit(trp,try_,tx))
        control=_apply(tep,np.empty((len(te),0)),_fit(trp,try_,np.empty((len(tr),0))))
        scored=_score(cand,tey);control_scored=_score(control,tey)
        ci=_cluster_ci(cand,control,tey,[r["date"] for r in te])
        simple_matched=float((tey==simple).mean())
        m.update({"status":"RESEARCH_ONLY_EVALUATED","candidate":scored,
                  "same_sample_calibration_only_control":control_scored,
                  "same_sample_frozen_v3":_score(tep,tey),
                  "matched_simple_baseline_accuracy_pct":round(100*simple_matched,4),
                  "paired_gain_vs_calibration_control_pct_points":
                     round(scored["accuracy_pct"]-control_scored["accuracy_pct"],4),
                  "paired_cluster_gain_95_pct_points":ci,
                  "brier_better_than_control":scored["brier"]<control_scored["brier"]})
        # RESEARCH gate, not automatic deployment; no fitting/tuning on holdout.
        m["research_gate_passed"]=bool(
            role=="NEW_PROSPECTIVE" and len(te)>=300 and scored["accuracy_pct"]>=55
            and scored["accuracy_pct"]-100*simple_matched>=5 and ci[0]>0
            and scored["brier"]<control_scored["brier"] and all(
              float((cand[s].argmax(axis=1)==tey[s]).mean())>=
              float((control[s].argmax(axis=1)==tey[s]).mean())
              for s in (slice(0,len(te)//2),slice(len(te)//2,None))
            ))
        m["requires_separate_user_authorization_to_promote"]=True
        out["arms"][arm]=m
    return out

def evaluate_ablation(payload:dict[str,Any])->dict[str,Any]:
    rows,role=_validate(payload)
    groups={}
    for row in rows:
        groups.setdefault(row["key"][:2],[]).append(row)
    result={"contract":"V3_READ_ONLY_ABCD_PAIRED_EVIDENCE_V1",
            "status":"NO_MATURED_VERIFIED_INPUT" if not rows else "EVALUATED_OR_SAFE_SKIPPED",
            "role":role,"input_rows":len(rows),"evaluations":{},
            "production_model_changed":False,"new_forecasts_issued":False,
            "original_tickets_mutated":False,"orders_executed":False,
            "additional_scheduled_tasks":0,"trade_permission":"WAIT"}
    for (symbol,horizon),part in sorted(groups.items()):
        result["evaluations"][symbol+":"+horizon]=_evaluate_group(part,role)
    return result

def main():
    parser=argparse.ArgumentParser(description="Private offline V3 A/B/C/D read-only ablation")
    parser.add_argument("input")
    args=parser.parse_args()
    report=evaluate_ablation(json.loads(Path(args.input).read_text(encoding="utf8")))
    print(json.dumps(report,ensure_ascii=False,sort_keys=True,allow_nan=False))

if __name__=="__main__":
    main()

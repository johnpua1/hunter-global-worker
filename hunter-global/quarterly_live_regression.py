"""Read-only G178 real-data regression for quarterly V2.

Never writes Drive.  Used only to prove that the quarterly implementation
reproduces the frozen SNAPSHOT_2026-10-01 authority before deployment.
"""
from __future__ import annotations
import json, math
from runner import Drive
import quarterly_v2 as q

SNAPSHOT="SNAPSHOT_2026-10-01"

EXPECTED={
  "direction":{
    "long":{"ticker":"SPY","signal":"S4","H":10,"n":64,
            "benchmark_n":199,"wr":0.8125,
            "benchmark_wr":0.7085427135678392,
            "delta":0.10395728643216084,
            "ci":[0.002262395990794081,0.24064926907263587]},
    "short":{"ticker":"IWM","signal":"S3","H":5,"n":11,
             "benchmark_n":50,"wr":0.6363636363636364,
             "benchmark_wr":0.3,
             "delta":0.33636363636363636,
             "ci":[0.15625,0.52]},
    "long_oos":{"n":93,"benchmark_n":241,"wr":0.46236559139784944,
                "benchmark_wr":0.5560165975103735,
                "delta":-0.09365100611252403,
                "ci":[-0.24595441948804292,0.05589208705048669],
                "pass":False},
    "short_oos":{"n":0,"benchmark_n":245,"wr":None,
                 "benchmark_wr":0.4897959183673469,
                 "delta":None,"ci":[None,None],"pass":False},
    "coverage":0.0,
  },
  "baseline":{
    "LONG":{
      "IS":{"n":5854,"own":0.3973315753041392,"bench":0.40606151903052695,
            "delta":-0.008729943726387746,
            "own_ci":[0.32074805500083875,0.4487483734177951],
            "bench_ci":[0.3326195719713835,0.45125047918601036],
            "delta_ci":[-0.04760463183193934,0.01766311297072966]},
      "FINAL_OOS":{"n":7410,"own":0.391389410574781,"bench":0.38934503480645566,
            "delta":0.002044375768325335,
            "own_ci":[0.32877753146850847,0.45678875091537513],
            "bench_ci":[0.34007501567340487,0.4447072467158767],
            "delta_ci":[-0.023803726458850493,0.0282587175145511]},
      "verdict":"MARKET_DRIFT_ONLY"},
    "SHORT":{
      "IS":{"n":7428,"own":0.39592643775206443,"bench":0.4052858110215986,
            "delta":-0.009359373269534177,
            "own_ci":[0.3160729764045456,0.4928254486078897],
            "bench_ci":[0.32799022662396715,0.5033731524768936],
            "delta_ci":[-0.02730780834120174,0.012354992675052976]},
      "FINAL_OOS":{"n":7611,"own":0.44561326955688624,"bench":0.4177880513769859,
            "delta":0.027825218179900357,
            "own_ci":[0.39130062221738043,0.5073132025127287],
            "bench_ci":[0.37275609381957503,0.4645877656299095],
            "delta_ci":[0.0008906898347010768,0.05922587376500989]},
      "verdict":"OOS_ONLY"},
  }
}

def eq(a,b,tol=1e-12):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a,(int,str,bool)) or isinstance(b,(int,str,bool)):
        return a==b
    return math.isclose(float(a),float(b),rel_tol=0.0,abs_tol=tol)

def assert_metric(label, got, exp):
    failures=[]
    for k,v in exp.items():
        if k=="ci":
            gv=got.get("delta_ci")
            if not (isinstance(gv,list) and len(gv)==2 and all(eq(x,y) for x,y in zip(gv,v))):
                failures.append([label+".delta_ci",gv,v])
        elif not eq(got.get(k),v):
            failures.append([label+"."+k,got.get(k),v])
    return failures

def main():
    drive=Drive()
    drive.health()
    manifest=drive.json(SNAPSHOT+"/MANIFEST.json")
    if manifest.get("as_of")!="2026-10-01":
        raise SystemExit("SNAPSHOT_MANIFEST_ASOF_MISMATCH")
    groups=q._load_snapshot_groups(drive,SNAPSHOT)
    bench=q._load_snapshot_benchmark(drive,SNAPSHOT)
    split=q._g178_split()
    direction=q.direction_backtest(bench,split)
    baseline=q.vertical_baseline(groups,split)
    same=q.same_direction_test(groups,split,direction)

    failures=[]
    failures += assert_metric("direction.long",direction["is_selected"]["LONG"],EXPECTED["direction"]["long"])
    failures += assert_metric("direction.short",direction["is_selected"]["SHORT"],EXPECTED["direction"]["short"])
    failures += assert_metric("direction.long_oos",direction["final_oos_standalone"]["LONG"],EXPECTED["direction"]["long_oos"])
    failures += assert_metric("direction.short_oos",direction["final_oos_standalone"]["SHORT"],EXPECTED["direction"]["short_oos"])
    if direction.get("LONG")!="无合格信号": failures.append(["direction.LONG",direction.get("LONG"),"无合格信号"])
    if direction.get("SHORT")!="无合格信号": failures.append(["direction.SHORT",direction.get("SHORT"),"无合格信号"])
    if direction.get("final_online")!=[]: failures.append(["direction.final_online",direction.get("final_online"),[]])
    if not eq(direction["final_replay_counts"]["coverage"],0.0):
        failures.append(["direction.coverage",direction["final_replay_counts"]["coverage"],0.0])

    for side in ("LONG","SHORT"):
        if baseline[side]["verdict"]!=EXPECTED["baseline"][side]["verdict"]:
            failures.append([f"baseline.{side}.verdict",baseline[side]["verdict"],EXPECTED["baseline"][side]["verdict"]])
        for seg in ("IS","FINAL_OOS"):
            got=baseline[side][seg]; exp=EXPECTED["baseline"][side][seg]
            for k,v in exp.items():
                gv=got.get(k)
                if isinstance(v,list):
                    if not (isinstance(gv,list) and len(gv)==len(v) and all(eq(x,y) for x,y in zip(gv,v))):
                        failures.append([f"baseline.{side}.{seg}.{k}",gv,v])
                elif not eq(gv,v):
                    failures.append([f"baseline.{side}.{seg}.{k}",gv,v])

    report={
      "snapshot":SNAPSHOT,
      "direction":{"LONG":direction.get("LONG"),"SHORT":direction.get("SHORT"),
                   "is_selected":direction.get("is_selected"),
                   "final_oos_standalone":direction.get("final_oos_standalone"),
                   "final_replay_counts":direction.get("final_replay_counts")},
      "baseline":baseline,
      "same_direction_test":same,
      "failures":failures,
      "pass":not failures,
    }
    print(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(",",":")))
    if failures:
        raise SystemExit("G178_REAL_DATA_REGRESSION_MISMATCH")

if __name__=="__main__":
    main()

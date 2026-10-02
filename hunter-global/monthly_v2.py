"""Monthly V2.0 recertification for INVESTMENT-DIRECTION-FIRST-PICK-V2-01.

The monthly writer is isolated from US/HK DAILY. It freezes one immutable
snapshot, recalculates only D1 + the registered vertical baseline comparison,
and commits ACTIVE_POINTER last. D/W grids, DW_MAX and DWR/THR are never
recomputed or replaced.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import random
from collections import defaultdict
from typing import Any, Iterable

INDEXES = ("SPY", "QQQ", "DIA", "IWM")
BENCHMARK_ETFS = ("SPY","QQQ","DIA","IWM","SSO","QLD","DDM","UWM","SDS","QID","DXD","TWM")
HORIZONS = (5, 10, 20)
BLOCK = 20
BOOTSTRAPS = 1000
WINDOW_BARS = 501
TAIL_BUFFER = 7
G178_ANALYSIS_DAYS = 494
G178_IS_DAYS = 249
G178_IS_RATIO = G178_IS_DAYS / G178_ANALYSIS_DAYS
G178_IS_END = "2025-09-30"
G178_OOS_START = "2025-10-01"
G178_OOS_END = "2026-09-22"
VALIDATION_SNAPSHOT = "SNAPSHOT_2026-10-01"


def _compact(x: Any) -> bytes:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += float(value)
        if i >= window:
            total -= float(values[i-window])
        if i >= window - 1:
            out[i] = total / window
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs)-1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi-pos) + xs[hi] * (pos-lo)


def _segment(date: str, split: dict[str, Any]) -> str | None:
    if split["is_start"] <= date <= split["is_end"]:
        return "IS"
    if split["oos_start"] <= date <= split["oos_end"]:
        return "FINAL_OOS"
    return None


def _dynamic_split(dates: list[str]) -> dict[str, Any]:
    """Re-cut the rolling window with the same 249:245 + 7-tail shape as G178."""
    dates = sorted(dict.fromkeys(dates))
    if len(dates) < WINDOW_BARS:
        raise RuntimeError("MONTH_SPLIT_REQUIRES_501_INDEX_BARS")
    window = dates[-WINDOW_BARS:]
    analysis = window[:-TAIL_BUFFER]
    if len(analysis) != G178_ANALYSIS_DAYS:
        raise RuntimeError("MONTH_SPLIT_ANALYSIS_LENGTH_DRIFT")
    cut = round(len(analysis) * G178_IS_RATIO)
    if cut != G178_IS_DAYS:
        raise RuntimeError("MONTH_SPLIT_RATIO_DRIFT")
    return {
        "mode": "G178_RATIO_RECUT",
        "window_bars": WINDOW_BARS,
        "tail_buffer_sessions": TAIL_BUFFER,
        "ratio_is": G178_IS_RATIO,
        "is_start": analysis[0],
        "is_end": analysis[cut-1],
        "oos_start": analysis[cut],
        "oos_end": analysis[-1],
        "tail_start": window[-TAIL_BUFFER],
        "tail_end": window[-1],
    }


def _g178_split() -> dict[str, Any]:
    return {
        "mode": "G178_VALIDATION_EXACT",
        "window_bars": WINDOW_BARS,
        "tail_buffer_sessions": TAIL_BUFFER,
        "ratio_is": G178_IS_RATIO,
        "is_start": "2024-09-23",
        "is_end": G178_IS_END,
        "oos_start": G178_OOS_START,
        "oos_end": G178_OOS_END,
        "tail_start": "2026-09-23",
        "tail_end": "2026-10-01",
    }


def _block_bootstrap_delta(signal_by_date: dict[str, list[int]],
                           base_by_date: dict[str, list[int]],
                           seed_key: str) -> tuple[float | None, float | None]:
    dates = sorted(set(signal_by_date) | set(base_by_date))
    blocks = [dates[i:i+BLOCK] for i in range(0, len(dates), BLOCK)]
    if not blocks:
        return None, None
    # Freeze the deterministic SHA-256 key width used by the G178 contract.
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(BOOTSTRAPS):
        chosen: list[str] = []
        for _j in range(len(blocks)):
            chosen.extend(blocks[rng.randrange(len(blocks))])
        # Preserve the original timeline length when the final block is partial.
        chosen = chosen[:len(dates)]
        s = [v for d in chosen for v in signal_by_date.get(d, ())]
        b = [v for d in chosen for v in base_by_date.get(d, ())]
        if s and b:
            draws.append(sum(s)/len(s) - sum(b)/len(b))
    return _pct(draws, .025), _pct(draws, .975)


def _direction_state(rows: list[dict]) -> dict[str, list[int]]:
    closes = [float(r["close"]) for r in rows]
    e8, e17 = _ema(closes, 8), _ema(closes, 17)
    macd = [a-b for a,b in zip(e8,e17)]
    sig9 = _ema(macd, 9)
    hist = [a-b for a,b in zip(macd,sig9)]
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)

    # S1 is the persistent confirmed 8-17-9 histogram state. The G178
    # acceptance run changes state only on a histogram zero-cross.
    s1 = [0] * len(rows)
    state = 0
    # G178 freezes S1 at NONE through the 50-session indicator warmup.
    # Crosses before the first eligible judgment date must not seed S1.
    for i in range(50, len(rows)):
        if hist[i-1] <= 0 < hist[i]:
            state = 1
        elif hist[i-1] >= 0 > hist[i]:
            state = -1
        s1[i] = state
    s2, s3, s4 = [0]*len(rows), [0]*len(rows), [0]*len(rows)
    for i, close in enumerate(closes):
        if sma50[i] is not None:
            s2[i] = 1 if close > sma50[i] else (-1 if close < sma50[i] else 0)
        if sma50[i] is not None and sma200[i] is not None:
            s3[i] = 1 if sma50[i] > sma200[i] else (-1 if sma50[i] < sma200[i] else 0)
        if s1[i] and s1[i] == s2[i]:
            s4[i] = s1[i]
    return {"S1": s1, "S2": s2, "S3": s3, "S4": s4}


def _metric_for_signal(rows: list[dict], state: list[int], side: int, h: int,
                       split: dict[str,Any], segment: str, signal_name: str,
                       seed_key: str) -> dict:
    sig: dict[str, list[int]] = defaultdict(list)
    base: dict[str, list[int]] = defaultdict(list)
    minimum = {"S1":50, "S4":50, "S2":49, "S3":199}[signal_name]
    for i, row in enumerate(rows):
        if i < minimum or _segment(row["date"], split) != segment:
            continue
        entry_i, exit_i = i + 1, i + 1 + h
        if exit_i >= len(rows):
            continue
        entry = float(rows[entry_i]["open"])
        final = float(rows[exit_i]["close"])
        if not (entry > 0 and math.isfinite(entry) and math.isfinite(final)):
            continue
        b = int(final > entry) if side > 0 else int(final < entry)
        base[row["date"]].append(b)
        if state[i] == side:
            sig[row["date"]].append(b)
    sv = [v for xs in sig.values() for v in xs]
    bv = [v for xs in base.values() for v in xs]
    wr = sum(sv)/len(sv) if sv else None
    bwr = sum(bv)/len(bv) if bv else None
    delta = wr-bwr if wr is not None and bwr is not None else None
    low, high = _block_bootstrap_delta(sig, base, seed_key) if sv and bv else (None,None)
    return {"n":len(sv),"benchmark_n":len(bv),"wr":wr,"benchmark_wr":bwr,
            "delta":delta,"delta_ci":[low,high]}


def _replay_once(active: dict[str,dict], by_ticker: dict[str,list[dict]],
                 states: dict[str,dict[str,list[int]]], split: dict[str,Any]) -> dict:
    oos_dates = [r["date"] for r in by_ticker["SPY"]
                 if _segment(r["date"], split) == "FINAL_OOS"]
    positions = {ticker:{r["date"]:i for i,r in enumerate(rows)}
                 for ticker,rows in by_ticker.items()}
    emitted = {"LONG":[], "SHORT":[]}
    for date in oos_dates:
        on=[]
        for side, rec in active.items():
            pos=positions[rec["ticker"]].get(date)
            if pos is None:
                continue
            want=1 if side=="LONG" else -1
            if states[rec["ticker"]][rec["signal"]][pos] == want:
                on.append(side)
        if len(on)==1:
            emitted[on[0]].append(date)

    metrics={}
    for side,rec in active.items():
        rows=by_ticker[rec["ticker"]]
        want=1 if side=="LONG" else -1
        signal_by_date: dict[str,list[int]] = defaultdict(list)
        base_by_date: dict[str,list[int]] = defaultdict(list)
        allowed=set(emitted[side])
        minimum={"S1":50,"S4":50,"S2":49,"S3":199}[rec["signal"]]
        for i,row in enumerate(rows):
            if i < minimum or _segment(row["date"], split) != "FINAL_OOS":
                continue
            entry_i,exit_i=i+1,i+1+int(rec["H"])
            if exit_i>=len(rows):
                continue
            entry=float(rows[entry_i]["open"]); final=float(rows[exit_i]["close"])
            if not (entry>0 and math.isfinite(entry) and math.isfinite(final)):
                continue
            out=int(final>entry) if want>0 else int(final<entry)
            base_by_date[row["date"]].append(out)
            if row["date"] in allowed:
                signal_by_date[row["date"]].append(out)
        sv=[v for xs in signal_by_date.values() for v in xs]
        bv=[v for xs in base_by_date.values() for v in xs]
        wr=sum(sv)/len(sv) if sv else None
        bwr=sum(bv)/len(bv) if bv else None
        delta=wr-bwr if wr is not None and bwr is not None else None
        ci=_block_bootstrap_delta(signal_by_date,base_by_date,
                                  f"REPLAY|{side}|{rec['ticker']}|{rec['signal']}|{rec['H']}") if sv and bv else (None,None)
        metrics[side]={"ticker":rec["ticker"],"signal":rec["signal"],"H":rec["H"],
                       "n":len(sv),"benchmark_n":len(bv),"wr":wr,"benchmark_wr":bwr,
                       "delta":delta,"delta_ci":[ci[0],ci[1]]}
    counts={"LONG":len(emitted["LONG"]),"SHORT":len(emitted["SHORT"])}
    counts["NONE"]=len(oos_dates)-counts["LONG"]-counts["SHORT"]
    counts["total"]=len(oos_dates)
    counts["coverage"]=(counts["LONG"]+counts["SHORT"])/len(oos_dates) if oos_dates else 0.0
    return {"metrics":metrics,"counts":counts,"emitted_dates":emitted}


def direction_backtest(etf_rows: list[dict], split: dict[str,Any]) -> dict:
    by_ticker: dict[str,list[dict]] = defaultdict(list)
    for row in etf_rows:
        if row.get("ticker") in INDEXES:
            by_ticker[row["ticker"]].append(row)
    for ticker in INDEXES:
        by_ticker[ticker].sort(key=lambda x:x["date"])
        if len(by_ticker[ticker]) != WINDOW_BARS:
            raise RuntimeError("D1_INDEX_HISTORY_NOT_501:"+ticker)

    candidates=[]
    states={}
    for ticker in INDEXES:
        rows=by_ticker[ticker]
        states[ticker]=_direction_state(rows)
        for signal in ("S1","S2","S3","S4"):
            for h in HORIZONS:
                for side_name,side in (("LONG",1),("SHORT",-1)):
                    for segment in ("IS","FINAL_OOS"):
                        m=_metric_for_signal(rows,states[ticker][signal],side,h,split,segment,signal,
                            f"D1-G178|{ticker}|{signal}|{h}|{side_name}|{segment}")
                        candidates.append({"ticker":ticker,"signal":signal,"H":h,
                                           "side":side_name,"segment":segment,**m})

    frozen={"LONG":None,"SHORT":None}
    standalone={"LONG":None,"SHORT":None}
    for side in ("LONG","SHORT"):
        eligible=[x for x in candidates if x["segment"]=="IS" and x["side"]==side
                  and x["wr"] is not None and x["wr"]>=.55
                  and x["delta_ci"][0] is not None and x["delta_ci"][0]>0]
        eligible.sort(key=lambda x:(-x["wr"],-x["delta"],x["ticker"],x["signal"],x["H"]))
        if eligible:
            frozen[side]=eligible[0]
            f=eligible[0]
            oos=next(x for x in candidates if x["segment"]=="FINAL_OOS" and
                     x["side"]==side and x["ticker"]==f["ticker"] and
                     x["signal"]==f["signal"] and x["H"]==f["H"])
            standalone[side]={**oos,"pass":bool(oos["wr"] is not None and oos["wr"]>=.55
                and oos["delta_ci"][0] is not None and oos["delta_ci"][0]>0)}

    active={s:standalone[s] for s in ("LONG","SHORT") if standalone[s] and standalone[s]["pass"]}
    replay_rounds=[]
    for round_no in (1,2):
        if not active:
            break
        replay=_replay_once(active,by_ticker,states,split)
        deleted=[s for s,m in replay["metrics"].items()
                 if m["delta_ci"][0] is None or m["delta_ci"][0] <= 0]
        replay_rounds.append({"round":round_no,"active_in":sorted(active),
                              "metrics":replay["metrics"],"counts":replay["counts"],
                              "deleted":deleted})
        if not deleted:
            break
        for side in deleted:
            active.pop(side,None)
        if not active:
            break
    final_replay=_replay_once(active,by_ticker,states,split)
    final_online=sorted(active)
    result={"split":split,"candidates":candidates,"is_selected":frozen,
            "final_oos_standalone":standalone,"replay_rounds":replay_rounds,
            "final_online":final_online,"final_replay_metrics":final_replay["metrics"],
            "final_replay_counts":final_replay["counts"],
            "_emitted_dates":final_replay["emitted_dates"]}
    for side in ("LONG","SHORT"):
        if side not in active:
            result[side]="无合格信号"
        else:
            result[side]={**{k:active[side][k] for k in ("ticker","signal","H")},
                          **final_replay["metrics"][side]}
    return result


def _row_valid(row: dict) -> bool:
    try:
        o,h,l,c,v=(float(row[k]) for k in ("open","high","low","close","volume"))
    except (KeyError,TypeError,ValueError):
        return False
    return (all(math.isfinite(x) for x in (o,h,l,c,v)) and min(o,h,l,c)>0 and v>=0
            and h>=max(o,c,l) and l<=min(o,c))


def _vertical_p(side: str, s0: float, st: float) -> float:
    move=st-s0 if side=="LONG" else s0-st
    return max(0.0,min(1.0,move/(s0*.05)))


def _stock_signal_samples(rows: list[dict], split: dict[str,Any]) -> list[dict]:
    if len(rows)<501:
        return []
    rows=sorted(rows,key=lambda x:x["date"])
    closes=[float(r["close"]) for r in rows]
    e8,e17,e50=_ema(closes,8),_ema(closes,17),_ema(closes,50)
    macd=[a-b for a,b in zip(e8,e17)]
    sig9=_ema(macd,9)
    hist=[a-b for a,b in zip(macd,sig9)]
    out=[]
    for i in range(50,len(rows)-20):
        seg=_segment(rows[i]["date"],split)
        if not seg or not _row_valid(rows[i]) or not _row_valid(rows[i+20]):
            continue
        s0=float(rows[i]["close"]); st=float(rows[i+20]["close"])
        if not (s0>0 and math.isfinite(st)):
            continue
        long_cross=(macd[i]>0>=macd[i-1] and e17[i]>e50[i] and hist[i]>0)
        short_cross=(macd[i]<0<=macd[i-1] and e17[i]<e50[i] and hist[i]<0)
        if long_cross:
            out.append({"security_id":rows[i]["security_id"],"date":rows[i]["date"],
                        "segment":seg,"side":"LONG","p":_vertical_p("LONG",s0,st),
                        "dwr":int(st>s0)})
        if short_cross:
            out.append({"security_id":rows[i]["security_id"],"date":rows[i]["date"],
                        "segment":seg,"side":"SHORT","p":_vertical_p("SHORT",s0,st),
                        "dwr":int(st<s0)})
    return out


def _bootstrap_paired(samples: list[dict], seed_key: str) -> dict[str,list[float|None]]:
    dates=sorted({x["date"] for x in samples})
    blocks=[dates[i:i+BLOCK] for i in range(0,len(dates),BLOCK)]
    if not blocks:
        return {"own_ci":[None,None],"bench_ci":[None,None],"delta_ci":[None,None]}
    by_date=defaultdict(list)
    for row in samples:
        by_date[row["date"]].append(row)
    rng=random.Random(int(hashlib.sha256(seed_key.encode()).hexdigest()[:16],16))
    own_draws=[]; bench_draws=[]; delta_draws=[]
    for _ in range(BOOTSTRAPS):
        chosen=[]
        for _j in range(len(blocks)):
            chosen.extend(blocks[rng.randrange(len(blocks))])
        rows=[x for d in chosen for x in by_date.get(d,())]
        if not rows:
            continue
        own=sum(x["own"] for x in rows)/len(rows)
        bench=sum(x["bench"] for x in rows)/len(rows)
        own_draws.append(own); bench_draws.append(bench); delta_draws.append(own-bench)
    return {"own_ci":[_pct(own_draws,.025),_pct(own_draws,.975)],
            "bench_ci":[_pct(bench_draws,.025),_pct(bench_draws,.975)],
            "delta_ci":[_pct(delta_draws,.025),_pct(delta_draws,.975)]}


def vertical_baseline(stock_groups: Iterable[list[dict]], split: dict[str,Any]) -> dict:
    groups=[sorted(rows,key=lambda x:x["date"]) for rows in stock_groups if len(rows)>=501]
    totals={side:{seg:defaultdict(lambda:[0,0.0]) for seg in ("IS","FINAL_OOS")}
            for side in ("LONG","SHORT")}
    own_lookup={}
    samples=[]
    for rows in groups:
        sid=rows[0]["security_id"]
        for i in range(0,len(rows)-20):
            seg=_segment(rows[i]["date"],split)
            if not seg or not _row_valid(rows[i]) or not _row_valid(rows[i+20]):
                continue
            s0=float(rows[i]["close"]); st=float(rows[i+20]["close"])
            for side in ("LONG","SHORT"):
                p=_vertical_p(side,s0,st)
                bucket=totals[side][seg][rows[i]["date"]]
                bucket[0]+=1; bucket[1]+=p
                own_lookup[(sid,rows[i]["date"],side)]=p
        samples.extend(_stock_signal_samples(rows,split))

    out={}
    for side in ("LONG","SHORT"):
        out[side]={}
        for seg in ("IS","FINAL_OOS"):
            paired=[]
            for s in samples:
                if s["side"]!=side or s["segment"]!=seg:
                    continue
                count,total=totals[side][seg].get(s["date"],(0,0.0))
                own=own_lookup.get((s["security_id"],s["date"],side))
                if own is None or count<=1:
                    continue
                bench=(total-own)/(count-1)
                paired.append({"date":s["date"],"own":own,"bench":bench})
            om=sum(x["own"] for x in paired)/len(paired) if paired else None
            bm=sum(x["bench"] for x in paired)/len(paired) if paired else None
            delta=om-bm if om is not None and bm is not None else None
            ci=_bootstrap_paired(paired,f"VERTICAL_BASELINE|{side}|{seg}")
            out[side][seg]={"n":len(paired),"own":om,"own_ci":ci["own_ci"],
                            "bench":bm,"bench_ci":ci["bench_ci"],"delta":delta,
                            "delta_ci":ci["delta_ci"],
                            "unique_dates":len({x["date"] for x in paired})}
        is_delta=out[side]["IS"]["delta"]
        oos_low=out[side]["FINAL_OOS"]["delta_ci"][0]
        if oos_low is not None and oos_low>0:
            out[side]["verdict"]="SIGNAL_EDGE_CONFIRMED" if is_delta is not None and is_delta>0 else "OOS_ONLY"
        else:
            out[side]["verdict"]="MARKET_DRIFT_ONLY"
    return out


def same_direction_test(stock_groups: Iterable[list[dict]], split: dict[str,Any],
                        direction: dict) -> dict:
    emitted=direction.get("_emitted_dates",{"LONG":[],"SHORT":[]})
    d2={d:"LONG" for d in emitted.get("LONG",[])}
    d2.update({d:"SHORT" for d in emitted.get("SHORT",[])})
    buckets={side:{k:[] for k in ("SAME","OPPOSITE","NONE")} for side in ("LONG","SHORT")}
    for rows in stock_groups:
        for s in _stock_signal_samples(rows,split):
            if s["segment"]!="FINAL_OOS":
                continue
            state=d2.get(s["date"])
            klass="SAME" if state==s["side"] else ("OPPOSITE" if state in ("LONG","SHORT") else "NONE")
            dw=.34 if s["side"]=="LONG" else .40
            r=s["p"]/dw-1
            buckets[s["side"]][klass].append((r,s["dwr"]))
    out={}
    for side in ("LONG","SHORT"):
        out[side]={}
        for klass,values in buckets[side].items():
            out[side][klass]={"n":len(values),
                              "vertical_wr":sum(1 for r,_ in values if r>0)/len(values) if values else None,
                              "avg_R":sum(r for r,_ in values)/len(values) if values else None,
                              "DWR":sum(d for _,d in values)/len(values) if values else None}
    return out


def _parse_gz_ndjson(data: bytes) -> list[dict]:
    return [json.loads(x) for x in gzip.decompress(data).splitlines() if x]


def _parse_gz_csv(data: bytes) -> list[dict]:
    text=gzip.decompress(data).decode("utf-8-sig")
    rows=[]
    for row in csv.DictReader(io.StringIO(text)):
        item=dict(row)
        for key in ("open","high","low","close","volume"):
            if key in item and item[key] not in ("",None):
                item[key]=float(item[key])
        rows.append(item)
    return rows


def _write_gz_ndjson(rows: list[dict]) -> bytes:
    raw=b"\n".join(_compact(x) for x in rows)+(b"\n" if rows else b"")
    return gzip.compress(raw,mtime=0)


def _load_snapshot_groups(drive, snapshot: str) -> list[list[dict]]:
    files=drive.list(snapshot)
    names={x["name"] for x in files}
    parts=sorted(x for x in names if x.startswith("US_ACTIVE_OHLC_PART_") and x.endswith(".ndjson.gz"))
    if parts:
        rows=[]
        for name in parts:
            rows.extend(_parse_gz_ndjson(drive.read(snapshot+"/"+name)))
    elif "US_ACTIVE_OHLC.csv.gz" in names:
        rows=_parse_gz_csv(drive.read(snapshot+"/US_ACTIVE_OHLC.csv.gz"))
    else:
        raise RuntimeError("SNAPSHOT_OHLC_MISSING:"+snapshot)
    by=defaultdict(list)
    for row in rows:
        by[row["security_id"]].append(row)
    return [sorted(v,key=lambda x:x["date"]) for _,v in sorted(by.items())]


def _load_snapshot_benchmark(drive, snapshot: str) -> list[dict]:
    names={x["name"] for x in drive.list(snapshot)}
    if "BENCHMARK_ETF_12_501.ndjson.gz" in names:
        rows=_parse_gz_ndjson(drive.read(snapshot+"/BENCHMARK_ETF_12_501.ndjson.gz"))
    elif "BENCHMARK_ETF_12_501.csv.gz" in names:
        rows=_parse_gz_csv(drive.read(snapshot+"/BENCHMARK_ETF_12_501.csv.gz"))
    else:
        raise RuntimeError("SNAPSHOT_BENCHMARK_MISSING:"+snapshot)
    by=defaultdict(list)
    for row in rows:
        by[row["ticker"]].append(row)
    if set(by)!=set(BENCHMARK_ETFS) or any(len(v)!=WINDOW_BARS for v in by.values()):
        raise RuntimeError("SNAPSHOT_BENCHMARK_IDENTITY_DRIFT")
    return rows


def _build_snapshot(drive, asof: str) -> tuple[str,list[list[dict]],list[dict]]:
    from analytics import compose, split_adjust
    from derived import read_files

    snapshot="SNAPSHOT_"+asof
    existing=drive.file(snapshot+"/MANIFEST.json")
    if existing:
        manifest=drive.json(snapshot+"/MANIFEST.json")
        if manifest.get("as_of")!=asof:
            raise RuntimeError("SNAPSHOT_NAME_ASOF_CONFLICT")
        return snapshot,_load_snapshot_groups(drive,snapshot),_load_snapshot_benchmark(drive,snapshot)

    universe=drive.json("US/CURRENT_UNIVERSE.json")
    active={x["security_id"]:x for x in universe["securities"] if x.get("listing_status")=="ACTIVE"}
    candidate_active={sid:sec for sid,sec in active.items()
                      if sec.get("ticker") not in BENCHMARK_ETFS}
    base_asof=drive.json("US/CHECKPOINT.json")["as_of"]

    daily=defaultdict(list)
    for item in drive.list("US/DAILY"):
        name=item["name"]
        if not (len(name)==10 and name[4]=="-" and name[7]=="-") or name<=base_asof or name>asof:
            continue
        for path in read_files(drive,"US","DAILY/"+name,(".ndjson.gz",".ndjson.gzip")):
            for row in _parse_gz_ndjson(drive.read(path)):
                if row.get("security_id") in candidate_active:
                    daily[row["security_id"]].append(row)

    patches=defaultdict(list)
    for path in read_files(drive,"US","REPAIR_PATCH",".json"):
        payload=drive.json(path)
        items=payload.get("items") if isinstance(payload,dict) else None
        if not isinstance(items,list):
            items=[payload] if isinstance(payload,dict) else []
        for patch in items:
            if patch.get("security_id") in candidate_active:
                patches[patch["security_id"]].append(patch)

    events=[]
    for path in read_files(drive,"US","CORPORATE_ACTIONS",".json"):
        payload=drive.json(path)
        if isinstance(payload,list):
            events.extend(payload)

    drive.folder(snapshot,create=True)
    uni_doc={"schema":"HUNTER_MONTHLY_SNAPSHOT_V2","snapshot_name":snapshot,
             "market":"US","as_of":asof,"active_count":len(active),
             "securities":[active[k] for k in sorted(active)]}
    drive.put(snapshot+"/US_ACTIVE_UNIVERSE.json",_compact(uni_doc),immutable=True)

    groups=[]
    part_manifest=[]
    base_files=sorted(x["name"] for x in drive.list("US/BASE") if x["name"].endswith(".ndjson.gz"))
    seen=set()
    for part_no,name in enumerate(base_files,1):
        by=defaultdict(list)
        for row in _parse_gz_ndjson(drive.read("US/BASE/"+name)):
            sid=row.get("security_id")
            if sid in candidate_active:
                by[sid].append(row)
        out=[]
        for sid in sorted(by):
            rows=split_adjust(compose(by[sid],patches[sid],daily[sid]),events)
            rows=[r for r in rows if r.get("trade_date",r["date"])<=asof]
            if rows:
                groups.append(rows); seen.add(sid); out.extend(rows)
        payload=_write_gz_ndjson(out)
        pname=f"US_ACTIVE_OHLC_PART_{part_no:04d}.ndjson.gz"
        drive.put(snapshot+"/"+pname,payload,"application/x-gzip",immutable=True)
        part_manifest.append({"name":pname,"rows":len(out),"bytes":len(payload),"sha256":_sha(payload)})

    tail=[]
    for sid in sorted(set(candidate_active)-seen):
        rows=split_adjust(compose([],patches[sid],daily[sid]),events)
        rows=[r for r in rows if r.get("trade_date",r["date"])<=asof]
        if rows:
            groups.append(rows); tail.extend(rows)
    if tail:
        payload=_write_gz_ndjson(tail)
        pname=f"US_ACTIVE_OHLC_PART_{len(part_manifest)+1:04d}.ndjson.gz"
        drive.put(snapshot+"/"+pname,payload,"application/x-gzip",immutable=True)
        part_manifest.append({"name":pname,"rows":len(tail),"bytes":len(payload),"sha256":_sha(payload)})

    bench_name=f"BENCHMARK_ETF_12_501_{asof.replace('-','')}.ndjson.gz"
    if not drive.file("US/DAILY/BENCHMARK/"+bench_name):
        raise RuntimeError("BENCHMARK_12_EXACT_ASOF_MISSING:"+asof)
    bench_bytes=drive.read("US/DAILY/BENCHMARK/"+bench_name)
    bench=_parse_gz_ndjson(bench_bytes)
    by=defaultdict(list)
    for row in bench:
        by[row["ticker"]].append(row)
    if set(by)!=set(BENCHMARK_ETFS) or any(len(v)!=WINDOW_BARS for v in by.values()):
        raise RuntimeError("BENCHMARK_12_501_IDENTITY_DRIFT")
    if any(max(r["date"] for r in v)!=asof for v in by.values()):
        raise RuntimeError("BENCHMARK_12_ASOF_DRIFT")
    drive.put(snapshot+"/BENCHMARK_ETF_12_501.ndjson.gz",bench_bytes,"application/x-gzip",immutable=True)

    manifest={"schema":"HUNTER_MONTHLY_SNAPSHOT_V2","snapshot":snapshot,"market":"US","as_of":asof,
              "semantic":"IMMUTABLE_READ_ONLY_BY_NAME_AND_SHA256","active_count":len(active),
              "candidate_active_count":len(candidate_active),
              "candidate_exclusion":"All BENCHMARK ETFs are excluded from stock candidate universe",
              "parts":part_manifest,"benchmark_etfs":list(BENCHMARK_ETFS),
              "benchmark_bars_each":WINDOW_BARS,"benchmark_sha256":_sha(bench_bytes)}
    drive.put(snapshot+"/MANIFEST.json",_compact(manifest),immutable=True)
    return snapshot,groups,bench


def _g178_validation(direction: dict, baseline: dict) -> dict:
    """Strict production regression against the frozen G178 2026-10-01 run."""
    def close(a, b, tol=1e-12):
        if a is None or b is None:
            return a is None and b is None
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)

    long_sel=direction.get("is_selected",{}).get("LONG") or {}
    short_sel=direction.get("is_selected",{}).get("SHORT") or {}
    standalone=direction.get("final_oos_standalone",{})
    long_oos=standalone.get("LONG") or {}
    short_oos=standalone.get("SHORT") or {}

    hard={
        "long_is_identity":(long_sel.get("ticker"),long_sel.get("signal"),long_sel.get("H"))==("SPY","S4",10),
        "long_is_core":long_sel.get("n")==64 and long_sel.get("benchmark_n")==199 and
            close(long_sel.get("wr"),0.8125) and
            close(long_sel.get("benchmark_wr"),0.7085427135678392) and
            close(long_sel.get("delta"),0.10395728643216084),
        "short_is_identity":(short_sel.get("ticker"),short_sel.get("signal"),short_sel.get("H"))==("IWM","S3",5),
        "short_is_core":short_sel.get("n")==11 and short_sel.get("benchmark_n")==50 and
            close(short_sel.get("wr"),0.6363636363636364) and
            close(short_sel.get("benchmark_wr"),0.3) and
            close(short_sel.get("delta"),0.33636363636363636),
        "long_final_oos":long_oos.get("n")==93 and long_oos.get("benchmark_n")==241 and
            close(long_oos.get("wr"),0.46236559139784944) and
            close(long_oos.get("benchmark_wr"),0.5560165975103735) and
            close(long_oos.get("delta"),-0.09365100611252403) and long_oos.get("pass") is False,
        "short_final_oos":short_oos.get("n")==0 and short_oos.get("benchmark_n")==245 and
            short_oos.get("wr") is None and close(short_oos.get("benchmark_wr"),0.4897959183673469) and
            short_oos.get("delta") is None and short_oos.get("pass") is False,
        "final_online_empty":direction.get("final_online")==[],
        "coverage_zero":close(direction.get("final_replay_counts",{}).get("coverage"),0.0),
        "long_direction_none":direction.get("LONG")=="无合格信号",
        "short_direction_none":direction.get("SHORT")=="无合格信号",
        "long_baseline_is":baseline["LONG"]["IS"]["n"]==5854 and
            close(baseline["LONG"]["IS"]["own"],0.3973315753041392) and
            close(baseline["LONG"]["IS"]["bench"],0.40606151903052695) and
            close(baseline["LONG"]["IS"]["delta"],-0.008729943726387746),
        "long_baseline_oos":baseline["LONG"]["FINAL_OOS"]["n"]==7410 and
            close(baseline["LONG"]["FINAL_OOS"]["own"],0.391389410574781) and
            close(baseline["LONG"]["FINAL_OOS"]["bench"],0.38934503480645566) and
            close(baseline["LONG"]["FINAL_OOS"]["delta"],0.002044375768325335),
        "short_baseline_is":baseline["SHORT"]["IS"]["n"]==7428 and
            close(baseline["SHORT"]["IS"]["own"],0.39592643775206443) and
            close(baseline["SHORT"]["IS"]["bench"],0.4052858110215986) and
            close(baseline["SHORT"]["IS"]["delta"],-0.009359373269534177),
        "short_baseline_oos":baseline["SHORT"]["FINAL_OOS"]["n"]==7611 and
            close(baseline["SHORT"]["FINAL_OOS"]["own"],0.44561326955688624) and
            close(baseline["SHORT"]["FINAL_OOS"]["bench"],0.4177880513769859) and
            close(baseline["SHORT"]["FINAL_OOS"]["delta"],0.027825218179900357),
        "long_baseline_verdict":baseline["LONG"]["verdict"]=="MARKET_DRIFT_ONLY",
        "short_baseline_verdict":baseline["SHORT"]["verdict"]=="OOS_ONLY",
    }
    diagnostics={
        "long_is_delta_ci":long_sel.get("delta_ci"),
        "short_is_delta_ci":short_sel.get("delta_ci"),
        "long_final_oos_delta_ci":long_oos.get("delta_ci"),
        "short_final_oos_delta_ci":short_oos.get("delta_ci"),
        "long_baseline_is_delta_ci":baseline["LONG"]["IS"].get("delta_ci"),
        "long_baseline_oos_delta_ci":baseline["LONG"]["FINAL_OOS"].get("delta_ci"),
        "short_baseline_is_delta_ci":baseline["SHORT"]["IS"].get("delta_ci"),
        "short_baseline_oos_delta_ci":baseline["SHORT"]["FINAL_OOS"].get("delta_ci"),
    }
    return {"pass":all(hard.values()),"hard_checks":hard,"diagnostics":diagnostics,
            "expected":{"LONG":"无合格信号","SHORT":"无合格信号",
                        "LONG_BASELINE":"MARKET_DRIFT_ONLY","SHORT_BASELINE":"OOS_ONLY"},
            "observed":{"LONG":direction.get("LONG"),"SHORT":direction.get("SHORT"),
                        "LONG_BASELINE":baseline["LONG"]["verdict"],
                        "SHORT_BASELINE":baseline["SHORT"]["verdict"]}}

def _month_name(asof: str) -> str:
    d=dt.date.fromisoformat(asof)
    return f"MONTH_{d.year}-{d.month:02d}"


def run_monthly(drive, snapshot_name: str | None = None, validation: bool = False) -> dict:
    checkpoint=drive.json("US/CONTROL/DAILY_CHECKPOINT.json")
    asof=checkpoint.get("last_completed_date") or checkpoint.get("as_of")
    if not asof:
        raise RuntimeError("US_DAILY_CHECKPOINT_DATE_MISSING")

    if snapshot_name:
        if not snapshot_name.startswith("SNAPSHOT_"):
            raise RuntimeError("SNAPSHOT_NAME_INVALID")
        asof=snapshot_name[len("SNAPSHOT_"):]
        manifest=drive.json(snapshot_name+"/MANIFEST.json")
        if manifest.get("as_of")!=asof:
            raise RuntimeError("SNAPSHOT_MANIFEST_ASOF_MISMATCH")
        groups=_load_snapshot_groups(drive,snapshot_name)
        bench=_load_snapshot_benchmark(drive,snapshot_name)
        split=_g178_split() if validation and snapshot_name==VALIDATION_SNAPSHOT else _dynamic_split(
            [r["date"] for r in bench if r.get("ticker")=="SPY"])
        snapshot=snapshot_name
    else:
        snapshot,groups,bench=_build_snapshot(drive,asof)
        split=_dynamic_split([r["date"] for r in bench if r.get("ticker")=="SPY"])

    direction=direction_backtest(bench,split)
    baseline=vertical_baseline(groups,split)
    direction["same_direction_test"]=same_direction_test(groups,split,direction)
    direction.pop("_emitted_dates",None)

    validation_result=_g178_validation(direction,baseline) if validation else {
        "pass":None,"checks":None,"expected":None,
        "observed":{"LONG":direction.get("LONG"),"SHORT":direction.get("SHORT"),
                    "LONG_BASELINE":baseline["LONG"]["verdict"],
                    "SHORT_BASELINE":baseline["SHORT"]["verdict"]}}
    if validation and not validation_result["pass"]:
        raise RuntimeError("G178_VALIDATION_MISMATCH:"+json.dumps(validation_result,ensure_ascii=False,sort_keys=True))

    month=_month_name(asof)
    prior_raw=drive.read("ACTIVE_POINTER") if drive.file("ACTIVE_POINTER") else None
    prior=json.loads(prior_raw) if prior_raw else None

    result_path=month+"/RESULT.json"
    existing_result=drive.json(result_path) if drive.file(result_path) else None
    now=(existing_result or {}).get("created_at_myt") or dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    result={"schema":"INVESTMENT_V2_MONTHLY_V2","month":month,"snapshot":snapshot,
            "as_of":asof,"created_at_myt":now,"direction":direction,
            "vertical_baseline":baseline,
            "excluded":{"dw_grid_recomputed":False,"dw_max_long":0.34,"dw_max_short":0.40,
                        "dwr_thr_recomputed":False},
            "validation":validation_result}
    if existing_result is not None and _compact(existing_result) != _compact(result):
        raise RuntimeError("MONTH_IMMUTABLE_RESULT_MISMATCH:"+month)

    drive.folder(month,create=True)
    drive.put(result_path,_compact(result),immutable=True)
    drive.put(month+"/DIRECTION_D1.json",_compact(direction),immutable=True)
    drive.put(month+"/VERTICAL_BASELINE.json",_compact(baseline),immutable=True)

    def signal_pointer(side: str):
        if direction[side]=="无合格信号":
            return "无合格信号"
        return {k:direction[side].get(k) for k in
                ("ticker","signal","H","wr","benchmark_wr","delta","delta_ci","n")}

    same_month = bool(prior and prior.get("month_file")==month and
                      prior.get("snapshot")==snapshot and prior.get("as_of")==asof)
    pointer={"schema":"INVESTMENT_V2_ACTIVE_POINTER_V3",
             "version":int(prior.get("version",1)) if same_month else
                       (1 if not prior else int(prior.get("version",0))+1),
             "month_file":month,"snapshot":snapshot,"as_of":asof,
             "long_direction_signal":signal_pointer("LONG"),
             "short_direction_signal":signal_pointer("SHORT"),
             "long_vertical_baseline":baseline["LONG"]["verdict"],
             "short_vertical_baseline":baseline["SHORT"]["verdict"],
             "direction_detail":{"IS_SELECTED":direction["is_selected"],
                                 "FINAL_OOS_STANDALONE":direction["final_oos_standalone"],
                                 "FINAL_REPLAY_METRICS":direction["final_replay_metrics"],
                                 "COVERAGE":direction["final_replay_counts"]["coverage"]},
             "previous":{"month_file":prior.get("month_file"),
                         "long_direction_signal":prior.get("long_direction_signal"),
                         "short_direction_signal":prior.get("short_direction_signal"),
                         "long_vertical_baseline":prior.get("long_vertical_baseline"),
                         "short_vertical_baseline":prior.get("short_vertical_baseline")} if prior else None,
             "month_update_pending":True,
             "writer":"HUNTER_MONTHLY_V2_US_PHASE","written_at_myt":now}
    # HK monthly is a separate track. The US phase must never erase the last
    # committed HK authority while the current month's HK recertification is
    # still pending.
    for key in (
        "hk_month_file","hk_snapshot","hk_as_of","hk_long_result",
        "hk_short_result","hk_shortable_list_version","hk_vertical_overlay",
        "hk_previous",
    ):
        if prior and key in prior:
            pointer[key]=prior[key]
    if same_month:
        comparable=dict(prior)
        comparable.update({"long_direction_signal":pointer["long_direction_signal"],
                           "short_direction_signal":pointer["short_direction_signal"],
                           "long_vertical_baseline":pointer["long_vertical_baseline"],
                           "short_vertical_baseline":pointer["short_vertical_baseline"],
                           "direction_detail":pointer["direction_detail"]})
        for key in ("long_direction_signal","short_direction_signal",
                    "long_vertical_baseline","short_vertical_baseline","direction_detail"):
            if prior.get(key) != comparable.get(key):
                raise RuntimeError("ACTIVE_POINTER_SAME_MONTH_CONFLICT:"+key)
        pointer=prior
    else:
        drive.put("ACTIVE_POINTER",_compact(pointer),
                  expected_sha=_sha(prior_raw) if prior_raw is not None else None)

    notice_path="US/CONTROL/MONTH_NOTICE.json"
    notice_raw=drive.read(notice_path) if drive.file(notice_path) else None
    notice={"schema":"INVESTMENT_V2_MONTH_NOTICE_V2","month_file":month,
            "pending":True,"pointer_version":pointer["version"],
            "created_at_myt":now,"previous":pointer["previous"],
            "current":{"long_direction_signal":pointer["long_direction_signal"],
                       "short_direction_signal":pointer["short_direction_signal"],
                       "long_vertical_baseline":pointer["long_vertical_baseline"],
                       "short_vertical_baseline":pointer["short_vertical_baseline"],
                       "direction_detail":pointer["direction_detail"]}}
    drive.put(notice_path,_compact(notice),
              expected_sha=_sha(notice_raw) if notice_raw is not None else None)
    return {"month":month,"snapshot":snapshot,"pointer":pointer,
            "validation":validation_result}

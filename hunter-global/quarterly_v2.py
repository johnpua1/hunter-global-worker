"""Quarterly V2.0 recertification for INVESTMENT-DIRECTION-FIRST-PICK-V2-01.

The quarterly writer is intentionally separate from US/HK DAILY.  It freezes one
read-only snapshot, recalculates only D1 + the vertical baseline comparison, and
commits ACTIVE_POINTER last.  D/W grids, DW_MAX and DWR/THR are never touched.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import random
from collections import defaultdict
from typing import Any, Iterable

INDEXES = ("SPY", "QQQ", "DIA", "IWM")
BENCHMARK_ETFS = ("SPY","QQQ","DIA","IWM","SSO","QLD","DDM","UWM","SDS","QID","DXD","TWM")
HORIZONS = (5, 10, 20)
BLOCK = 20
BOOTSTRAPS = 1000
G178_IS_END = "2025-09-30"
G178_OOS_START = "2025-10-01"
G178_OOS_END = "2026-09-22"
G178_IS_RATIO = 249 / 494
VALIDATION_SNAPSHOT = "SNAPSHOT_2026-10-01"
VALIDATION_MANIFEST_SHA = "6dcd6cb9b5abccaa2bc265a1e4a7678ed7edc4c0653c67e2b7f54eeef10260c5"


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


def _segment(date: str, split: dict[str, str]) -> str | None:
    if date <= split["is_end"]:
        return "IS"
    if split["oos_start"] <= date <= split["oos_end"]:
        return "FINAL_OOS"
    return None


def _dynamic_split(dates: list[str]) -> dict[str, str]:
    dates = sorted(dict.fromkeys(dates))
    if len(dates) < 100:
        raise RuntimeError("QUARTER_SPLIT_INSUFFICIENT_DATES")
    cut = max(1, min(len(dates)-1, round(len(dates) * G178_IS_RATIO)))
    return {
        "mode": "G178_RATIO_RECUT",
        "ratio_is": G178_IS_RATIO,
        "is_start": dates[0],
        "is_end": dates[cut-1],
        "oos_start": dates[cut],
        "oos_end": dates[-1],
    }


def _g178_split() -> dict[str, str]:
    return {
        "mode": "G178_VALIDATION_EXACT",
        "ratio_is": G178_IS_RATIO,
        "is_start": "2024-09-23",
        "is_end": G178_IS_END,
        "oos_start": G178_OOS_START,
        "oos_end": G178_OOS_END,
    }


def _block_bootstrap_delta(signal_by_date: dict[str, list[int]],
                           base_by_date: dict[str, list[int]],
                           seed_key: str) -> tuple[float | None, float | None]:
    dates = sorted(set(signal_by_date) | set(base_by_date))
    blocks = [dates[i:i+BLOCK] for i in range(0, len(dates), BLOCK)]
    if not blocks:
        return None, None
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(BOOTSTRAPS):
        chosen: list[str] = []
        for _j in range(len(blocks)):
            chosen.extend(blocks[rng.randrange(len(blocks))])
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

    # S1: last confirmed 8/17 cross; a cross is confirmed only when the
    # 8-17-9 histogram agrees.  Before the first confirmed cross = NONE.
    s1 = [0] * len(rows)
    state = 0
    for i in range(1, len(rows)):
        cross = 1 if hist[i] > 0 >= hist[i-1] else (-1 if hist[i] < 0 <= hist[i-1] else 0)
        if cross:
            state = cross
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
                       split: dict[str,str], segment: str, seed_key: str,
                       available_from: int) -> dict:
    sig: dict[str, list[int]] = defaultdict(list)
    base: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        if i < available_from or _segment(row["date"], split) != segment:
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


def direction_backtest(etf_rows: list[dict], split: dict[str,str]) -> dict:
    by_ticker: dict[str,list[dict]] = defaultdict(list)
    for row in etf_rows:
        if row.get("ticker") in INDEXES:
            by_ticker[row["ticker"]].append(row)
    positions = {}
    for ticker in INDEXES:
        by_ticker[ticker].sort(key=lambda x:x["date"])
        if len(by_ticker[ticker]) < 200:
            raise RuntimeError("D1_INDEX_HISTORY_MISSING:"+ticker)
        positions[ticker] = {row["date"]: i for i,row in enumerate(by_ticker[ticker])}

    candidates = []
    states = {}
    for ticker in INDEXES:
        rows = by_ticker[ticker]
        states[ticker] = _direction_state(rows)
        for signal in ("S1","S2","S3","S4"):
            for h in HORIZONS:
                for side_name, side in (("LONG",1),("SHORT",-1)):
                    for segment in ("IS","FINAL_OOS"):
                        warmup = {"S1":50,"S2":49,"S3":199,"S4":50}[signal]
                        m = _metric_for_signal(rows, states[ticker][signal], side, h, split, segment,
                                               f"{ticker}|{signal}|{h}|{side_name}|{segment}",
                                               warmup)
                        candidates.append({"ticker":ticker,"signal":signal,"H":h,
                                           "side":side_name,"segment":segment,**m})

    frozen: dict[str,dict|None] = {"LONG":None,"SHORT":None}
    standalone: dict[str,dict|None] = {"LONG":None,"SHORT":None}
    for side in ("LONG","SHORT"):
        eligible = [x for x in candidates if x["segment"]=="IS" and x["side"]==side
                    and x["wr"] is not None and x["wr"] >= .55
                    and x["delta_ci"][0] is not None and x["delta_ci"][0] > 0]
        eligible.sort(key=lambda x:(-x["wr"], -x["delta"], x["ticker"], x["signal"], x["H"]))
        if eligible:
            frozen[side] = eligible[0]
            fz = eligible[0]
            oos = next(x for x in candidates if x["segment"]=="FINAL_OOS" and
                       x["side"]==side and x["ticker"]==fz["ticker"] and
                       x["signal"]==fz["signal"] and x["H"]==fz["H"])
            standalone[side] = {**oos, "pass": bool(oos["wr"] is not None and oos["wr"] >= .55
                and oos["delta_ci"][0] is not None and oos["delta_ci"][0] > 0)}

    all_dates = [r["date"] for r in by_ticker["SPY"]
                 if split["oos_start"] <= r["date"] <= split["oos_end"]]
    online = {s:standalone[s] for s in ("LONG","SHORT") if standalone[s] and standalone[s]["pass"]}
    replay_rounds = []

    def active_on(side: str, rec: dict, date: str) -> bool:
        i = positions[rec["ticker"]].get(date)
        if i is None:
            return False
        want = 1 if side == "LONG" else -1
        return states[rec["ticker"]][rec["signal"]][i] == want

    def outcome(rec: dict, side: str, date: str) -> int | None:
        rows = by_ticker[rec["ticker"]]
        i = positions[rec["ticker"]].get(date)
        if i is None:
            return None
        entry_i, exit_i = i + 1, i + 1 + int(rec["H"])
        if exit_i >= len(rows):
            return None
        entry = float(rows[entry_i]["open"])
        final = float(rows[exit_i]["close"])
        if not (entry > 0 and math.isfinite(entry) and math.isfinite(final)):
            return None
        return int(final > entry) if side == "LONG" else int(final < entry)

    for round_no in (1,2):
        emitted = {"LONG":[], "SHORT":[]}
        for date in all_dates:
            active = [side for side,rec in online.items() if active_on(side,rec,date)]
            if len(active) == 1:
                emitted[active[0]].append(date)

        metrics = {}
        deleted = []
        for side,rec in list(online.items()):
            sig_by: dict[str,list[int]] = defaultdict(list)
            base_by: dict[str,list[int]] = defaultdict(list)
            for date in emitted[side]:
                value = outcome(rec,side,date)
                if value is not None:
                    sig_by[date].append(value)
            for date in all_dates:
                value = outcome(rec,side,date)
                if value is not None:
                    base_by[date].append(value)
            sv=[v for xs in sig_by.values() for v in xs]
            bv=[v for xs in base_by.values() for v in xs]
            wr=sum(sv)/len(sv) if sv else None
            bwr=sum(bv)/len(bv) if bv else None
            delta=wr-bwr if wr is not None and bwr is not None else None
            low,high=_block_bootstrap_delta(
                sig_by,base_by,
                f"REPLAY|{round_no}|{rec['ticker']}|{rec['signal']}|{rec['H']}|{side}|FINAL_OOS"
            ) if sv and bv else (None,None)
            metrics[side]={"n":len(sv),"benchmark_n":len(bv),"wr":wr,
                           "benchmark_wr":bwr,"delta":delta,"delta_ci":[low,high]}
            if low is None or low <= 0:
                deleted.append(side)

        counts={"LONG":len(emitted["LONG"]),"SHORT":len(emitted["SHORT"])}
        counts["NONE"]=max(0,len(all_dates)-counts["LONG"]-counts["SHORT"])
        counts["total"]=len(all_dates)
        counts["coverage"]=(counts["LONG"]+counts["SHORT"])/len(all_dates) if all_dates else 0.0
        replay_rounds.append({"round":round_no,"active_in":sorted(online),
                              "metrics":metrics,"counts":counts,"deleted":sorted(deleted)})
        if not deleted:
            break
        for side in deleted:
            online.pop(side,None)
        if not online:
            break

    final_online=sorted(online)
    daily_direction={}
    for date in all_dates:
        active=[side for side,rec in online.items() if active_on(side,rec,date)]
        daily_direction[date]=active[0] if len(active)==1 else "NONE"

    final_metrics=(replay_rounds[-1]["metrics"] if replay_rounds else {})
    def final_record(side: str):
        if side not in online:
            return "无合格信号"
        return {**online[side], "replay":final_metrics.get(side)}

    return {"split":split,"candidates":candidates,"is_selected":frozen,
            "final_oos_standalone":standalone,"replay_rounds":replay_rounds,
            "final_online":final_online,"daily_direction":daily_direction,
            "LONG":final_record("LONG"),"SHORT":final_record("SHORT")}


def _vertical_p(side: str, s0: float, st: float) -> float:
    move = st-s0 if side=="LONG" else s0-st
    return max(0.0, min(1.0, move/(s0*.05)))


def _stock_signals(rows: list[dict], split: dict[str,str],
                   target_map: dict[str,str]) -> Iterable[tuple[str,str,str,float,bool]]:
    if len(rows) < 501:
        return
    rows=sorted(rows,key=lambda x:x["date"])
    closes=[float(r["close"]) for r in rows]
    dates=[r["date"] for r in rows]
    by_date={r["date"]:r for r in rows}
    e8,e17,e50=_ema(closes,8),_ema(closes,17),_ema(closes,50)
    macd=[a-b for a,b in zip(e8,e17)]
    sig9=_ema(macd,9)
    hist=[a-b for a,b in zip(macd,sig9)]
    for i in range(50, len(rows)):
        date=dates[i]
        seg=_segment(date,split)
        target=target_map.get(date)
        if not seg or not target or target not in by_date:
            continue
        s0=float(rows[i]["close"])
        st=float(by_date[target]["close"])
        if not (s0>0 and math.isfinite(s0) and math.isfinite(st)):
            continue
        long_cross=(macd[i]>0>=macd[i-1] and e17[i]>e50[i] and hist[i]>0)
        short_cross=(macd[i]<0<=macd[i-1] and e17[i]<e50[i] and hist[i]<0)
        if long_cross:
            yield "LONG",seg,date,_vertical_p("LONG",s0,st),bool(st>s0)
        if short_cross:
            yield "SHORT",seg,date,_vertical_p("SHORT",s0,st),bool(st<s0)


def vertical_baseline(stock_groups: Iterable[list[dict]], split: dict[str,str],
                      calendar: list[str], d2_by_date: dict[str,str]) -> tuple[dict,dict]:
    calendar=sorted(dict.fromkeys(calendar))
    target_map={date:calendar[i+20] for i,date in enumerate(calendar[:-20])}
    market={side:defaultdict(lambda:[0.0,0]) for side in ("LONG","SHORT")}
    signals={side:{seg:[] for seg in ("IS","FINAL_OOS")} for side in ("LONG","SHORT")}

    for rows in stock_groups:
        rows=sorted(rows,key=lambda x:x["date"])
        if len(rows)<501:
            continue
        sid=rows[0]["security_id"]
        by_date={r["date"]:r for r in rows}
        for date,target in target_map.items():
            if date not in by_date or target not in by_date:
                continue
            s0=float(by_date[date]["close"]); st=float(by_date[target]["close"])
            if not (s0>0 and math.isfinite(s0) and math.isfinite(st)):
                continue
            for side in ("LONG","SHORT"):
                p=_vertical_p(side,s0,st)
                market[side][date][0]+=p
                market[side][date][1]+=1
        for side,seg,date,p,dwr_ok in _stock_signals(rows,split,target_map):
            signals[side][seg].append({"security_id":sid,"date":date,"p":p,"dwr":dwr_ok})

    out={}
    same_direction={side:{bucket:{"n":0,"vertical_wr":None,"avg_R":None,"DWR":None}
                          for bucket in ("SAME","OPPOSITE","NONE")}
                    for side in ("LONG","SHORT")}
    same_acc={side:{bucket:[] for bucket in ("SAME","OPPOSITE","NONE")}
              for side in ("LONG","SHORT")}

    for side in ("LONG","SHORT"):
        out[side]={}
        for seg in ("IS","FINAL_OOS"):
            own_by_date: dict[str,list[float]]=defaultdict(list)
            bench_by_date: dict[str,list[float]]=defaultdict(list)
            paired=[]
            for item in signals[side][seg]:
                total,count=market[side].get(item["date"],(0.0,0))
                if count<=1:
                    continue
                bench=(total-item["p"])/(count-1)
                own_by_date[item["date"]].append(item["p"])
                bench_by_date[item["date"]].append(bench)
                paired.append((item,bench))
            ov=[v for xs in own_by_date.values() for v in xs]
            bv=[v for xs in bench_by_date.values() for v in xs]
            om=sum(ov)/len(ov) if ov else None
            bm=sum(bv)/len(bv) if bv else None
            delta=om-bm if om is not None and bm is not None else None
            low,high=_block_bootstrap_delta(
                own_by_date,bench_by_date,
                f"VERTICAL_BASELINE|{side}|{seg}|{split['is_end']}|{split['oos_start']}|{split['oos_end']}"
            ) if ov and bv else (None,None)
            out[side][seg]={"n":len(ov),"own":om,"benchmark_n":len(bv),"bench":bm,
                            "delta":delta,"delta_ci":[low,high],
                            "unique_dates":len(own_by_date)}

            if seg=="FINAL_OOS":
                dw=.34 if side=="LONG" else .40
                for item,_bench in paired:
                    market_dir=d2_by_date.get(item["date"],"NONE")
                    bucket=("SAME" if market_dir==side else
                            "OPPOSITE" if market_dir in ("LONG","SHORT") else "NONE")
                    same_acc[side][bucket].append(
                        (item["p"]/dw-1.0, item["dwr"]))

        is_delta=out[side]["IS"]["delta"]
        oos_low=out[side]["FINAL_OOS"]["delta_ci"][0]
        if oos_low is not None and oos_low>0:
            out[side]["verdict"]="SIGNAL_EDGE_CONFIRMED" if is_delta is not None and is_delta>0 else "OOS_ONLY"
        else:
            out[side]["verdict"]="MARKET_DRIFT_ONLY"

    for side in ("LONG","SHORT"):
        for bucket,vals in same_acc[side].items():
            if vals:
                same_direction[side][bucket]={
                    "n":len(vals),
                    "vertical_wr":sum(1 for r,_ in vals if r>0)/len(vals),
                    "avg_R":sum(r for r,_ in vals)/len(vals),
                    "DWR":sum(1 for _,ok in vals if ok)/len(vals)
                }
    return out,same_direction


def _parse_gz_ndjson(data: bytes) -> list[dict]:
    return [json.loads(x) for x in gzip.decompress(data).splitlines() if x]


def _write_gz_ndjson(rows: list[dict]) -> bytes:
    raw=b"\n".join(_compact(x) for x in rows)+(b"\n" if rows else b"")
    return gzip.compress(raw,mtime=0)


def _parse_gz_csv(data: bytes) -> list[dict]:
    stream=io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(data)),encoding="utf-8-sig",newline="")
    out=[]
    for row in csv.DictReader(stream):
        item=dict(row)
        for key in ("open","high","low","close","volume"):
            if key in item and item[key] not in ("",None):
                item[key]=float(item[key])
        out.append(item)
    return out


def _iter_gz_csv_groups(data: bytes) -> Iterable[list[dict]]:
    stream=io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(data)),encoding="utf-8-sig",newline="")
    current=None
    group=[]
    for row in csv.DictReader(stream):
        sid=row.get("security_id")
        if not sid:
            raise RuntimeError("SNAPSHOT_SECURITY_ID_MISSING")
        item=dict(row)
        for key in ("open","high","low","close","volume"):
            if item.get(key) not in ("",None):
                item[key]=float(item[key])
        if current is None:
            current=sid
        if sid!=current:
            yield group
            group=[]
            current=sid
        group.append(item)
    if group:
        yield group


def _load_snapshot_groups(drive, snapshot: str) -> Iterable[list[dict]]:
    files=drive.list(snapshot)
    names={x["name"] for x in files}
    parts=sorted(name for name in names
                 if name.startswith("US_ACTIVE_OHLC_PART_") and name.endswith(".ndjson.gz"))
    if parts:
        for name in parts:
            rows=_parse_gz_ndjson(drive.read(snapshot+"/"+name))
            by=defaultdict(list)
            for row in rows:
                by[row["security_id"]].append(row)
            for sid in sorted(by):
                yield by[sid]
        return
    legacy="US_ACTIVE_OHLC.csv.gz"
    if legacy in names:
        yield from _iter_gz_csv_groups(drive.read(snapshot+"/"+legacy))
        return
    raise RuntimeError("SNAPSHOT_OHLC_MISSING:"+snapshot)


def _load_snapshot_bench(drive, snapshot: str) -> list[dict]:
    names={x["name"] for x in drive.list(snapshot)}
    nd="BENCHMARK_ETF_12_501.ndjson.gz"
    legacy="BENCHMARK_ETF_12_501.csv.gz"
    if nd in names:
        return _parse_gz_ndjson(drive.read(snapshot+"/"+nd))
    if legacy in names:
        return _parse_gz_csv(drive.read(snapshot+"/"+legacy))
    raise RuntimeError("SNAPSHOT_BENCHMARK_MISSING:"+snapshot)


def _build_snapshot(drive, asof: str) -> tuple[str,list[list[dict]],list[dict]]:
    snapshot="SNAPSHOT_"+asof
    existing=drive.file(snapshot+"/MANIFEST.json")
    if existing:
        manifest=drive.json(snapshot+"/MANIFEST.json")
        if manifest.get("as_of") != asof:
            raise RuntimeError("SNAPSHOT_NAME_ASOF_CONFLICT")
        groups=_load_snapshot_groups(drive,snapshot)
        bench=_load_snapshot_bench(drive,snapshot)
        return snapshot,groups,bench

    universe=drive.json("US/CURRENT_UNIVERSE.json")
    active={x["security_id"]:x for x in universe["securities"] if x.get("listing_status")=="ACTIVE"}
    base_asof=universe.get("as_of")
    daily=defaultdict(dict)
    for item in drive.list("US/DAILY"):
        name=item["name"]
        if not (len(name)==10 and name[4]=="-" and name[7]=="-"):
            continue
        if base_asof and name<=base_asof or name>asof:
            continue
        for f in drive.list("US/DAILY/"+name):
            if f["name"].endswith((".gz",".gzip")):
                for row in _parse_gz_ndjson(drive.read("US/DAILY/"+name+"/"+f["name"])):
                    if row.get("security_id") in active:
                        daily[row["security_id"]][row.get("date") or row.get("trade_date")]=row

    drive.folder(snapshot,create=True)
    uni_doc={"schema":"HUNTER_QUARTER_SNAPSHOT_V1","snapshot_name":snapshot,
             "market":"US","as_of":asof,"active_count":len(active),
             "securities":[active[k] for k in sorted(active)]}
    drive.put(snapshot+"/US_ACTIVE_UNIVERSE.json",_compact(uni_doc),immutable=True)

    groups=[]
    part_manifest=[]
    base_files=sorted(x["name"] for x in drive.list("US/BASE") if x["name"].endswith(".ndjson.gz"))
    seen=set()
    for part_no,name in enumerate(base_files,1):
        by=defaultdict(dict)
        for row in _parse_gz_ndjson(drive.read("US/BASE/"+name)):
            sid=row.get("security_id")
            if sid in active:
                by[sid][row["date"]]=row
        out=[]
        for sid in sorted(by):
            by[sid].update(daily.get(sid,{}))
            rows=sorted(by[sid].values(),key=lambda x:x["date"])[-501:]
            groups.append(rows); seen.add(sid); out.extend(rows)
        payload=_write_gz_ndjson(out)
        pname=f"US_ACTIVE_OHLC_PART_{part_no:04d}.ndjson.gz"
        drive.put(snapshot+"/"+pname,payload,"application/x-gzip",immutable=True)
        part_manifest.append({"name":pname,"rows":len(out),"bytes":len(payload),"sha256":_sha(payload)})
    # New listings with no historical BASE are still frozen with all available DAILY.
    tail=[]
    for sid in sorted(set(active)-seen):
        rows=sorted(daily.get(sid,{}).values(),key=lambda x:x["date"])[-501:]
        if rows:
            groups.append(rows); tail.extend(rows)
    if tail:
        payload=_write_gz_ndjson(tail)
        pname=f"US_ACTIVE_OHLC_PART_{len(part_manifest)+1:04d}.ndjson.gz"
        drive.put(snapshot+"/"+pname,payload,"application/x-gzip",immutable=True)
        part_manifest.append({"name":pname,"rows":len(tail),"bytes":len(payload),"sha256":_sha(payload)})

    bench_name=f"BENCHMARK_ETF_12_501_{asof.replace('-','')}.ndjson.gz"
    if not drive.file("US/DAILY/BENCHMARK/"+bench_name):
        candidates=sorted(x["name"] for x in drive.list("US/DAILY/BENCHMARK")
                          if x["name"].startswith("BENCHMARK_ETF_12_501_"))
        if not candidates:
            raise RuntimeError("BENCHMARK_12_MISSING")
        bench_name=candidates[-1]
    bench_bytes=drive.read("US/DAILY/BENCHMARK/"+bench_name)
    drive.put(snapshot+"/BENCHMARK_ETF_12_501.ndjson.gz",bench_bytes,"application/x-gzip",immutable=True)
    bench=_parse_gz_ndjson(bench_bytes)
    manifest={"schema":"HUNTER_QUARTER_SNAPSHOT_V1","snapshot":snapshot,"market":"US","as_of":asof,
              "semantic":"IMMUTABLE_READ_ONLY_BY_NAME_AND_SHA256","active_count":len(active),
              "parts":part_manifest,"benchmark_etfs":list(BENCHMARK_ETFS),
              "benchmark_sha256":_sha(bench_bytes)}
    drive.put(snapshot+"/MANIFEST.json",_compact(manifest),immutable=True)
    return snapshot,groups,bench


def _quarter_name(asof: str) -> str:
    d=dt.date.fromisoformat(asof)
    return f"QUARTER_{d.year}Q{(d.month-1)//3+1}"


def run_quarterly(drive, snapshot_name: str | None = None, validation: bool = False) -> dict:
    checkpoint=drive.json("US/CONTROL/DAILY_CHECKPOINT.json")
    asof=checkpoint.get("last_completed_date") or checkpoint.get("as_of")
    if not asof:
        raise RuntimeError("US_DAILY_CHECKPOINT_DATE_MISSING")

    if snapshot_name:
        if not snapshot_name.startswith("SNAPSHOT_"):
            raise RuntimeError("SNAPSHOT_NAME_INVALID")
        asof=snapshot_name[len("SNAPSHOT_"):]
        manifest=drive.json(snapshot_name+"/MANIFEST.json")
        if manifest.get("as_of") != asof:
            raise RuntimeError("SNAPSHOT_MANIFEST_ASOF_MISMATCH")
        snapshot=snapshot_name
        groups=_load_snapshot_groups(drive,snapshot)
        bench=_load_snapshot_bench(drive,snapshot)
    else:
        snapshot,groups,bench=_build_snapshot(drive,asof)

    spy_dates=sorted(r["date"] for r in bench if r.get("ticker")=="SPY")
    split=_g178_split() if validation and snapshot==VALIDATION_SNAPSHOT else _dynamic_split(spy_dates)
    direction=direction_backtest(bench,split)
    baseline,same_direction=vertical_baseline(
        groups,split,spy_dates,direction.get("daily_direction",{}))
    direction["same_direction_test"]=same_direction

    def selected_key(side: str):
        row=direction.get("is_selected",{}).get(side)
        return None if not row else [row.get("ticker"),row.get("signal"),row.get("H")]
    def standalone_pass(side: str):
        row=direction.get("final_oos_standalone",{}).get(side)
        return None if row is None else bool(row.get("pass"))

    expected={
        "LONG":"无合格信号","SHORT":"无合格信号",
        "IS_LONG":["SPY","S4",10],"IS_SHORT":["IWM","S3",5],
        "FO_LONG_PASS":False,"FO_SHORT_PASS":False,
        "LONG_BASELINE":"MARKET_DRIFT_ONLY","SHORT_BASELINE":"OOS_ONLY"
    }
    observed={
        "LONG":direction["LONG"],"SHORT":direction["SHORT"],
        "IS_LONG":selected_key("LONG"),"IS_SHORT":selected_key("SHORT"),
        "FO_LONG_PASS":standalone_pass("LONG"),"FO_SHORT_PASS":standalone_pass("SHORT"),
        "LONG_BASELINE":baseline["LONG"]["verdict"],
        "SHORT_BASELINE":baseline["SHORT"]["verdict"]
    }
    if validation and observed != expected:
        raise RuntimeError("G178_VALIDATION_MISMATCH:"+json.dumps(observed,ensure_ascii=False,sort_keys=True))

    quarter=_quarter_name(asof)
    prior=drive.json("ACTIVE_POINTER") if drive.file("ACTIVE_POINTER") else None
    created=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    result={"schema":"INVESTMENT_V2_QUARTERLY_V1","quarter":quarter,"snapshot":snapshot,
            "as_of":asof,"created_at_myt":created,
            "direction":direction,"vertical_baseline":baseline,
            "excluded":{"dw_grid_recomputed":False,"dw_max_long":0.34,"dw_max_short":0.40,
                        "dwr_thr_recomputed":False},
            "validation":{"requested":validation,"expected":expected if validation else None,
                          "observed":observed,"pass":(observed==expected) if validation else None}}
    drive.folder(quarter,create=True)
    drive.put(quarter+"/RESULT.json",_compact(result),immutable=True)
    drive.put(quarter+"/DIRECTION_D1.json",_compact(direction),immutable=True)
    drive.put(quarter+"/VERTICAL_BASELINE.json",_compact(baseline),immutable=True)

    def signal_pointer(side: str):
        row=direction[side]
        if row=="无合格信号":
            return "无合格信号"
        return {k:row.get(k) for k in ("ticker","signal","H","wr","benchmark_wr","delta","delta_ci","n")}

    pointer={"schema":"INVESTMENT_V2_ACTIVE_POINTER_V1",
             "version":1 if not prior else int(prior.get("version",0))+1,
             "quarter_file":quarter,"snapshot":snapshot,"as_of":asof,
             "long_direction_signal":signal_pointer("LONG"),
             "short_direction_signal":signal_pointer("SHORT"),
             "long_vertical_baseline":baseline["LONG"]["verdict"],
             "short_vertical_baseline":baseline["SHORT"]["verdict"],
             "direction_detail":{"LONG":direction.get("final_oos_standalone",{}).get("LONG"),
                                 "SHORT":direction.get("final_oos_standalone",{}).get("SHORT"),
                                 "coverage":(direction.get("replay_rounds") or [{}])[-1]
                                            .get("counts",{}).get("coverage",0.0)},
             "previous":{"quarter_file":prior.get("quarter_file"),
                         "long_direction_signal":prior.get("long_direction_signal"),
                         "short_direction_signal":prior.get("short_direction_signal"),
                         "long_vertical_baseline":prior.get("long_vertical_baseline"),
                         "short_vertical_baseline":prior.get("short_vertical_baseline")} if prior else None,
             "writer":"HUNTER_QUARTERLY_V2","written_at_myt":created}
    expected_sha=_sha(_compact(prior)) if prior else None
    drive.put("ACTIVE_POINTER",_compact(pointer),expected_sha=expected_sha)

    notice_path="US/CONTROL/QUARTER_NOTICE.json"
    notice={"schema":"INVESTMENT_V2_QUARTER_NOTICE_V1","quarter_file":quarter,"pending":True,
            "pointer_version":pointer["version"],"old":pointer["previous"],
            "new":{"long_direction_signal":pointer["long_direction_signal"],
                   "short_direction_signal":pointer["short_direction_signal"],
                   "long_vertical_baseline":pointer["long_vertical_baseline"],
                   "short_vertical_baseline":pointer["short_vertical_baseline"],
                   "direction_detail":pointer["direction_detail"]},
            "created_at_myt":created}
    notice_sha=_sha(drive.read(notice_path)) if drive.file(notice_path) else None
    drive.put(notice_path,_compact(notice),expected_sha=notice_sha)
    return {"quarter":quarter,"snapshot":snapshot,"pointer":pointer,
            "validation":result["validation"],"observed":observed}


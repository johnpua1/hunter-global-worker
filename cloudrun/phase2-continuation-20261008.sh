#!/usr/bin/env bash
(
set -euo pipefail
umask 077
phase2_sha="${1:-}"
if [[ ! "$phase2_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'STOPPED=请使用包含固定版本号的完整下载执行命令'; exit 1
fi
phase2_work="$(mktemp -d "$HOME/hunter-phase2-closeout.XXXXXX")"
trap 'rm -rf -- "$phase2_work"' EXIT
printf '%s\n' 'PREPARING_PINNED_SOURCE=正在自动准备固定版本收口代码'
mkdir -p "$phase2_work/source"
git -C "$phase2_work/source" init -q
GIT_TERMINAL_PROMPT=0 git -C "$phase2_work/source" fetch -q --depth=1 \
  https://github.com/johnpua1/hunter-global-worker.git "$phase2_sha"
if [ "$(git -C "$phase2_work/source" rev-parse FETCH_HEAD)" != "$phase2_sha" ]; then
  echo 'STOPPED=SOURCE_SHA_MISMATCH'; exit 1
fi
git -C "$phase2_work/source" checkout -q --detach FETCH_HEAD
if ! python3 -c 'import requests' >/dev/null 2>&1; then
  python3 -m pip install --quiet --target "$phase2_work/deps" requests
fi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$phase2_work/deps${PYTHONPATH:+:$PYTHONPATH}" python3 -u - "$phase2_work/source" "$phase2_sha" <<'PHASE2_PY' 2>&1 | tee -a "$HOME/hunter-phase2-closeout.log"
import concurrent.futures, contextlib, datetime as dt, fcntl, importlib.util, os, pathlib, re, sys, time
from zoneinfo import ZoneInfo

ROOT, SHA = pathlib.Path(sys.argv[1]), sys.argv[2]
EXPECTED_SHA = {"US": SHA, "HK": SHA}
os.environ["HUNTER_CONTINUE_ONLY"] = "1"
CANCELLED_BY_UPGRADE = set()
PROGRESS_CREDITED = set()
GOAL = {"US": "2026-10-07", "HK": "2026-10-08"}
LOCK_ERROR = "BRIDGE_Lock timeout: another process was holding the lock for too long."
CANCELLED_BY_THIS_MONITOR = set()
OBSERVED_FAILURES = {
    "hunter-us-daily-6wgdv": ("US", "DAILY_INCOMPLETE:US:MARKET_WIDE_DATA_UNAVAILABLE", "US_SOURCE_WAIT_THEN_ONE_RESUME"),
    "hunter-hk-daily-jcwng": ("HK", "BRIDGE_Service error: Drive", "HK_CONFIRMED_READ_CACHE_FAILURE_ONE_RESUME"),
    "hunter-hk-daily-cc7hv": ("HK", "BRIDGE_Service error: Drive", "HK_PATCHED_READ_CACHE_FAILURE_ONE_RESUME"),
}
OBSERVED_RECOVERY_USED = set()
LAST_FATAL = {}
US_SOURCE = {"next_check": 0.0, "ready": False}
TZ = ZoneInfo("Asia/Kuala_Lumpur")
LOCK = open(pathlib.Path.home() / "hunter-phase2-closeout.lock", "a")
try:
    fcntl.flock(LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("MONITOR_ALREADY_RUNNING=已有监控正在运行；本次未部署或启动任务；升级监控时只停止旧终端监控，保留云端执行", flush=True)
    sys.exit(1)

spec = importlib.util.spec_from_file_location("phase2_closeout_helpers", ROOT / "cloudrun/deploy-large-read-fix-20261008.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
spec = importlib.util.spec_from_file_location("phase2_us_pack_deployment", ROOT / "cloudrun/deploy-continuation-20261008.py")
pack_deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack_deployment)
sys.path.insert(0, str(ROOT / "cloudrun"))
from closeout_gcloud import gc as monitored_gc
r.gc = monitored_gc
sys.path.insert(0, str(ROOT / "hunter-global"))
from runner import Drive, validate_phase2_resume, closed_dates_since, fetch_security

def emit(text):
    print(dt.datetime.now(TZ).strftime("%m-%d %H:%M:%S MYT") + " " + text, flush=True)

def safe(text):
    text = re.sub(r"https?://\S+", "[URL]", str(text))
    text = re.sub(r"(?i)(token|secret|shared_key|authorization)([=:\s]+)\S+", r"\1=[REDACTED]", text)
    return text[:350]

class Reader(Drive):
    def _call(self, op, **kwargs):
        if op not in {"file", "list", "read", "read_chunk", "read_verified_chunk", "source_inventory"}:
            raise RuntimeError("READ_ONLY_VERIFICATION")
        return super()._call(op, **kwargs)

@contextlib.contextmanager
def reader(doc, market):
    saved = dict(os.environ)
    obj = None
    try:
        env = {e["name"]: e for e in r.deployment.container(doc, market).get("env", [])}
        for key in ("APPS_SCRIPT_WEBAPP_URL", "APPS_SCRIPT_SHARED_KEY"):
            os.environ[key] = r.base.deploy.preflight_env_value(doc, env.get(key, {}), key)
        os.environ.update(HUNTER_BRIDGE_READ_TIMEOUT_SECONDS="90", HUNTER_BRIDGE_ATTEMPTS="2", HUNTER_READ_CHECKPOINTS="0")
        obj = Reader()
        yield obj
    finally:
        if obj is not None:
            obj.http.close()
        os.environ.clear(); os.environ.update(saved)

def choose(cp, market):
    date, phase = cp.get("last_completed_date"), cp.get("phase2_completed_date")
    if cp.get("market") != market or not date:
        raise RuntimeError("CHECKPOINT_IDENTITY_INVALID")
    dt.date.fromisoformat(date)
    if date < "2026-10-06" or (phase and phase > date):
        raise RuntimeError("CHECKPOINT_REGRESSION_OR_CONFLICT")
    if phase == date and date >= GOAL[market]:
        if not cp.get("phase2_completed_at_myt"):
            raise RuntimeError("PHASE2_COMPLETION_TIME_MISSING")
        return "COMPLETE", date
    if date < GOAL[market]:
        return "NEW_SESSION", date
    return "PHASE2", date

def active(rows):
    return [r.base.recovery.name(x) for x in rows if r.base.recovery.state(x) == "ACTIVE"]

def head(rows):
    return (r.base.recovery.name(rows[0]), r.base.recovery.state(rows[0])) if rows else (None, None)

def peer_active(market):
    # All Bridge writes share a ScriptLock. Preserve active peers, including
    # queued executions, before reading checkpoints or starting another job.
    return [name for peer in GOAL if peer != market for name in active(r.executions(peer))]

def recoverable_failure(name, market):
    if name in CANCELLED_BY_UPGRADE:
        return "OLD_EXECUTION_STOPPED_FOR_NEW_VERSION"
    if name in CANCELLED_BY_THIS_MONITOR:
        return "OWN_COMPETING_EXECUTION_CANCELLED"
    query = ('resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="' + name
             + '" AND "hunter halted:"')
    logs = r.gc("logging", "read", query, "--freshness=1d", "--limit=1", "--order=desc")
    if logs:
        row = logs[0]
        message = str(row.get("textPayload") or row.get("jsonPayload", {}).get("message", ""))
        fatal = next((line.partition("hunter halted:")[2].strip()
                      for line in message.splitlines() if "hunter halted:" in line), "")
        LAST_FATAL[name] = fatal
        observed = OBSERVED_FAILURES.get(name)
        if (observed and observed[:2] == (market, fatal)
                and name not in OBSERVED_RECOVERY_USED):
            return observed[2]
        if fatal == LOCK_ERROR:
            # Gateway.waitLock fails before the write transaction begins.
            # STALE_WRITE, hash conflicts and unrelated errors stay fatal.
            return "BRIDGE_LOCK_TIMEOUT"
        if fatal and fatal not in {"WORK_BUDGET_EXHAUSTED_RESUME_REQUIRED",
                                   "LARGE_READ_TRANSPORT_RETRY_REQUIRED"}:
            return None
    if r.timeout_failure(name):
        return "EXISTING_RESUMABLE_INTERRUPTION"
    return None

def us_source_ready():
    # This check runs in Cloud Shell. It only gates one retry; the Cloud Run
    # worker still has to pass its full coverage and persistence checks.
    if time.monotonic() < US_SOURCE["next_check"]:
        return US_SOURCE["ready"]
    symbols = ("AAPL", "MSFT", "AMZN", "GOOGL", "SPY")
    date = GOAL["US"]
    keys = ("HUNTER_HTTP_RETRY_ATTEMPTS", "HUNTER_HTTP_TIMEOUT_SECONDS")
    saved = {key: os.environ.get(key) for key in keys}

    def probe(symbol):
        security = {"market": "US", "ticker": symbol, "security_id": "US:" + symbol}
        rows, flags, _, reason = fetch_security(security, [date], date, daily=True)
        ok = (len(rows) == 1 and rows[0].get("date") == date
              and "PASS_DAILY" in flags and "DATA_SUSPECT" not in flags)
        detail = "READY" if ok else str(reason or "INVALID_TARGET_BAR").split(":", 1)[0]
        return symbol, ok, safe(detail)

    emit("US SOURCE_CHECK=QUERY1 TARGET=" + date + " SAMPLES=5")
    try:
        os.environ.update(HUNTER_HTTP_RETRY_ATTEMPTS="1", HUNTER_HTTP_TIMEOUT_SECONDS="12")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(probe, symbols))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    ready = sum(ok for _, ok, _ in results)
    US_SOURCE.update(next_check=time.monotonic() + 300, ready=ready == len(symbols))
    emit("US SOURCE_COMPLETE_OHLCV=" + str(ready) + "/5 "
         + " ".join(symbol + "=" + detail for symbol, _, detail in results))
    if not US_SOURCE["ready"]:
        emit("US WAIT_SOURCE_TARGET=" + date + ";下次小样本检查在5分钟后；本次不启动美股整批执行")
    return US_SOURCE["ready"]

def daily_already_saved(doc, market):
    date = GOAL[market]
    path = market + "/CONTROL/DAILY_RUN_" + date + ".json"
    with reader(doc, market) as d:
        record = d.json(path) if d.file(path) else {}
    return (record.get("market") == market and record.get("trade_date") == date
            and record.get("status") == "COMPLETE")

seen = {}
def progress(market, name):
    query = ('resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="' + name
             + '" AND ("PHASE2_" OR "DERIVED_" OR "INPUT_CACHE_" OR "INPUT_PACK_" OR "LARGE_READ_" OR "SYNC_")')
    try:
        logs = r.gc("logging", "read", query, "--freshness=1d", "--limit=1", "--order=desc")
        if logs and seen.get(market) != logs[0].get("timestamp"):
            row = logs[0]
            text = row.get("textPayload") or row.get("jsonPayload", {}).get("message", "")
            match = re.search(r"(?:PHASE2_|DERIVED_|INPUT_CACHE_|INPUT_PACK_|LARGE_READ_|SYNC_)[A-Z_]+[^\n]*", str(text))
            if match:
                emit(market + " " + name + " LOG_AT=" + str(row.get("timestamp", "UNKNOWN")) + " " + safe(match.group()))
            seen[market] = row.get("timestamp")
    except Exception:
        emit(market + " PROGRESS_LOG_UNAVAILABLE;现有任务保留")

def full_proof(doc, market, date):
    with reader(doc, market) as d:
        cp = validate_phase2_resume(d, market, date)
        run = d.json(market + "/CONTROL/DAILY_RUN_" + date + ".json")
        if (choose(cp, market) != ("COMPLETE", date) or run.get("market") != market
                or run.get("trade_date") != date or run.get("status") != "COMPLETE"):
            raise RuntimeError("FINAL_DAILY_RANK_PHASE2_MISMATCH")

def credit_saved_progress(market, latest, key, attempts):
    if latest in PROGRESS_CREDITED:
        return
    query = ('resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="'
             + latest + '" AND "DERIVED_BATCH_SAVED"')
    rows = r.gc("logging", "read", query, "--freshness=1d", "--limit=1", "--order=desc")
    if rows:
        PROGRESS_CREDITED.add(latest)
        attempts[key] = 0
        emit(market + " SAVED_BATCH_PROGRESS=" + latest + ";允许接续余下批次")

def step(market, doc, attempts):
    rows = r.executions(market)
    running = active(rows)
    if running:
        emit(market + " KEEP_RUNNING=" + ",".join(running))
        progress(market, running[0])
        return None
    peers = peer_active(market)
    if peers:
        emit(market + " WAIT_FOR_ACTIVE_PEER=" + ",".join(peers))
        return None
    cp = r.checkpoint(doc, market)
    stage, date = choose(cp, market)
    emit(market + " DAILY=" + date + " PHASE2=" + str(cp.get("phase2_completed_date", "MISSING")))
    if stage == "COMPLETE":
        return date
    latest, state = head(rows)
    if state == "FAILED":
        reason = recoverable_failure(latest, market)
        if reason is None:
            progress(market, latest)
            if LAST_FATAL.get(latest):
                emit(market + " LATEST_FATAL=" + safe(LAST_FATAL[latest]))
            raise RuntimeError("UNRECOGNIZED_FAILURE_REQUIRES_TARGETED_FIX:" + latest)
        emit(market + " RECOVERABLE_FAILURE=" + reason + " EXECUTION=" + latest)
    key = (market, stage, date)
    if state == "FAILED" and latest:
        credit_saved_progress(market, latest, key, attempts)
    if attempts.get(key, 0) >= 3:
        raise RuntimeError("BOUNDED_RESUME_LIMIT_REACHED")
    if (market == "US" and stage == "NEW_SESSION"
            and not daily_already_saved(doc, market) and not us_source_ready()):
        return None
    args = ["--mode", "auto", "--market", market]
    if stage == "PHASE2":
        with reader(doc, market) as d:
            validate_phase2_resume(d, market, date)
        args += ["--phase2-only", "--as-of", date]
    else:
        pending = closed_dates_since(market, date)
        if not pending or pending[-1] != GOAL[market]:
            raise RuntimeError("NEW_SESSION_NOT_EXACTLY_FROZEN_GOAL")
    current = r.gc("run", "jobs", "describe", "hunter-" + market.lower() + "-daily")
    r.verify(doc, current, market, EXPECTED_SHA[market])
    if pack_deployment.configuration(r, doc) != pack_deployment.configuration(r, current):
        raise RuntimeError("CONFIGURATION_CHANGED_NO_START")
    if choose(r.checkpoint(current, market), market) != (stage, date):
        return None
    latest_rows = r.executions(market)
    if active(latest_rows) or head(latest_rows) != head(rows) or peer_active(market):
        return None
    attempts[key] = attempts.get(key, 0) + 1
    if latest in OBSERVED_FAILURES:
        OBSERVED_RECOVERY_USED.add(latest)
    job = "hunter-" + market.lower() + "-daily"
    result = r.gc("run", "jobs", "execute", job, "--args=" + ",".join(args), "--async")
    own = r.base.recovery.name(result)
    if not re.fullmatch(re.escape(job) + r"-[a-z0-9]+", own or ""):
        raise RuntimeError("LAUNCH_RESPONSE_UNKNOWN_DO_NOT_REPEAT_BLINDLY")
    emit(market + " STARTED=" + own + " STAGE=" + stage + " FROM=" + date)
    other = [n for n in active(r.executions(market)) if n != own] + peer_active(market)
    if other:
        r.gc("run", "jobs", "executions", "cancel", own, "--async")
        CANCELLED_BY_THIS_MONITOR.add(own)
        emit(market + " CONCURRENT_EXISTING_PRESERVED=" + ",".join(other) + ";仅取消本程序刚创建的竞争执行")
    return None

def error_reason(exc):
    reason = str(exc)
    if reason.startswith("GCLOUD_FAILED:"):
        return reason
    # Only program-defined codes are exposed; arbitrary response bodies stay private.
    if re.fullmatch(r"[A-Z][A-Z0-9_]*(?::(?:hunter-(?:us|hk)-daily-[a-z0-9]+|US|HK))?", reason):
        return reason
    category = reason.split(":", 1)[0].split(" ", 1)[0]
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", category):
        return type(exc).__name__ + ":" + category
    return type(exc).__name__ + ":UNCLASSIFIED_MONITOR_ERROR"

def main():
    emit("VERSION=20261008_CONTINUATION_ONLY;GOAL=US:2026-10-07,HK:2026-10-08;两市统一新版本，只继续未完成阶段，写后不回读")
    deadline = time.monotonic() + 7 * 3600
    prepared = pack_deployment.deploy_both(r, SHA)
    CANCELLED_BY_UPGRADE.update(prepared["cancelled"])
    if prepared["status"] != "VERIFIED":
        raise RuntimeError("US_PACK_DEPLOYMENT_NOT_VERIFIED")
    docs = prepared["docs"]
    for market in GOAL:
        doc = docs[market]
        r.verify(doc, doc, market, EXPECTED_SHA[market])
        if r.base.deploy.daily_runtime(doc) != (7200, 0):
            raise RuntimeError("EXPECTED_EXISTING_RUNTIME_REQUIRED:" + market)
    attempts, done, stopped = {}, {}, {}
    while time.monotonic() < deadline:
        for market, doc in docs.items():
            if market in done or market in stopped:
                continue
            try:
                date = step(market, doc, attempts)
                if date:
                    done[market] = date
            except Exception as exc:
                stopped[market] = error_reason(exc)
                emit(market + " MONITOR_STOPPED=" + stopped[market] + ";云端执行未被本错误处理取消")
        if len(done) == 2:
            for market, doc in docs.items():
                cp = r.checkpoint(doc, market)
                stage, date = choose(cp, market)
                if stage != "COMPLETE" or active(r.executions(market)):
                    done.pop(market, None)
                    continue
                full_proof(doc, market, date)
                done[market] = date
            if len(done) == 2:
                final = {}
                for market, doc in docs.items():
                    cp = r.checkpoint(doc, market)
                    rows = r.executions(market)
                    if choose(cp, market) != ("COMPLETE", done[market]) or active(rows):
                        done.pop(market, None)
                        emit(market + " FINAL_STATE_CHANGED;继续等待并重新核验")
                        continue
                    final[market] = (done[market], head(rows)[1])
                if len(final) == 2:
                    for market, (date, state) in final.items():
                        emit(market + " DAILY_RANK_PHASE2_VERIFIED=" + date
                             + " EXECUTION_STATE=" + str(state))
                    emit("US_AND_HK_PHASE2_ACCEPTED_US_2026-10-07_HK_2026-10-08")
                    return
        if len(done) + len(stopped) == 2:
            raise RuntimeError("PHASE2_NOT_ACCEPTED_SEE_MARKET_STOPPED")
        time.sleep(45)
    raise RuntimeError("MONITOR_TIME_LIMIT_EXISTING_CLOUD_RUN_EXECUTIONS_PRESERVED")

try:
    main()
except Exception as exc:
    emit("NOT_ACCEPTED=" + error_reason(exc))
    sys.exit(1)
PHASE2_PY
)

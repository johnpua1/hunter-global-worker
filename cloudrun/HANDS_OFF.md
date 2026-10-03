# Hunter unattended-operation hardening

All operator times use Asia/Kuala_Lumpur (MYT, UTC+8).

| Component | Normal trigger | Recovery |
| --- | --- | --- |
| US Daily | Apps Script, 06:00–07:00 MYT | Hourly watchdog from 07:00; at most two additional launches per MYT day |
| HK Daily | Apps Script, 18:00–19:00 MYT | Hourly watchdog from 19:00; at most two additional launches per MYT day |
| Monthly | Existing monthlyV2 handler | Next-morning check remains armed until both market pointers commit |
| Maintenance | Cloud Scheduler, 20:00 MYT | Existing Cloud Run task retry; queue progress is durable |

The watchdog uses the same ScriptLock and durable launch receipt as normal daily triggers. Running/pending executions and successful launches for the same MYT date are skipped. An ambiguous launch remains blocked and alerts instead of submitting a duplicate. Compensation is bounded; persistent failures require investigation.

Daily success is the guarded business execution receipt, not an unrelated configuration probe. The existing daily worker determines completed trading sessions and holidays. The watchdog does not fabricate checkpoint dates or treat weekends as missing trading sessions.

Maintenance uses cooperative I/O deadlines, five-row queue checkpoints, and separate reserves for queue CAS and lock release. The clock starts before startup checks. Expired fetch work never consumes a repair attempt or becomes a NO_DATA finding. Threaded repair and option fetches carry their deadlines. HTTP timeouts are inactivity bounds, not an absolute wall-clock kill guarantee. Cloud Run retains its 3600-second hard limit.

Deploy a clean checkout of the pinned reviewed release:

```sh
python3 cloudrun/deploy-hands-off.py --wait-idle
```

The helper checks the Maintenance-only Scheduler topology, builds only when needed, waits for running jobs, deploys only Maintenance, verifies its configuration and mutex, patches only the reviewed Bridge functions plus Watchdog.gs, preserves daily/monthly trigger IDs and other live source, and installs/readbacks one hourly watchdog. It does not start another historical repair run. Unknown source/configuration changes block deployment. Failed Maintenance mutex verification rolls back its image/configuration; failed Bridge deployment verification rolls back the Bridge version/source. A later watchdog installation error leaves the already verified runtime patch deployed; rerunning reads current state before attempting installation.

`HARDENING_DEPLOYED_AND_READ_BACK` confirms deployment/readback only. It does not claim the next automatic business cycle succeeded. Check watchdog lastCheck, the next daily checkpoints and run receipts, the next 20:00 Maintenance result, and monthly pointer commitment when due. The 28 NO_DATA entries observed on 2026-10-03 remain subject to bounded retries and evidence gates.

Existing Cloud Monitoring policies receive job failures and hunter-control errors. Maintenance failure email delivery was observed on 2026-10-03. Hourly compensation and drift alerts depend on Apps Script executing; a complete Apps Script outage is not independently covered by this watchdog. This release is not proof that every possible silent failure is detected.

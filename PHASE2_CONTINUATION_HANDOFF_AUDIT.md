# Continuation monitor handoff audit — 2026-10-08

Observed from the user's 20:55 MYT screenshot: both templates use worker 702bc750ed544efe7eea94ff2a96f343865b30e5; HK started hunter-hk-daily-7m95r; US was stopped by the monitor because old execution hunter-us-daily-59mqq reported BRIDGE_READ_CHUNK_POSITION_MISMATCH:REPAIR_QUEUE.json.

This patch changes the monitor only. It attaches to two already-deployed matching continuation-enabled templates without building, updating or cancelling them. The worker image remains 702bc750ed544efe7eea94ff2a96f343865b30e5 for both markets.

The handoff exception is limited to that exact old US execution and error, requires the recorded old source identity and a COMPLETE US DAILY_RUN for the target date, and requires the new continuation-enabled US template. The new worker skips that completed daily stage and continues unfinished ranking. The exception is consumed once on launch. It does not classify a current-worker integrity error, a different path, a missing daily completion record, or a mismatched worker as recoverable.

Code audit checked source/version identity, exact failure scope, stage completion preconditions, one-use launch credit, preservation of active HK execution, and no worker-image mutations. Python compilation and shell syntax checks passed. After the audit, 19 isolated handoff, controller and deployment tests passed:

```sh
PYTHONPATH=/workspace/scratch/13ab152026d1/test-deps:hunter-global:tests python -m unittest test_continuation_handoff test_continuation_controller test_continuation_deploy -q
```

This permits a new-worker handoff when its preconditions are satisfied. It does not establish the underlying old queue-size/offset mismatch's cause or claim that general concurrent queue mutation is repaired. Production acceptance still depends on the completion records produced by the workers.

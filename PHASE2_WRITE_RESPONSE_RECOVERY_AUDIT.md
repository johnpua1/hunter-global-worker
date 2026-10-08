# Phase 2 write-response recovery — 2026-10-08 MYT

Status: code audited and tested; production deployment and acceptance pending.
Parent: d869789ad39575079b47f4ff466f64b6bec21aa1.
Deployment input for both workers: 702bc750ed544efe7eea94ff2a96f343865b30e5.

## Observed evidence
HK execution 7m95r failed in derived_resume.BatchResults.save -> immutable put -> _continuation_write -> HTTP POST ReadTimeout (120 seconds). US screenshot confirms HTTP 404 on script.googleusercontent.com; its upper frames were not visible. The deployment controller retrieves each exact latest execution traceback and permits the handoff only if the immutable batch-save call chain is present. It rejects unrelated or mutable-write failures before building/deploying.

## Change and audit
- Trusted response-link GET may retry at most three times. It never reissues POST, sends the shared key to another host, or downloads the stored Drive output.
- A put with immutable=True and no expected_sha256 may recover an unacknowledged transport outcome with at most three identical requests. The existing Gateway ScriptLock + identical-SHA return precedes createFile; different content raises IMMUTABLE_CONFLICT. No server change is needed. This is a narrowly idempotent operation, not a general retry of ambiguous writes.
- Mutable puts, CAS requests and append requests are never reposted by this recovery. Hash conflicts, unknown errors, authorization failures and canonical endpoint 404 are not accepted as success.
- Exhaustion is explicitly classified IMMUTABLE_WRITE_TRANSPORT_RETRY_REQUIRED; shared uncertainty state still blocks further writes and exit cache flush. A later worker can restore saved immutable batch state.
- Deployment accepts only current 702 workers or its own pinned new release; both templates must use the new release before launch. No rollback, old-image launch, cache deletion or Bridge change.
- derived.py, analytics.py and derived_resume.py exactly match deployed 702 source, preserving calculation signatures and saved batch keys. Existing output progress rules remain in force.

## Tests
129 Python regression tests passed, then the four existing deployment race/failure tests passed against the actual new deployment helper (19 tests in the targeted module, including the 15 already covered).
The actual Gateway.gs code was executed in the Node test harness: lost-ACK replay produced the identical acknowledgment with exactly one file creation; repeated rename/trash were forbidden; differing bytes were rejected without mutation. Existing Bridge tests also passed.
Python compilation and shell syntax checks passed.

## Limits
These tests are not production acceptance. Unknown mutable-write outcomes still require specific resolution and are not bypassed. Necessary unfinished-stage inputs may be restored after process restart. No completion-time or zero-network-failure guarantee is made. This patch does not modify or certify the future daily scheduling system.

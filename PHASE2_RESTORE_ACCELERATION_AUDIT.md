# Phase 2 bounded restore acceleration — 2026-10-08 MYT

Status: code prepared, production deployment/acceptance pending.
Parent release: b747df7deb0f9257c8e74f5c94c027bc1a63f032.

## Changes
- Existing immutable read-cache pieces restore with up to four concurrent requests, globally bounded across simultaneous pack restorations in the execution. Ordering, byte lengths, offsets, SHA checks and work/checkpoint time budgets are preserved. Only four pieces per restore are queued; failures cancel pending work. Independent HTTP reader sessions close after worker completion.
- Manifest-named compressed pieces use one direct Bridge read without a preceding file-metadata query. Path scope is restricted to hashed READ_CACHE part files. The response hash, path hash, compressed size cap and decoded piece hash/length are checked. Shared per-path memory prevents duplicate successful transfers within the same execution.
- In continuation mode a malformed cache manifest, failed/corrupt pack or unusable unchanged cached entry never becomes an automatic cache miss and source download. The failure retains the manifest and loaded-version state and suppresses exit cache flush. New/changed source inputs remain readable for genuinely new daily work.
- Calculation files derived.py, analytics.py and derived_resume.py were not changed; existing batch keys and persisted pre-ranking results remain compatible.
- Deployment accepts current 702, b747 or the new release as template inputs, writes the same new image to both markets, and preserves existing active workers. New workers receive acceleration. There is no old-image rollback, cache clearing or completed-data rescan.
- Known cancelled continuation workers may resume in response to the user's stop/resume instruction. Unknown/integrity failures and forbidden rebuilds remain failures, never completion.

## Verification
Python tests cover bounded cross-pack concurrency, byte-exact ordering, no duplicate successful piece transfer, cancellation of prefetch on error, budget-context propagation, absence of per-piece metadata reads, cache failure without source reads/writes, compatible completed-stage reuse, deployment failure handling and preserving active executions.
Shell syntax and Python compilation passed. See /tmp/hunter-accelerator-final-tests.log for the local test result.

## Limits
This reduces restore request count and overlapping wait time; no production speed multiple or finish deadline has been measured. Restoring necessary cached inputs after process restart remains necessary. Active workers keep their current image and speed until completion/interruption. No claim of production acceptance or future daily scheduling acceptance is made.

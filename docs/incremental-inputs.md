# Incremental daily inputs

Enable with `HUNTER_INCREMENTAL_INPUTS=1` after deploying the matching Bridge.
Use `cloudrun/deploy-incremental-inputs-20261007.sh <exact-commit-sha>`.
It updates the existing Bridge deployment and daily job images. It does not
launch/cancel executions, change schedules, or increase runtime resources.
The script checks the new inventory operation before enabling either worker.

The first execution builds a verified input cache under each market's existing
`CONTROL/INCREMENTAL_CACHE`. Later executions restore compressed input packs,
not hundreds of individual historical originals. A batched, metadata-only
Bridge inventory identifies files by path, size and Drive content MD5. Only
new or changed originals are fetched. This also detects equal-size corrections,
late additions to old DAILY folders, deletion and new corporate actions.

This is incremental source I/O, not a change to financial formulas: ranking
still computes indicators from full relevant history, including long-window
EMA/RSI and split adjustments. Phase 2 uses the same cached BASE and repair
inputs as ranking. It does not bypass validation or manufacture completion.

Packs are sized below existing Bridge limits. A SHA-256 manifest is published
with CAS after pack writes verify. Two slots bound ordinary tail-pack updates;
small new daily inputs fill an existing tail pack. Every restored source is
checked against the live fingerprint and stored SHA-256. Corrupt/missing packs
fall back to the authoritative originals; an input changing during its read
fails validation. Cache writes never modify BASE, DAILY or repair originals.

Validated DAILY inputs are saved before market fetching; ranking inputs are
saved before Phase 2. In addition, new reads/writes checkpoint after 25 dirty
files, 6 MB of dirty bytes, or 120 seconds at the next completed read/write.
The checkpoint serializes cache state while independent source reads remain
bounded and concurrent. A separate HTTP session writes verified packs before
CAS-publishing the manifest. Failed checkpoint writes stop normal processing;
an already-failing run preserves its original exception and logs a failed
final-save attempt. Cache state is never a market completion receipt.

An abrupt process kill retains the last successful checkpoint; the current
uncommitted batch may be read again. The restored cache is revalidated against
fresh source fingerprints. Recovery therefore avoids rereading saved unchanged
originals, rather than trusting a numeric file offset that can become stale.
Known inventory metadata also avoids a redundant `file` request per cold read.
Initial cache creation and corrupt-cache recovery still require original reads.

With `HUNTER_RESUMABLE_RUN=1`, normal Bridge requests stop after 6000 seconds
of worker processing. Checkpoint requests have a separate reserve ending at
6900 seconds, below the unchanged 7200-second Cloud Run limit. Request hops
respect the remaining budget. Budget exhaustion fails the execution explicitly,
so the existing watchdog does not mistake a partial run for success. This is
not a guarantee of source latency or of completion in a single execution.

Before publishing PHASE2 history/calendar, verified compressed result blobs and
a CAS manifest are saved under `CONTROL/PHASE2_RESUME`. They bind the market,
daily date, MYT processing day, source fingerprints, universe and sector map.
After an interrupted write/readback, matching results resume publication without
recomputing reactions. Both destinations must match their expected old or desired
hashes; conflicting writes and corrupt staging stop publication. Completion is
still committed only after both outputs verify. Changed inputs invalidate staged
computations. The result blobs are content addressed and retained for recovery.

Operational evidence:

- `INPUT_SOURCE_READ`: actual original input fetched after a cache miss.
- `INPUT_PACK_RESTORED`: previously verified history restored from a pack.
- `INPUT_CACHE_COMMITTED`: a usable cache manifest was saved.
- `INPUT_CACHE_SUMMARY`: original reads versus reused input reads for this run.
- `INPUT_CACHE_RESUME`: unchanged saved inputs available on this execution.
- `PHASE2_RESULTS_STAGED`: results are durable but completion is not yet committed.
- `PHASE2_RESUMING_RESULT_COMMIT`: resuming publication from verified staged results.
- `PHASE2_COMMITTED`: existing market completion criterion, unchanged.

Unit/integration coverage includes 500 historical DAILY segments plus next-day
and late appends, 664 repair files for both markets, exact ranking/Phase 2 output
equivalence, corrections/deletion/splits, corruption recovery, interrupted and
conflicting cache writes, concurrent pack reuse, bounded tail-pack growth, and
Bridge pagination/market isolation. Production timing and the first warm
scheduled execution still require live verification after deployment.

## Unified recovery release, October 7

Run `python3 cloudrun/deploy-resumable-phase2-20261007.py <pinned-commit-sha>`.
The already-deployed incremental Bridge is preflighted, not redeployed. One
image build updates both existing daily templates and enables resumable mode;
resources, task timeout/retries, schedules, and active executions stay intact.
The script follows both markets, skips committed dates, and launches only
Phase 2 for committed US Oct 6 / HK Oct 7 daily and ranking inputs. It checks
for running/queued work before every launch and cancels only its own new
execution if it observes another concurrent launcher.

The Cloud Shell monitor retries only timeout/work-budget failures (up to three
launches per market in this invocation), prints progress, and verifies both
completion dates. Other errors stop that invocation with an explicit incomplete
result. The monitor ends after at most seven hours; closing Cloud Shell can
interrupt monitoring, while already-started Cloud Run work continues. Tomorrow's
normal auto entry uses the same durable cache and completion rules; the recovery
argument override never changes the scheduled entrypoint.

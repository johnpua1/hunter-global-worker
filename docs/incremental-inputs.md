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
saved before Phase 2. A later stage failure therefore retains useful cached
inputs. Cache save failures are explicit `INPUT_CACHE_SAVE_FAILED` errors in
logs; they do not misrepresent otherwise valid market results as incomplete.
The next run rebuilds any missing cache entries. Initial cache creation and
cache recovery can still require original-history reads.

Operational evidence:

- `INPUT_SOURCE_READ`: actual original input fetched after a cache miss.
- `INPUT_PACK_RESTORED`: previously verified history restored from a pack.
- `INPUT_CACHE_COMMITTED`: a usable cache manifest was saved.
- `INPUT_CACHE_SUMMARY`: original reads versus reused input reads for this run.
- `PHASE2_COMMITTED`: existing market completion criterion, unchanged.

Unit/integration coverage includes 500 historical DAILY segments plus next-day
and late appends, 664 repair files for both markets, exact ranking/Phase 2 output
equivalence, corrections/deletion/splits, corruption recovery, interrupted and
conflicting cache writes, concurrent pack reuse, bounded tail-pack growth, and
Bridge pagination/market isolation. Production timing and the first warm
scheduled execution still require live verification after deployment.

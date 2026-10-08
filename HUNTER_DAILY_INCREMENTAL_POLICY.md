# Daily incremental update requirement

User instruction confirmed 2026-10-09 00:51 MYT, clarified 00:54 MYT. Applies
to US and HK updates, including daily data, indicators and ranking. This records the required behavior; it
does not claim that the current production worker already implements it.

## Required behavior

- Process the current closed trading day and explicitly missing trading days.
- Reuse acknowledged historical data and committed outputs. Do not download,
  recompute, rewrite, audit, review or scan historical business data merely
  because a new day, process, deployment or recovery attempt starts.
- Resume unfinished work only. Do not reset checkpoints or clear saved work.
- Advance indicators using persisted calculation state plus the newly supplied
  bars. Reading bounded calculation/checkpoint state is distinct from loading
  historical raw data, source archives or all old repair files again.
- Existing historical business data is read-only archival material for a
  separately requested query or analysis that actually needs it. No update
  task may invoke that query exception as part of its own preparation.
- Update tasks, including daily runs and recovery, must not scan, verify,
  read back, review, audit, rewrite or delete existing historical business
  data. Inserting a genuinely missing date is not permission to revisit dates
  that already have committed data.
- Historical corrections are not automatically authorized by an update job;
  they require a separate, explicitly scoped user instruction.
- Keep existing US work scoped to 2026-10-07 and HK work to 2026-10-08 until
  that closeout completes. Never silently extend those recovery goals.
- Continue daily scheduling without requiring routine Cloud Shell commands.

## No silent fallback

The persisted calculation/checkpoint state referenced above must be a dedicated
operational state, not a relabelled historical archive or yesterday's business
output that is scanned to reconstruct missing state.

If the calculation state needed for a delta update is absent, incompatible or
incomplete, report the specific missing state. Do not automatically re-read
history to reconstruct it. Do not substitute a stale ranking, change indicator
formulas, advance a completion date or publish an acceptance marker to hide it.

Deleting validation calls or increasing read concurrency is not a substitute
for implementing incremental state. Existing downloaded file caches alone do
not satisfy the requirement if each new run still reloads historical contents.

Code review and tests may use synthetic fixtures to verify the implementation;
they must not cause production historical-data scans. Keep daily-data receipt,
ranking publication and full Phase 2 completion as distinct evidence.

## Release evidence required

Before calling the runtime compliant, demonstrate with isolated test fixtures:

1. Next-day and missing-day runs read only changed/new business files and
   bounded prior calculation state; historical raw-file reads remain zero.
2. A restart does not redo committed batches or outputs.
3. Incremental indicators/ranking match the established formulas.
4. Missing state reports an explicit gap rather than invoking a full rebuild.
5. Active executions and saved progress are preserved during deployment.

The previously proposed daily-only HK acceptance/deletion was withdrawn by
the user's later instruction to finish ranking. It is not an active waiver.

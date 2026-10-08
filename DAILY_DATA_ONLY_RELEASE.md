# US/HK daily data separated from permanently retired Phase 2

Authorized 2026-10-09 MYT. This release is code and a deployment procedure;
publication alone does not mean the user's Cloud Run jobs have changed.

## Behavior

- A dedicated image enters daily_data_only.py. It contains no foundation,
  ranking, analytics, Phase 2 or old recovery modules. Its runner.py is only
  the extracted Bridge transport, without the old main function.
- The existing US/HK daily job names and Scheduler targets are retained.
  Legacy ordinary daily mode arguments route to data-only ingestion.
- DAILY_DATA_CHECKPOINT is independent of the frozen ranking/Phase 2 pointer.
  Bootstrap reads the old control date only, then trusts completed DAILY_RUN
  receipts. It does not scan or reconstruct archived prices.
- New requests cover only unprocessed closed dates. Stock requests cover
  exactly one day, without the old five-day padding or corporate-action scan.
- Current symbol identity metadata is used when a new date needs data. This
  does not load historical OHLC, BASE, repair archives, rankings or earnings.
- Pending per-day fetch stages allow restart; committed batches are skipped.
  A lost append ACK is reconciled by metadata for that exact pending output.
- The existing Bridge still enforces append uniqueness against other files
  in the same target-day folder. No previous-day folder is requested.
- Missing symbols are explicit COMPLETE_WITH_GAPS, not full data coverage.
  Market-wide source failure does not advance the daily pointer; retries
  fetch only symbols that have no committed bar.
- A legacy partial date with no usable receipt stops safely. This release
  never scans the archive or guesses deduplication keys to resolve it.
- Phase 2 checkpoints, old ranking files and historical data remain untouched.

## Deployment

Use cloudrun/deploy-daily-data-only.sh with its exact published commit SHA.
The script builds the dedicated image, changes both existing job templates,
confirms the changed templates, cancels old active daily/Phase 2 executions,
waits for their termination and requests data-only executions. It does not
deploy the retired worker or silently roll back. A running old local monitor
must first be stopped with Ctrl+C; the shared lock prevents conflicting work.

Cloud Shell needs the user's existing project access. Credential failure
stops before building. Keep the shell connected until deployment finishes;
the launched Cloud Run jobs and existing schedulers run independently after it.

Deployment output DAILY_DATA_CUTOVER_DEPLOYED proves template cutover only.
Live daily success requires DAILY_DATA_DONE for each market and the new
DAILY_DATA_CHECKPOINT. These live checks have not been performed by the agent.

## Validation

Synthetic tests cover both markets, completed-date skip, no archive access,
restart without refetch, lost append ACK, source outage recovery, explicit
coverage gaps, date boundaries, source request ranges, isolated image imports,
secret/resource preservation, and no launch after deployment failure.
No production historical business files were read for these tests.

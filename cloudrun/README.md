# Hunter Cloud Run release

Deploy only from a verified merged `main` commit after Phase 1 live Bridge
gates pass. `deploy.sh` checks the exact commit, linked billing, and project
access. It prompts for the Bridge URL and key only inside authenticated Cloud
Shell and writes them directly to Secret Manager. Do not paste either secret
in chat, a source file, a shell command, or GitHub.

Run `cloudrun/deploy.sh` with `GCP_PROJECT_ID` and `MERGED_MAIN_SHA` set. The
script creates or updates three jobs, without any Scheduler jobs. Each job is
one task, 1 vCPU, 512MiB, 60 minutes, one retry; the Python worker uses ten
network workers. The third job executes `maintenance.py`. Maintenance runs
repair, catches up weekly official listing refresh, aggregates existing split
events, and catches up the current month's options labels. An independent
benchmark remains unset until the user supplies the benchmark definition.

Manually execute US and HK in Cloud Run, inspect each execution's exit status,
logs, checkpoint readback, BASE SHA, and Drive sidecar/write receipts. Run each
twice and confirm the second run adds zero DAILY rows. Execute maintenance on
the 2026-09-25 baseline and inspect its repair, split, and metadata receipts.
No newer market bar is required for these release checks.

Set the account-currency equivalent of USD 1 as `BUDGET_AMOUNT` and run
`cloudrun/budget.sh` with `BILLING_ACCOUNT_ID` and `GCP_PROJECT_ID`. Confirm
the alert recipients. A budget is a warning, not a spending cap.

Only after both market jobs pass, run `cloudrun/schedule.sh` with both
`CLOUD_RUN_US_PASS=YES` and `CLOUD_RUN_HK_PASS=YES`. It creates exactly three
time-zone-aware HTTP schedules, then disable the GitHub production schedules
without deleting their workflow files. Inspect the Cloud Scheduler list and
billing-account free-tier usage before scheduling; other projects share the
three free Scheduler jobs and Cloud Run allowances. Monitor Cloud Run CPU,
memory, network, Scheduler, Artifact Registry, and Cloud Build monthly.

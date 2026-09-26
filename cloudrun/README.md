# Hunter Cloud Run release

Deploy only from the merged `main` commit after Phase 1 live Bridge
deployment. `deploy.sh` checks the exact commit, linked billing, and project
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

At the planning assumption of 8,466 securities, two seconds per fetch, ten
internal workers, and 22 trading days, 1 vCPU uses about 37,200 vCPU-seconds
per month (15.5% of the 240,000 vCPU-second Cloud Run allowance). This is a
capacity estimate, not proof of RM0 billing; inspect actual free-tier usage,
memory, network, Cloud Build, Registry storage, and other projects on the same
billing account.

Set the account-currency equivalent of USD 1 as `BUDGET_AMOUNT` and run
`cloudrun/budget.sh` with `BILLING_ACCOUNT_ID` and `GCP_PROJECT_ID`. Confirm
the alert recipients. A budget is a warning, not a spending cap.

After the three Cloud Run jobs are configured, run `cloudrun/schedule.sh`. It
creates exactly three Asia/Kuala_Lumpur schedules: US and HK daily at 08:37
(the existing production time), maintenance Saturday at 10:00. Disable the
GitHub production schedules without deleting their workflow files. Inspect the Cloud Scheduler list and
billing-account free-tier usage before scheduling; other projects share the
three free Scheduler jobs and Cloud Run allowances. Monitor Cloud Run CPU,
memory, network, Scheduler, Artifact Registry, and Cloud Build monthly.

# Hunter Cloud Run release

Deploy only from the merged `main` commit after Phase 1 live Bridge
deployment. `deploy.sh` checks the exact commit, linked billing, project
access, and the existing enabled Secret Manager versions. It never prompts
for or creates the Bridge secrets.

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

After the three Cloud Run jobs are configured, run `cloudrun/phase1.sh`. It
upserts the three Asia/Kuala_Lumpur schedules (US 07:15 Tue-Sat, HK 17:45
Mon-Fri, maintenance 20:00 daily), initializes the foundation, executes
maintenance twice, drains OPEN repairs, and builds 2026-09-25 DERIVED. The
GitHub production schedules remain disabled. Inspect the Cloud Scheduler list and
billing-account free-tier usage before scheduling; other projects share the
three free Scheduler jobs and Cloud Run allowances. Monitor Cloud Run CPU,
memory, network, Scheduler, Artifact Registry, and Cloud Build monthly.

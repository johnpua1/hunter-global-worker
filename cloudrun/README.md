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

## Identity and trigger hardening

Production trigger authority is singular. The default is `APPS_SCRIPT`; `phase1.sh`
will not create Cloud Scheduler jobs unless `HUNTER_TRIGGER_AUTHORITY=CLOUD_SCHEDULER`
is explicitly set. Never run both authorities for the same production job.

`cloudrun/deploy.sh` supports two IAM modes:

- `HUNTER_IAM_MODE=legacy` (default): preserves the current shared
  `hunter-jobs@` service account and shared Bridge key.
- `HUNTER_IAM_MODE=scoped`: requires three dedicated runtime service accounts
  (`hunter-us-daily@`, `hunter-hk-daily@`, `hunter-maintenance@`) and three
  per-worker Bridge key secrets. The common Apps Script URL secret remains shared.
  Deployment fails if a scoped runtime account has broad project-level roles such
  as Owner, Editor, Viewer, Run Admin/Developer, Secret Manager Admin, Artifact
  Registry Admin, or Storage Admin.

The Apps Script gateway accepts optional scoped keys through script properties
`BRIDGE_US_KEY`, `BRIDGE_HK_KEY`, and `BRIDGE_MAINT_KEY`. The legacy
`BRIDGE_SHARED_KEY` remains accepted until cutover. US and HK scoped keys are
path-isolated; they may update the shared `REPAIR_QUEUE.json` only if the
foreign-market portion is byte-semantically unchanged. `_BRIDGE_TEST` remains
legacy-only.

Run `cloudrun/iam-audit.sh` from an authenticated Cloud Shell for a read-only
live IAM audit before and after any scoped cutover.



## V2 monthly recertification

`cloudrun/deploy-monthly.sh` deploys only `hunter-monthly-v2`. It uses a dedicated
`hunter-monthly@` runtime service account and a Bridge credential scoped to
`SNAPSHOT_* / MONTH_* / ACTIVE_POINTER / US/CONTROL/MONTH_NOTICE.json`.

The worker runs once per month after the first completed US trading session has
been written by DAILY. It freezes an immutable `SNAPSHOT_<date>`, re-runs the
registered D1 direction acceptance flow and vertical-baseline comparison, writes
immutable `MONTH_<YYYY-MM>` artifacts, and commits `ACTIVE_POINTER` last.
D/W grids, the frozen 0.34 / 0.40 D/W limits, and DWR / THR / THR_TOUCH are not
recomputed by this job.

The Apps Script one-shot trigger begins at 08:00 MYT on calendar day 2 of the
target month and retries each morning until DAILY proves that the first US
session of that month has completed. After a successful run it arms the next
month. `cloudrun/deploy-monthly-full.sh` updates the existing Apps Script Web
App, performs the frozen `SNAPSHOT_2026-10-01` G178 regression, validates
`ACTIVE_POINTER`, installs the monthly trigger, and only then retires any
superseded quarterly Cloud Run job/secret/service account.
